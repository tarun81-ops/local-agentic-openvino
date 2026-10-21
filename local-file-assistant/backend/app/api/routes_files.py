import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core import indexer, known_folders, personalize
from app.core.assistant import learning
from app.core.watcher import watcher
from app.db import sqlite_fts

router = APIRouter(prefix="/files", tags=["files"])


class FolderRequest(BaseModel):
    folder: str


class PathRequest(BaseModel):
    path: str
    query: str = ""  # the search that led to opening it (optional; feeds search ranking)


def _conn():
    return sqlite_fts.connect(settings.db_path)


NAMES_ONLY = ("This app is set to names only: it doesn't keep copies of file text, so folders aren't indexed. "
              "Find and chat still work with every file whole-PC access covers. To index folders again, set "
              "STORE_FILE_TEXT=true in backend\\.env and restart.")


def _no_text_store():
    if not settings.store_file_text:
        raise HTTPException(status_code=409, detail=NAMES_ONLY)


# One index run at a time: a single-user local app, not a job queue.
_status_lock = threading.Lock()
_COUNTS = {"seen": 0, "indexed": 0, "skipped": 0, "failed": 0, "removed": 0, "keyword_only": 0}
_status = {"state": "idle", "folder": None, "queue": [], "started": None, "error": None, **_COUNTS}


def _update(**kw):
    with _status_lock:
        _status.update(kw)


def _run_index(folders: list[Path]) -> None:
    """Scans folders one after another (a single folder when added from the Index page, all
    unscanned folders at startup)."""
    indexer.CANCEL.clear()
    try:
        for i, folder in enumerate(folders):
            if indexer.CANCEL.is_set():
                break
            _update(folder=str(folder), queue=[str(f) for f in folders[i + 1 :]], started=time.monotonic(), **_COUNTS)
            result = indexer.index_folder(folder, settings.db_path, settings.vector_db_dir, on_progress=lambda c: _update(**c))
            _update(**result)
        _update(state="cancelled" if indexer.CANCEL.is_set() else "done", queue=[])
    except Exception as exc:
        _update(state="error", error=f"{type(exc).__name__}: {exc}", queue=[])


def _start_index(folders: list[Path]) -> dict:
    with _status_lock:
        if _status["state"] == "running":
            raise HTTPException(status_code=409, detail=f"Already scanning {_status['folder']}. Try again when it finishes.")
        _status.update(state="running", folder=str(folders[0]), error=None, **_COUNTS)
    threading.Thread(target=_run_index, args=(folders,), name="index-run", daemon=True).start()
    return {"state": "started", "folder": str(folders[0])}


def rescan_unscanned() -> list[str]:
    """Called at startup: scans folders that were never fully scanned — new ones whose scan
    was interrupted, and all of them after an index schema upgrade."""
    conn = _conn()
    try:
        folders = [Path(p) for p in sqlite_fts.unscanned_roots(conn) if Path(p).is_dir()]
    finally:
        conn.close()
    if folders:
        try:
            _start_index(folders)
        except HTTPException:
            return []
    return [str(f) for f in folders]


def auto_access_startup() -> list[str]:
    """Called at startup: gives the app access to Documents, Desktop and Downloads on first launch
    (and to any on-by-default folder added in a later version), so nothing has to be picked by hand.
    A folder the user removed is not added again: each is offered once."""
    keys = known_folders.startup_keys()
    if not keys:
        return []
    conn = _conn()
    try:
        added = known_folders.add(keys, conn)
    finally:
        conn.close()
    if added:
        watcher.refresh()
        try:
            _start_index(added)
        except HTTPException:
            return []  # a scan is already running; these show as NOT SCANNED and rescan_unscanned picks them up next start
    return [str(p) for p in added]


def _existing_dir(folder: str) -> Path:
    path = Path(folder).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {folder}")
    return path.resolve()


@router.get("/roots")
def roots():
    conn = _conn()
    try:
        rows = sqlite_fts.list_roots(conn)
    finally:
        conn.close()
    progress = _snapshot()
    running = progress["folder"] if progress["state"] == "running" else None
    queued = set(progress.get("queue") or [])
    for r in rows:
        r["profile"] = personalize.folder_profile(r["path"])
        if r["path"] == running:
            r["state"] = "scanning"
            r["progress"] = progress
        elif r["path"] in queued:
            r["state"] = "queued"
        elif not Path(r["path"]).is_dir():
            r["state"] = "missing"
        elif r["last_scan"] is None:
            r["state"] = "not_scanned"
        else:
            r["state"] = "up_to_date"
    return {"roots": rows}


class CommonRequest(BaseModel):
    keys: list[str]


class AutoAccessRequest(BaseModel):
    enabled: bool


@router.get("/common")
def common_folders():
    """Documents, Desktop, Downloads and Pictures on this PC, and which of them are indexed."""
    conn = _conn()
    try:
        roots = sqlite_fts.root_paths(conn)
    finally:
        conn.close()
    return {"enabled": known_folders.enabled(), "folders": known_folders.listing(roots)}


@router.post("/common")
def add_common(req: CommonRequest):
    """Adds the chosen everyday folders in one go (no folder picker) and scans them."""
    _no_text_store()
    conn = _conn()
    try:
        added = known_folders.add(req.keys, conn)
    finally:
        conn.close()
    if not added:
        return {"state": "nothing_new", "added": []}
    watcher.refresh()
    _start_index(added)
    return {"state": "started", "added": [str(p) for p in added]}


@router.put("/common/auto")
def set_auto_access(req: AutoAccessRequest):
    """Turns on or off adding the everyday folders automatically at launch."""
    known_folders.set_enabled(req.enabled)
    return {"enabled": known_folders.enabled()}


@router.post("/roots")
def add_root(req: FolderRequest):
    _no_text_store()
    folder = _existing_dir(req.folder)
    conn = _conn()
    try:
        sqlite_fts.add_root(conn, folder)
    finally:
        conn.close()
    watcher.refresh()
    return _start_index([folder])


class ProfileRequest(BaseModel):
    folder: str
    profile: dict


@router.put("/roots/profile")
def save_profile(req: ProfileRequest):
    """Saves a folder's rules, then rescans it so files the new rules exclude leave the index
    and newly allowed ones come in. If a scan is already running the rescan is left to the user."""
    folder = _existing_dir(req.folder)
    try:
        profile = personalize.set_folder_profile(folder, req.profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        _start_index([folder])
        rescan = "started"
    except HTTPException:
        rescan = "busy"
    return {"profile": profile, "rescan": rescan}


@router.post("/roots/remove")
def remove_root(req: FolderRequest):
    """Stops indexing a folder and forgets its entries. The files themselves are not touched."""
    removed = indexer.remove_root(req.folder, settings.db_path, settings.vector_db_dir)
    watcher.refresh()
    return {"removed": removed}


@router.post("/index")
def start_index(req: FolderRequest):
    _no_text_store()
    return _start_index([_existing_dir(req.folder)])


@router.post("/index/cancel")
def cancel_index():
    """Stops the scan after the file it is on. Files done so far stay indexed; the next scan
    carries on from there (unchanged files are skipped quickly)."""
    with _status_lock:
        running = _status["state"] == "running"
    if running:
        indexer.CANCEL.set()
    return {"cancelling": running}


def _snapshot() -> dict:
    with _status_lock:
        current = dict(_status)
    started = current.pop("started", None)
    if current["state"] == "running" and started:
        minutes = max((time.monotonic() - started) / 60, 1e-6)
        current["files_per_min"] = round(current["seen"] / minutes, 1)
    return current


@router.get("/index/status")
def status():
    return {**_snapshot(), "errors": list(indexer.RECENT_ERRORS)}


RECENT_LIMIT, CHANGED_DAYS = 8, 7


@router.get("/recent")
def recent():
    """Files worth surfacing on the Chat home: the ones you opened last, then ones changed in the
    past week. Only files still in the index; opening goes through /files/open, so the
    open-history learning keeps working."""
    conn = _conn()
    try:
        rows, seen = [], set()
        for o in learning.recent_opens(RECENT_LIMIT * 3):
            if o["path"] not in seen and sqlite_fts.is_indexed(conn, o["path"]):
                seen.add(o["path"])
                rows.append({"path": o["path"], "name": Path(o["path"]).name, "reason": "opened", "at": o["at"]})
        for f in sqlite_fts.recent_files(conn, time.time() - CHANGED_DAYS * 86400, RECENT_LIMIT * 3):
            if f["path"] not in seen:
                seen.add(f["path"])
                rows.append({"path": f["path"], "name": Path(f["path"]).name, "reason": "changed", "at": f["mtime"]})
        return {"files": rows[:RECENT_LIMIT]}
    finally:
        conn.close()


@router.get("")
def list_files(root: str | None = None):
    conn = _conn()
    try:
        return {"files": sqlite_fts.list_files(conn, root)}
    finally:
        conn.close()


@router.post("/open")
def open_file(req: PathRequest):
    """Opens a file in its default app: one in the index, or (names-only mode, where chat reads files
    live) one that whole-PC access covers. Anything else is refused, so this can't be used to launch
    arbitrary programs."""
    from app.core import find_file

    known = find_file.allowed_to_open(req.path)
    path = Path(req.path)
    if not known or not path.is_file():
        raise HTTPException(status_code=404, detail="That file isn't in the index (it may have moved).")
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - path is an indexed document, not user-typed
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
    try:
        learning.record_open(str(path), req.query)
    except Exception:
        pass  # learning must never stop a file from opening
    return {"opened": str(path)}


@router.post("/keep")
def keep_file(req: PathRequest):
    """"Keep in index" on a file dropped into chat: until then it was only read for that answer, never stored.
    Refused in names-only mode (the app keeps no file text at all) and for credential-like files.
    ponytail: a kept file outside the indexed folders is a snapshot (the watcher doesn't follow it); drop it again
    after it changes."""
    from app.core import pc_access

    _no_text_store()
    path = Path(req.path)
    ok, why = pc_access.can_read(path, explicit=True)
    if not ok:
        raise HTTPException(status_code=400, detail=why)
    conn = _conn()
    try:
        root = sqlite_fts.root_for(conn, path)
    finally:
        conn.close()
    result = indexer.index_file(path, settings.db_path, settings.vector_db_dir, root=Path(root) if root else path.parent)
    if result in ("failed", "ignored"):
        raise HTTPException(status_code=400, detail=f"{path.name} couldn't be added to the index ({result}). Its type may not be indexable.")
    return {"kept": str(path), "result": result}

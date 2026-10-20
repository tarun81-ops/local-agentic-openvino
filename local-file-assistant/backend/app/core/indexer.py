import fnmatch
import hashlib
import logging
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import settings
from app.core import pc_access, personalize, ram
from app.core.parsers import PARSERS, image_parser
from app.core.search.vector_search import embed
from app.db import sqlite_fts, vector_store

log = logging.getLogger(__name__)

# The watcher thread and a folder scan can index at the same time; one file at a time keeps
# the SQLite and LanceDB rows for a file consistent with each other.
WRITE_LOCK = threading.Lock()

# Set to stop the folder scan in progress after the current file (Index page > STOP).
CANCEL = threading.Event()

# Most recent failures, shown on the Index page so a bad file can actually be diagnosed.
RECENT_ERRORS: deque = deque(maxlen=20)


def is_indexable(path: Path) -> bool:
    return path.suffix.lower() in PARSERS and not path.name.startswith("~$")  # ~$ = Office lock files


def is_allowed(path: Path, root: Path, profile: dict) -> bool:
    """The folder's own rules: a file-type allowlist (empty = all) and exclusion patterns, matched
    against the path inside the folder and against each folder or file name on the way.
    Both the folder scan and the watcher go through here (the watcher via index_file), so they agree."""
    if pc_access.is_sensitive(path):  # credential-like files and folders are never indexed, in any folder
        return False
    exts = profile.get("extensions") or []
    if exts and path.suffix.lower() not in exts:
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    posix = rel.as_posix().lower()
    for g in profile.get("exclude_globs") or []:
        g = g.lower().replace("\\", "/")
        if fnmatch.fnmatch(posix, g) or any(fnmatch.fnmatch(part, g) for part in posix.split("/")):
            return False
    return True


def _caption_override(profile: dict) -> bool | None:
    if profile.get("ocr_only"):
        return False
    return profile.get("caption_images")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _record_error(path: Path, exc: Exception) -> None:
    # The traceback goes in as text, not as a live exc_info: a log record that keeps the exception
    # alive also keeps its frames alive, and a parser that failed half-way can still hold the
    # file open, which on Windows blocks moving or deleting the file afterwards.
    from app.core.assistant.file_agent import unreadable_reason
    from app.core.parsers.pdf_parser import LockedPdf

    # Expected, user-side reasons (a locked PDF, a file on the phone or online-only in OneDrive, a file another
    # program holds): one plain line, no traceback. 195 tracebacks for CrossDevice files buried the real errors.
    expected = isinstance(exc, (LockedPdf, PermissionError, FileNotFoundError)) or "encrypted" in str(exc).lower() or (
        isinstance(exc, OSError) and (exc.errno in (5, 22) or getattr(exc, "winerror", None) in (362, 389, 390, 395, 396)))
    if expected:
        reason = unreadable_reason(Path(path), exc)
        log.warning("skipped %s: %s", path, reason)
    else:
        trace = "" if isinstance(exc, RuntimeError) else "\n" + "".join(traceback.format_exception(exc)).rstrip()
        log.error("failed to index %s: %s%s", path, str(exc), trace)
        reason = f"{type(exc).__name__}: {exc}"
    RECENT_ERRORS.appendleft(
        {
            "path": str(path),
            "error": reason,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )


class _Run:
    """State shared across one indexing pass: DB handles, the lazily opened vector table,
    and whether the embedder is reachable (after one failure the rest of the pass is
    keyword-only instead of waiting on a timeout for every file)."""

    def __init__(self, db_path: Path, vector_db_dir: Path):
        self.conn = sqlite_fts.connect(db_path)
        self.vdb = vector_store.connect(vector_db_dir)
        if sqlite_fts.vectors_stale(self.conn):
            # The keyword index was rebuilt for a new schema; start the vectors over with it.
            with WRITE_LOCK:
                vector_store.drop_table(self.vdb)
                sqlite_fts.clear_vectors_stale(self.conn)
        self.table = vector_store.open_table(self.vdb)
        self.embed_ok = True
        self.low_memory_noted = False
        self.keyword_only = 0  # files indexed without vectors in this pass (embedder down, low RAM)

    def close(self):
        self.conn.close()


def embed_text(path: Path, root: Path, text: str) -> str:
    """What the embedder sees for a chunk: its file name and folder, then the text. "My car
    insurance" can then find a policy whose text says "motor" and never "car". The stored and
    cited chunk text stays the text alone."""
    try:
        folder = path.parent.relative_to(root) if path.parent != root else Path(root.name)
    except ValueError:
        folder = Path(path.parent.name)
    return f"File: {path.name}\nFolder: {folder.as_posix()}\n\n{text}"


def _index_one(path: Path, root: Path, run: _Run, profile: dict | None = None) -> str:
    if not settings.store_file_text:  # every way into the index ends here: names-only stores no text
        return "skipped"
    stat = path.stat()
    # Cheap check first: hashing reads the whole file, which dominates rescans of big folders.
    if sqlite_fts.file_unchanged(run.conn, path, stat.st_size, stat.st_mtime):
        return "skipped"
    file_hash = _hash_file(path)
    if sqlite_fts.file_hash_matches(run.conn, path, file_hash):
        return "skipped"
    token = image_parser.CAPTION_OVERRIDE.set(_caption_override(profile if profile is not None else personalize.folder_profile(root)))
    try:
        chunks = list(enumerate(PARSERS[path.suffix.lower()](path)))
    finally:
        image_parser.CAPTION_OVERRIDE.reset(token)

    vectors = None
    if chunks and run.embed_ok and ram.is_low():
        # Keyword-indexed now; the blank hash below makes the next scan add the vectors.
        if not run.low_memory_noted:
            run.low_memory_noted = True
            free = ram.status()["free_mb"]
            _record_error(path, RuntimeError(f"low memory ({free} MB free): keyword index only for now, vectors added on the next scan"))
    elif chunks and run.embed_ok:
        try:
            vectors = embed([embed_text(path, root, c.text) for _, c in chunks])
        except Exception as exc:
            run.embed_ok = False
            _record_error(path, RuntimeError(f"embedding model unreachable, keyword index only ({exc})"))

    with WRITE_LOCK:
        # The hash goes in last (mark_complete), after the vectors: until then the file counts
        # as unfinished, so a failure or crash anywhere before that is retried on the next scan.
        sqlite_fts.upsert_file(run.conn, path, root, stat.st_size, stat.st_mtime, chunks)
        if vectors is not None:
            if run.table is None:
                run.table = vector_store.get_or_create_table(run.vdb, dim=len(vectors[0]))
            rows = [
                {
                    "path": str(path),
                    "root": str(root),
                    "loc_kind": c.loc_kind,
                    "loc_no": c.loc_no,
                    "chunk_no": n,
                    "text": c.text,
                    "vector": v,
                }
                for (n, c), v in zip(chunks, vectors, strict=True)
            ]
            vector_store.upsert(run.table, str(path), rows)
        elif run.table is not None:
            vector_store.delete_path(run.table, str(path))
        if vectors is not None or not chunks:
            sqlite_fts.mark_complete(run.conn, path, file_hash)
        else:
            run.keyword_only += 1  # searchable by keyword; vectors on the next scan
    return "indexed"


def _forget(path: str, run: _Run) -> None:
    with WRITE_LOCK:
        sqlite_fts.delete_file(run.conn, path)
        if run.table is not None:
            vector_store.delete_path(run.table, path)


def index_folder(
    folder: Path,
    db_path: Path,
    vector_db_dir: Path,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Indexes new/changed files under folder and removes index entries for files that
    no longer exist there. Does nothing in names-only mode (settings.store_file_text off)."""
    counts = {"seen": 0, "indexed": 0, "skipped": 0, "failed": 0, "removed": 0, "keyword_only": 0}
    if not settings.store_file_text:
        return counts
    folder = folder.resolve()
    run = _Run(db_path, vector_db_dir)
    cancelled = False
    try:
        sqlite_fts.add_root(run.conn, folder)
        present: set[str] = set()
        profile = personalize.folder_profile(folder)
        for path in folder.rglob("*"):
            if CANCEL.is_set():
                cancelled = True
                break
            if not is_indexable(path) or not path.is_file() or not is_allowed(path, folder, profile):
                continue
            present.add(str(path))
            counts["seen"] += 1
            try:
                status = _index_one(path, folder, run, profile)
            except Exception as exc:
                _record_error(path, exc)
                status = "failed"
            counts[status] += 1
            counts["keyword_only"] = run.keyword_only
            if on_progress:
                on_progress(dict(counts))

        if not cancelled:
            # Only a complete walk knows which files are really gone (including ones the folder's
            # rules now exclude: they are simply not in `present`).
            for stale in set(sqlite_fts.indexed_paths(run.conn, folder)) - present:
                _forget(stale, run)
                counts["removed"] += 1
            sqlite_fts.mark_scanned(run.conn, folder)
    finally:
        run.close()
    counts["semantic"] = run.embed_ok and run.keyword_only == 0  # every file got its vectors
    counts["cancelled"] = cancelled
    return counts


def index_file(path: Path, db_path: Path, vector_db_dir: Path, root: Path | None = None) -> str:
    """Indexes one file (used by the folder watcher). root defaults to the indexed folder
    that contains the file. Does nothing in names-only mode."""
    if not settings.store_file_text:
        return "ignored"
    run = _Run(db_path, vector_db_dir)
    try:
        if root is None:
            found = sqlite_fts.root_for(run.conn, path)
            if found is None:
                return "ignored"
            root = Path(found)
        profile = personalize.folder_profile(root)
        if not is_allowed(path, root, profile):
            _forget(str(path), run)  # it was indexed before the rules excluded it
            return "ignored"
        return _index_one(path, root, run, profile)
    except Exception as exc:
        _record_error(path, exc)
        return "failed"
    finally:
        run.close()


def remove_file(path: Path | str, db_path: Path, vector_db_dir: Path) -> None:
    """Drops a deleted or moved-away file from both indexes."""
    run = _Run(db_path, vector_db_dir)
    try:
        _forget(str(path), run)
    finally:
        run.close()


def remove_root(root: str, db_path: Path, vector_db_dir: Path) -> int:
    """Stops indexing a folder and forgets its entries. Files on disk are untouched."""
    run = _Run(db_path, vector_db_dir)
    try:
        with WRITE_LOCK:
            paths = sqlite_fts.remove_root(run.conn, root)
            if run.table is not None:
                for p in paths:
                    vector_store.delete_path(run.table, p)
        return len(paths)
    finally:
        run.close()

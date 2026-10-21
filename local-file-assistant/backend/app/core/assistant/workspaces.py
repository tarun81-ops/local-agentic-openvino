"""Workspaces (assistant.db): a name, the folders chat answers from, and an answer style. Chat search is limited to
the folders (routes_chat passes them down as `within`); a workspace with no folders covers the whole PC. A workspace
only narrows what the app may already read: PC access and the never-opened rules still apply inside it."""
import json
import os
import sqlite3
import time
from pathlib import Path

from app.core import personalize
from app.db import assistant_db


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "roots": json.loads(r["roots_json"]), "answer_style": r["answer_style"],
            "created_at": r["created_at"], "pinned": bool(r["pinned"])}


def clean(name: str, roots: list[str], answer_style: str) -> tuple[str, list[str], str]:
    """Validated fields, or ValueError with a message the user can act on."""
    name = " ".join((name or "").split())[:60]
    if not name:
        raise ValueError("Give the workspace a name.")
    if answer_style not in ("", *personalize.STYLES):
        raise ValueError(f"Answer style must be one of: {', '.join(personalize.STYLES)}.")
    out: list[str] = []
    for r in roots or []:
        p = Path(r)
        if not p.is_absolute() or not p.is_dir():
            raise ValueError(f"{r} isn't a folder on this PC. Pick it again with Add folder.")
        if os.path.normcase(str(p)) not in map(os.path.normcase, out):
            out.append(str(p))
    return name, out, answer_style


def _check_free(conn, name: str, ws_id: int | None = None) -> None:
    """Names are unique ignoring case (migration 0012). casefold() here also covers non-ASCII letters, which SQLite's
    lower() in that index doesn't fold."""
    if any(r["name"].casefold() == name.casefold() for r in conn.execute("SELECT id, name FROM workspaces") if r["id"] != ws_id):
        raise ValueError(f"There is already a workspace called “{name}”. Pick another name.")


def create(name: str, roots: list[str], answer_style: str = "") -> dict:
    name, roots, answer_style = clean(name, roots, answer_style)
    conn = assistant_db.connect()
    try:
        _check_free(conn, name)
        cur = conn.execute("INSERT INTO workspaces(name, roots_json, answer_style, created_at) VALUES (?, ?, ?, ?)",
                           (name, json.dumps(roots), answer_style, time.time()))
        conn.commit()
        return get(cur.lastrowid)
    except sqlite3.IntegrityError:  # saved by another request at the same moment
        raise ValueError(f"There is already a workspace called “{name}”. Pick another name.") from None
    finally:
        conn.close()


def list_all() -> list[dict]:
    conn = assistant_db.connect()
    try:
        return [_row(r) for r in conn.execute("SELECT * FROM workspaces ORDER BY pinned DESC, name COLLATE NOCASE")]
    finally:
        conn.close()


def get(ws_id: int | None) -> dict | None:
    if ws_id is None:
        return None
    conn = assistant_db.connect()
    try:
        r = conn.execute("SELECT * FROM workspaces WHERE id = ?", (ws_id,)).fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def update(ws_id: int, name: str, roots: list[str], answer_style: str = "") -> dict | None:
    name, roots, answer_style = clean(name, roots, answer_style)
    conn = assistant_db.connect()
    try:
        _check_free(conn, name, ws_id)
        conn.execute("UPDATE workspaces SET name = ?, roots_json = ?, answer_style = ? WHERE id = ?",
                     (name, json.dumps(roots), answer_style, ws_id))
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"There is already a workspace called “{name}”. Pick another name.") from None
    finally:
        conn.close()
    return get(ws_id)


def delete(ws_id: int) -> None:
    """Its chats move to "All files" (ON DELETE SET NULL). Files on disk are never touched."""
    conn = assistant_db.connect()
    try:
        conn.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        conn.commit()
    finally:
        conn.close()

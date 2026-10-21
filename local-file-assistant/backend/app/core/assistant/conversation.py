"""Conversation storage (assistant.db) and what of a conversation the model gets to see."""
import json
import time

from app.config import settings
from app.core import personalize
from app.db import assistant_db


def _msg(r) -> dict:
    return {"id": r["id"], "role": r["role"], "content": r["content"], "meta": json.loads(r["meta_json"]), "created_at": r["created_at"]}


def title_from(message: str) -> str:
    one = " ".join(message.split())
    return one if len(one) <= 60 else one[:57] + "…"


def create(title: str = "", created_at: float | None = None, workspace_id: int | None = None) -> dict:
    now = created_at or time.time()
    conn = assistant_db.connect()
    try:
        cur = conn.execute("INSERT INTO conversations(title, created_at, updated_at, workspace_id) VALUES (?, ?, ?, ?)", (title, now, now, workspace_id))
        conn.commit()
        return {"id": cur.lastrowid, "title": title, "workspace_id": workspace_id}
    finally:
        conn.close()


def list_all(workspace_id: int | None = None) -> list[dict]:
    """The chats of one workspace; None = the "All files" chats (those in no workspace)."""
    conn = assistant_db.connect()
    try:
        rows = conn.execute("SELECT id, title, updated_at, pinned, workspace_id FROM conversations WHERE workspace_id IS ? "
                            "ORDER BY pinned DESC, updated_at DESC", (workspace_id,)).fetchall()
        return [{**dict(r), "pinned": bool(r["pinned"])} for r in rows]
    finally:
        conn.close()


def get(conv_id: int) -> dict | None:
    conn = assistant_db.connect()
    try:
        c = conn.execute("SELECT id, title, updated_at, pinned, workspace_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if c is None:
            return None
        rows = conn.execute("SELECT * FROM messages WHERE conv_id = ? ORDER BY id", (conv_id,)).fetchall()
        ratings = {r["msg_id"]: r["rating"] for r in conn.execute("SELECT f.msg_id, f.rating FROM feedback f JOIN messages m ON m.id = f.msg_id WHERE m.conv_id = ?", (conv_id,))}
        return {**dict(c), "pinned": bool(c["pinned"]), "messages": [{**_msg(r), "rating": ratings.get(r["id"])} for r in rows]}
    finally:
        conn.close()


def update(conv_id: int, title: str | None = None, pinned: bool | None = None) -> bool:
    conn = assistant_db.connect()
    try:
        if title is not None:
            conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
        if pinned is not None:
            conn.execute("UPDATE conversations SET pinned = ? WHERE id = ?", (int(pinned), conv_id))
        conn.commit()
        return conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone() is not None
    finally:
        conn.close()


def delete(conv_id: int) -> None:
    conn = assistant_db.connect()
    try:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
    finally:
        conn.close()


def add_messages(conv_id: int, messages: list[tuple[str, str, dict]], at: float | None = None) -> list[int]:
    """messages: (role, content, meta). Returns their ids and bumps the conversation's updated_at."""
    now = at or time.time()
    conn = assistant_db.connect()
    try:
        ids = [
            conn.execute("INSERT INTO messages(conv_id, role, content, meta_json, created_at) VALUES (?, ?, ?, ?, ?)", (conv_id, role, content, json.dumps(meta), now)).lastrowid
            for role, content, meta in messages
        ]
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        conn.commit()
        return ids
    finally:
        conn.close()


def window(messages: list[dict], turns: int | None = None, token_budget: int | None = None) -> list[dict]:
    """The last `turns` exchanges as {role, content}, dropping the oldest until they fit the
    token budget. A small model needs room left for the excerpts, so this stays short."""
    turns = personalize.effective("history_turns") if turns is None else turns
    budget = (settings.history_token_budget if token_budget is None else token_budget) * 4  # ~4 chars/token
    kept = [{"role": m["role"], "content": m["content"]} for m in messages[-turns * 2 :]]
    while kept and sum(len(m["content"]) for m in kept) > budget:
        kept.pop(0)
    while kept and kept[0]["role"] != "user":  # never start mid-exchange
        kept.pop(0)
    return kept


def import_chats(chats: list[dict]) -> int:
    """One-time import of the browser's old localStorage chats ({title, at, turns:[{q, answer, ...}]})."""
    n = 0
    for c in sorted(chats, key=lambda c: c.get("at", 0)):
        turns = [t for t in c.get("turns", []) if t.get("q") and t.get("answer")]
        if not turns:
            continue
        at = c.get("at", 0) / 1000 or time.time()
        conv = create(c.get("title") or title_from(turns[0]["q"]), at)
        msgs: list[tuple[str, str, dict]] = []
        for t in turns:
            meta = {"route": "files", **{k: t[k] for k in ("citations", "uncited", "truncated") if k in t}}
            msgs += [("user", t["q"], {}), ("assistant", t["answer"], meta)]
        add_messages(conv["id"], msgs, at)
        n += 1
    return n

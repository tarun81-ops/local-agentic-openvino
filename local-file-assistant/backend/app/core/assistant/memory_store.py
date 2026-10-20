"""Long-term memory: short facts about the user, in assistant.db. Recall = keyword (FTS5) plus
cosine similarity over stored embeddings (numpy; a few hundred facts need no vector index)."""
import logging
import re
import time

import numpy as np

from app.config import settings
from app.core.search import vector_search
from app.db import assistant_db
from app.core.words import words

log = logging.getLogger(__name__)

DUPLICATE_COSINE = 0.92
MIN_COSINE = 0.35  # below this a semantic hit is noise
EMBED_TIMEOUT = 5  # chat must not stall for long when the embedder is down
_STOP = {"the", "and", "for", "what", "which", "who", "how", "you", "your", "are", "was", "were", "with", "that", "this", "have", "has", "does", "did", "can", "about", "from", "into", "when", "where", "why", "tell", "please"}
_COLS = "id, text, kind, pinned, created_at, last_used_at, use_count, status"


def _embed(texts: list[str], query: bool = False) -> list[bytes | None]:
    try:
        return [np.asarray(v, dtype=np.float32).tobytes() for v in (vector_search.embed_query if query else vector_search.embed)(texts, timeout=EMBED_TIMEOUT)]
    except Exception as exc:  # no embedder: keyword recall still works, vectors are filled in later
        log.warning("memory embedding skipped: %s", exc)
        return [None] * len(texts)


def _norm(blob: bytes) -> np.ndarray:
    v = np.frombuffer(blob, dtype=np.float32)
    return v / (np.linalg.norm(v) or 1.0)


def _row(r) -> dict:
    return {**{k: r[k] for k in r.keys() if k in ("id", "text", "kind", "created_at", "last_used_at", "use_count", "status")}, "pinned": bool(r["pinned"])}


def add(text: str, kind: str = "fact", source_conv: int | None = None, source_msg: int | None = None, pinned: bool = False, status: str = "active") -> dict | None:
    """Stores a fact; returns None if the same fact is already remembered (active or waiting for review).
    Facts come from the user saying "remember ..." (status "active"). "pending" is only left by older
    versions, which guessed facts from chats; drop_pending() clears those at startup."""
    text = " ".join(text.split())
    if not text:
        return None
    [blob] = _embed([text])
    conn = assistant_db.connect()
    try:
        for r in conn.execute("SELECT text, embedding, embed_model FROM memories"):
            if r["text"].lower() == text.lower():
                return None
            if blob and r["embedding"] and r["embed_model"] == settings.embedding_model and float(_norm(blob) @ _norm(r["embedding"])) >= DUPLICATE_COSINE:
                return None
        cur = conn.execute(
            "INSERT INTO memories(text, kind, source_conv, source_msg, created_at, pinned, embedding, embed_model, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, kind, source_conv, source_msg, time.time(), int(pinned), blob, settings.embedding_model if blob else None, status),
        )
        conn.commit()
        return {"id": cur.lastrowid, "text": text, "kind": kind, "pinned": pinned, "status": status}
    finally:
        conn.close()


def list_all(q: str = "", status: str | None = None) -> list[dict]:
    """status="active" / "pending" filters; None returns both."""
    conn = assistant_db.connect()
    try:
        where, args = ("WHERE status = ?", (status,)) if status else ("", ())
        rows = conn.execute(f"SELECT {_COLS} FROM memories {where} ORDER BY pinned DESC, created_at DESC", args).fetchall()
        out = [_row(r) for r in rows]
        return [m for m in out if q.lower() in m["text"].lower()] if q else out
    finally:
        conn.close()


def update(mem_id: int, text: str | None = None, pinned: bool | None = None, kind: str | None = None) -> bool:
    text = " ".join(text.split()) if text else None
    blob = _embed([text])[0] if text else None
    conn = assistant_db.connect()
    try:
        if text:
            conn.execute("UPDATE memories SET text = ?, embedding = ?, embed_model = ? WHERE id = ?", (text, blob, settings.embedding_model if blob else None, mem_id))
        if pinned is not None:
            conn.execute("UPDATE memories SET pinned = ? WHERE id = ?", (int(pinned), mem_id))
        if kind:
            conn.execute("UPDATE memories SET kind = ? WHERE id = ?", (kind, mem_id))
        conn.commit()
        return conn.execute("SELECT 1 FROM memories WHERE id = ?", (mem_id,)).fetchone() is not None
    finally:
        conn.close()


def drop_pending() -> int:
    """Deletes facts still waiting for review. They came from the removed background extraction,
    were never used in answers, and there is no longer a place to review them."""
    conn = assistant_db.connect()
    try:
        n = conn.execute("DELETE FROM memories WHERE status = 'pending'").rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def counts() -> dict:
    conn = assistant_db.connect()
    try:
        got = dict(conn.execute("SELECT status, COUNT(*) FROM memories GROUP BY status").fetchall())
        return {"active": got.get("active", 0), "pending": got.get("pending", 0)}
    finally:
        conn.close()


def delete(mem_id: int) -> None:
    conn = assistant_db.connect()
    try:
        conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        conn.commit()
    finally:
        conn.close()


def wipe() -> int:
    conn = assistant_db.connect()
    try:
        n = conn.execute("DELETE FROM memories").rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _refresh_embeddings(conn) -> None:
    """Embeds facts that have no vector yet or were embedded with a different model."""
    rows = conn.execute("SELECT id, text FROM memories WHERE embedding IS NULL OR embed_model IS NOT ? LIMIT 100", (settings.embedding_model,)).fetchall()
    if not rows:
        return
    blobs = _embed([r["text"] for r in rows])
    for r, blob in zip(rows, blobs):
        if blob:
            conn.execute("UPDATE memories SET embedding = ?, embed_model = ? WHERE id = ?", (blob, settings.embedding_model, r["id"]))
    conn.commit()


def recall(query: str, k: int | None = None, touch: bool = True) -> list[dict]:
    """The k active facts most relevant to `query`, best first. Facts awaiting review are never used."""
    k = settings.memory_top_k if k is None else k
    if k <= 0 or not query.strip():
        return []
    conn = assistant_db.connect()
    try:
        _refresh_embeddings(conn)
        scores: dict[int, float] = {}
        terms = [t for t in words(query.lower()) if len(t) > 2 and t not in _STOP]  # Hindi words stay whole
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            for rank, r in enumerate(conn.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? AND rowid IN (SELECT id FROM memories WHERE status = 'active') ORDER BY rank LIMIT 20", (match,))):
                scores[r[0]] = scores.get(r[0], 0) + 1 / (60 + rank + 1)
        [qblob] = _embed([query], query=True)
        if qblob:
            q = _norm(qblob)
            sims = []
            for r in conn.execute("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL AND embed_model = ? AND status = 'active'", (settings.embedding_model,)):
                s = float(q @ _norm(r["embedding"]))
                if s >= MIN_COSINE:
                    sims.append((s, r["id"]))
            for rank, (_, mid) in enumerate(sorted(sims, reverse=True)[:20]):
                scores[mid] = scores.get(mid, 0) + 1 / (60 + rank + 1)
        ids = sorted(scores, key=scores.get, reverse=True)[:k]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        found = {r["id"]: _row(r) for r in conn.execute(f"SELECT {_COLS} FROM memories WHERE id IN ({marks})", ids)}
        if touch:
            conn.execute(f"UPDATE memories SET use_count = use_count + 1, last_used_at = ? WHERE id IN ({marks})", [time.time(), *ids])
            conn.commit()
        return [found[i] for i in ids]
    finally:
        conn.close()


def forget(text: str) -> str | None:
    """Deletes the single fact that best matches `text`; returns what it was, or None."""
    hit = recall(text, k=1, touch=False)
    if not hit:
        return None
    delete(hit[0]["id"])
    return hit[0]["text"]

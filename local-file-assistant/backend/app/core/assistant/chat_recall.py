"""RAG over the user's past conversations.

Each finished exchange (question + answer) becomes one passage in `chat_index` (assistant.db). A
new message is matched against those passages with the same recipe as memory recall: keyword
(FTS5) plus cosine similarity over embeddings (numpy; thousands of exchanges need no vector
index), fused with reciprocal rank. The best few are put in the prompt as quoted data.

The index is derived from `messages`: `sync()` adds missing exchanges, and rows disappear with
their message or conversation (foreign-key cascade), so deleting a chat also forgets it here.
Embeddings are filled in by a background thread, never while the user waits for an answer."""
import logging
import re
import threading

import numpy as np

from app.config import settings
from app.core import ram
from app.core.search import vector_search
from app.db import assistant_db
from app.core.words import words

log = logging.getLogger(__name__)

RRF_K = 60
EMBED_TIMEOUT = 5  # the answer must not stall for long when the embedder is down
EMBED_BATCH = 32
Q_CHARS, A_CHARS = 400, 800  # what of an exchange is indexed: enough to find it, short enough to quote
SKIP_ROUTES = ("memory", "action", "task")  # commands ("remember ...", "remind me ...") are not conversation
_STOP = {"the", "and", "for", "what", "which", "who", "how", "you", "your", "are", "was", "were", "with", "that", "this", "have", "has", "does", "did", "can", "about", "from", "into", "when", "where", "why", "tell", "please", "remind", "again"}

_embed_lock = threading.Lock()


def _embed(texts: list[str], query: bool = False) -> list[bytes | None]:
    try:
        return [np.asarray(v, dtype=np.float32).tobytes() for v in (vector_search.embed_query if query else vector_search.embed)(texts, timeout=EMBED_TIMEOUT)]
    except Exception as exc:  # no embedder: keyword recall still works, vectors are filled in later
        log.warning("chat recall embedding skipped: %s", exc)
        return [None] * len(texts)


def _unit(blob: bytes) -> np.ndarray:
    v = np.frombuffer(blob, dtype=np.float32)
    return v / (np.linalg.norm(v) or 1.0)


def exchange_text(question: str, answer: str) -> str:
    q, a = " ".join(question.split())[:Q_CHARS], " ".join(answer.split())[:A_CHARS]
    return f"User: {q}\nAssistant: {a}" if q else f"Assistant: {a}"


def sync(conn) -> int:
    """Adds an index row for every finished exchange that has none yet; returns how many."""
    skip = ",".join("?" * len(SKIP_ROUTES))
    rows = conn.execute(
        f"""SELECT m.id, m.conv_id, m.content, m.created_at,
                   COALESCE((SELECT u.content FROM messages u WHERE u.conv_id = m.conv_id AND u.role = 'user' AND u.id < m.id
                             ORDER BY u.id DESC LIMIT 1), '') AS question
            FROM messages m LEFT JOIN chat_index c ON c.msg_id = m.id
            WHERE m.role = 'assistant' AND c.msg_id IS NULL AND TRIM(m.content) != ''
              AND COALESCE(json_extract(m.meta_json, '$.route'), '') NOT IN ({skip})
            ORDER BY m.id LIMIT 500""",
        SKIP_ROUTES,
    ).fetchall()
    for r in rows:
        conn.execute("INSERT INTO chat_index(msg_id, conv_id, text, created_at) VALUES (?, ?, ?, ?)", (r["id"], r["conv_id"], exchange_text(r["question"], r["content"]), r["created_at"]))
    conn.commit()
    return len(rows)


def embed_pending(limit: int = EMBED_BATCH) -> int:
    """Syncs, then embeds up to `limit` exchanges that have no vector (or one from another model)."""
    conn = assistant_db.connect()
    try:
        sync(conn)
        rows = conn.execute("SELECT msg_id, text FROM chat_index WHERE embedding IS NULL OR embed_model IS NOT ? ORDER BY msg_id DESC LIMIT ?", (settings.embedding_model, limit)).fetchall()
        if not rows:
            return 0
        done = 0
        for r, blob in zip(rows, _embed([r["text"] for r in rows])):
            if blob:
                conn.execute("UPDATE chat_index SET embedding = ?, embed_model = ? WHERE msg_id = ?", (blob, settings.embedding_model, r["msg_id"]))
                done += 1
        conn.commit()
        return done
    finally:
        conn.close()


def schedule_embed() -> None:
    """Fills in embeddings in the background (one worker at a time; skipped when RAM is low)."""
    if not _embed_lock.acquire(blocking=False):
        return

    def work():
        try:
            if not ram.status()["low"]:
                embed_pending()
        except Exception:
            log.exception("chat recall indexing failed")
        finally:
            _embed_lock.release()

    threading.Thread(target=work, name="chat-recall-embed", daemon=True).start()


def recall(query: str, exclude_conv: int | None = None, k: int | None = None) -> list[dict]:
    """The k past exchanges most relevant to `query`, best first, never from `exclude_conv`
    (the current chat is already in the history window). With an embedder, a hit must be
    semantically close (cosine >= chat_recall_min_cosine); keywords then only re-rank, because
    keyword search alone matches almost any sentence. Without one, keyword hits are used."""
    k = settings.chat_recall_k if k is None else k
    if k <= 0 or not query.strip():
        return []
    skip_conv = -1 if exclude_conv is None else exclude_conv
    conn = assistant_db.connect()
    try:
        sync(conn)
        fts_rank: dict[int, int] = {}
        terms = [t for t in words(query.lower()) if len(t) > 2 and t not in _STOP]  # Hindi words stay whole
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = conn.execute("SELECT rowid FROM chat_index_fts WHERE chat_index_fts MATCH ? AND rowid IN (SELECT msg_id FROM chat_index WHERE conv_id != ?) ORDER BY rank LIMIT 20", (match, skip_conv))
            fts_rank = {r[0]: i for i, r in enumerate(rows)}
        [qblob] = _embed([query], query=True)
        vec_rank: dict[int, int] = {}
        if qblob:
            rows = conn.execute("SELECT msg_id, embedding FROM chat_index WHERE embedding IS NOT NULL AND embed_model = ? AND conv_id != ?", (settings.embedding_model, skip_conv)).fetchall()
            if rows:
                sims = np.vstack([_unit(r["embedding"]) for r in rows]) @ _unit(qblob)
                order = [i for i in np.argsort(-sims) if sims[i] >= settings.chat_recall_min_cosine][:20]
                vec_rank = {rows[i]["msg_id"]: n for n, i in enumerate(order)}
        scores: dict[int, float] = {}
        # With vectors, only semantically close exchanges qualify; keywords add to their score.
        for mid in vec_rank if qblob else fts_rank:
            scores[mid] = sum(1 / (RRF_K + r[mid] + 1) for r in (vec_rank, fts_rank) if mid in r)
        ids = sorted(scores, key=scores.get, reverse=True)[:k]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        found = {
            r["msg_id"]: {"msg_id": r["msg_id"], "conv_id": r["conv_id"], "title": r["title"], "text": r["text"], "created_at": r["created_at"]}
            for r in conn.execute(f"SELECT c.msg_id, c.conv_id, c.text, c.created_at, v.title FROM chat_index c JOIN conversations v ON v.id = c.conv_id WHERE c.msg_id IN ({marks})", ids)
        }
        return [found[i] for i in ids if i in found]
    finally:
        conn.close()

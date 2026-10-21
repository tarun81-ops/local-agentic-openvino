import logging
from pathlib import Path

from app.config import settings
from app.core.assistant import learning
from app.core.search import fts_search, reranker, vector_search
from app.db import sqlite_fts, vector_store

log = logging.getLogger(__name__)

SNIPPET_CHARS = 240
RERANK_POOL = 20  # candidates the cross-encoder re-reads; its cost grows with this


def merge(fts_results: list[dict], vector_results: list[dict], k: int = 60, limit: int = 6) -> list[dict]:
    """Reciprocal Rank Fusion: combines two ranked lists by rank position only, so bm25 and
    vector distances never need to be normalised against each other. Each result is tagged
    with how it matched: "keyword", "semantic" or "both"."""
    scores: dict[tuple[str, int], float] = {}
    rows: dict[tuple[str, int], dict] = {}
    seen_in: dict[tuple[str, int], set[str]] = {}
    for kind, results in (("keyword", fts_results), ("semantic", vector_results)):
        for rank, r in enumerate(results):
            key = (r["path"], r["chunk_no"])
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank + 1)
            rows.setdefault(key, r)
            seen_in.setdefault(key, set()).add(kind)
    ranked = sorted(scores, key=scores.get, reverse=True)[:limit]
    out = []
    for key in ranked:
        kinds = seen_in[key]
        out.append({**rows[key], "match": "both" if len(kinds) == 2 else next(iter(kinds))})
    return out


def snippet(text: str, query_terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """A window of the chunk around the first query term, instead of the whole chunk."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    lower = flat.lower()
    hits = [i for i in (lower.find(t) for t in query_terms) if i >= 0]
    centre = min(hits) if hits else 0
    start = max(0, centre - width // 3)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    piece = flat[start:end]
    if start > 0 and " " in piece:
        piece = "…" + piece[piece.find(" ") + 1 :]
    if end < len(flat) and " " in piece:
        piece = piece[: piece.rfind(" ")] + "…"
    return piece


def _apply_open_prior(q: str, rows: list[dict]) -> list[dict]:
    """Moves files the user habitually opens for similar searches up a few places (bounded by
    open_prior_max_shift). Learning is a bonus: any failure leaves the ranking untouched."""
    try:
        boosts = learning.open_prior(q, {r["path"] for r in rows})
    except Exception as exc:
        log.warning("open-history ranking skipped: %s", exc)
        return rows
    if not boosts:
        return rows
    order = sorted(range(len(rows)), key=lambda i: i - boosts.get(rows[i]["path"], 0.0) - (1e-6 if rows[i]["path"] in boosts else 0.0))  # a boost wins ties
    return [rows[i] for i in order]


def _hidden_roots_when_off(conn) -> list[str]:
    """Whole-PC content roots to leave out of results while PC access is switched off (kept on disk, never returned)."""
    from app.core import pc_access

    return [] if pc_access.enabled() else sqlite_fts.hidden_roots(conn)


def _drop_hidden(rows: list[dict], skip: list[str]) -> list[dict]:
    """Same exclusion for vector hits (done before the final cut, so they can't use up the result slots)."""
    if not skip or not rows:
        return rows
    conn = sqlite_fts.connect(settings.db_path)
    try:
        return [r for r in rows if sqlite_fts.file_root(conn, r["path"]) not in set(skip)]
    finally:
        conn.close()


def hybrid_search(q: str, root: str | None = None, limit: int = 6, within: list[str] | None = None) -> dict:
    """Keyword + semantic search, then (when a model is installed) a cross-encoder re-ranks the
    top RERANK_POOL. Keyword search always runs; semantic search and re-ranking are skipped
    (not fatal) when their model is missing, down or slow."""
    rerank_on = reranker.available()
    # A wider pool lets a habitually opened file rise into view even when the re-ranker is off.
    pool = max(limit, RERANK_POOL) if (rerank_on or settings.open_prior_max_shift > 0) else limit
    # within: a workspace's folders. Both searches filter them before their top hits are cut (a small workspace in a
    # big index would otherwise get no hits and FILES ONLY would refuse): keyword search in SQL, vector search with
    # one deeper retry (vector_search.search).
    conn = sqlite_fts.connect(settings.db_path)
    try:
        skip = _hidden_roots_when_off(conn)
        fts_results = fts_search.search(conn, q, limit=pool + 2, root=root, skip_roots=skip or None, within=within)
        stale = sqlite_fts.vectors_stale(conn)  # schema upgrade pending a rebuild
    finally:
        conn.close()

    vector_results: list[dict] = []
    semantic_ok = False
    try:
        # Names-only mode stores no vectors, so there is nothing to search: skip it, and with it the
        # embedding-model call (seconds on CPU) that every question would otherwise pay for nothing.
        table = None if (stale or not settings.store_file_text) else vector_store.open_table(vector_store.connect(settings.vector_db_dir))
        if table is not None:
            vector_results = vector_search.search(table, q, limit=pool + 2, root=root, within=within)
            semantic_ok = True
    except Exception as exc:  # embedder unreachable/timeout, or a LanceDB error
        log.warning("semantic search skipped: %s", exc)

    if vector_results:
        merged = merge(fts_results, vector_results, limit=pool)
    else:
        merged = [{**r, "match": "keyword"} for r in fts_results[:pool]]

    reranked = False
    if rerank_on and len(merged) > 1:
        try:
            merged = reranker.rerank(q, merged[:RERANK_POOL]) + merged[RERANK_POOL:]
            reranked = True
        except Exception as exc:  # a broken model file must not break search
            log.warning("re-ranking skipped: %s", exc)
    merged = _apply_open_prior(q, _drop_hidden(merged, skip))[:limit]

    query_terms = fts_search.terms(q)
    for r in merged:
        r["name"] = Path(r["path"]).name
        r["snippet"] = snippet(r["text"], query_terms)
    return {"results": merged, "terms": query_terms, "semantic": semantic_ok, "reranked": reranked}

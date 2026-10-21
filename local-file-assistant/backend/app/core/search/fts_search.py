import os
import re
import sqlite3
from pathlib import Path

# Question words and glue. Requiring them to appear (the old behaviour: every word ANDed)
# made natural questions like "What is the invoice total for Acme Corp?" match nothing.
STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if in
    into is it its me my of on or our should so than that the their them then there these they
    this to was we were what when where which who whom why will with would you your about any
    tell show find give please""".split()
)

from app.core.words import WORD as _TOKEN_RE  # keeps Hindi words whole, like the index tokenizer


def terms(query: str) -> list[str]:
    """The words worth matching, lowercased and de-duplicated in order."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(query)]
    kept = [t for t in tokens if t not in STOPWORDS and len(t) > 1] or tokens
    return list(dict.fromkeys(kept))


def build_match(query: str) -> str | None:
    """An FTS5 MATCH expression: every term quoted (so FTS5 operators and stray quotes in user
    input are plain text) and ORed, letting bm25 rank chunks that contain more of them first."""
    ts = terms(query)
    if not ts:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in ts)


def search(conn: sqlite3.Connection, query: str, limit: int = 8, root: str | None = None, skip_roots: list[str] | None = None,
           within: list[str] | None = None) -> list[dict]:
    """within: a workspace's folders. Filtered here, before the top `limit` are taken, so a workspace holding a small
    share of the index still gets its own hits (filtering the top hits afterwards could leave none)."""
    match = build_match(query)
    if match is None:
        return []
    sql = (
        "SELECT c.path, c.loc_kind, c.loc_no, c.chunk_no, c.text, bm25(chunks) AS score "
        "FROM chunks c "
    )
    args: list = [match]
    if root or skip_roots:
        sql += "JOIN files f ON f.path = c.path "
    sql += "WHERE chunks MATCH ? "
    if root:
        sql += "AND f.root = ? "
        args.append(root)
    elif skip_roots:  # e.g. whole-PC contents while PC access is off: excluded here so they can't crowd out real hits
        sql += f"AND f.root NOT IN ({','.join('?' * len(skip_roots))}) "
        args += skip_roots
    # ponytail: a Python call per matched row (~2 µs: 0.2 s at 100k matches); a stored folder column if broad queries get slow.
    if within:  # folded in Python like pc_access.under does: SQLite lower() is ASCII-only, so "Ünal" would never match "ünal"
        bases = tuple(os.path.normcase(str(Path(r))).rstrip("\\/") + os.sep for r in within)
        conn.create_function("in_workspace", 1, lambda path: os.path.normcase(path).startswith(bases))
        sql += "AND in_workspace(c.path) "
    sql += "ORDER BY score LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return [
        {
            "path": r[0],
            "loc_kind": r[1],
            "loc_no": int(r[2]),
            "chunk_no": int(r[3]),
            "text": r[4],
            "score": r[5],
        }
        for r in rows
    ]

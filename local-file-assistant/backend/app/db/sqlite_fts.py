import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Bump when the schema changes. An older index is dropped and rebuilt on the next scan,
# which is cheap next to keeping migration code for a single-user local cache.
# 3: vectors now embed the file name and folder too (indexer.embed_text), so the old ones go.
# Porter stemming (tokenize = 'porter unicode61') was measured with eval/run_eval.py --retrieval
# and cost a question without winning one; retry it against real documents before adding it.
# 4: chunk rowids are file id * CHUNKS_PER_FILE + chunk number.
# 5: Hindi words stay whole (TOKENIZE): the default tokenizer split them at every vowel sign.
SCHEMA_VERSION = 5

# unicode61 treats only letters, numbers and private-use characters as part of a word. Devanagari vowel signs
# and the virama (ा ि ी ् ं) are marks (Mn/Mc), so "देवता" was stored as fragments like "द" and "वत" that match
# almost any Hindi text. Counting marks as word characters keeps Hindi words whole; English is unchanged.
TOKENIZE = "unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'"

# A file's chunks are one rowid range. FTS5 reaches a rowid through an index, while
# "WHERE path = ?" reads the whole table (40 ms per file at 60k chunks, measured).
CHUNKS_PER_FILE = 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    path TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    last_scan TEXT
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE INDEX IF NOT EXISTS files_hash ON files(hash);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    path, loc_kind UNINDEXED, loc_no UNINDEXED, chunk_no UNINDEXED, text,
    tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'"
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets the watcher thread and an index run write while searches keep reading.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    reset = conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION
    if reset:
        conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS files;")
    conn.executescript(SCHEMA)
    if not any(r[1] == "hidden" for r in conn.execute("PRAGMA table_info(roots)")):
        # Additive, no rebuild: whole-PC content roots are internal and kept out of the folder lists.
        conn.execute("ALTER TABLE roots ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    if reset:
        with conn:
            # Folders are kept but marked unscanned, so the app rebuilds them on startup, and
            # the old vectors are flagged: they'd otherwise outlive files the rebuild never sees.
            conn.execute("UPDATE roots SET last_scan = NULL")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('vectors_stale', '1')")
        # Last, so a crash part-way through simply repeats the upgrade next time.
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


def vectors_stale(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'vectors_stale'").fetchone()
    return row is not None and row[0] == "1"


def clear_vectors_stale(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM meta WHERE key = 'vectors_stale'")


# ---- files and chunks -------------------------------------------------------------

def file_hash_matches(conn: sqlite3.Connection, path: Path, file_hash: str) -> bool:
    row = conn.execute("SELECT hash FROM files WHERE path = ?", (str(path),)).fetchone()
    return row is not None and row[0] == file_hash


def file_unchanged(conn: sqlite3.Connection, path: Path, size: int, mtime: float) -> bool:
    """Same size and modification time as when it was fully indexed: skip without re-reading.
    A blank hash means the last attempt had no vectors, so that file is always retried."""
    row = conn.execute("SELECT hash, size, mtime FROM files WHERE path = ?", (str(path),)).fetchone()
    return row is not None and row[0] != "" and row[1] == size and abs(row[2] - mtime) < 1e-6


def _chunk_rowids(file_id: int) -> tuple[int, int]:
    return file_id * CHUNKS_PER_FILE, (file_id + 1) * CHUNKS_PER_FILE - 1


def upsert_file(conn, path: Path, root: Path, size: int, mtime: float, chunks) -> None:
    """Replaces a file's chunks. chunks: iterable of (chunk_no, Chunk). The hash is left blank
    ("not finished"): mark_complete sets it once the vectors are stored too."""
    p = str(path)
    chunks = list(chunks)
    if len(chunks) > CHUNKS_PER_FILE:
        raise ValueError(f"{len(chunks)} chunks is more than one file can hold in the index")
    with conn:
        conn.execute(
            "INSERT INTO files (path, root, hash, size, mtime, indexed_at) VALUES (?, ?, '', ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET root=excluded.root, hash='', size=excluded.size, "
            "mtime=excluded.mtime, indexed_at=excluded.indexed_at",
            (p, str(root), size, mtime, _now()),
        )
        first, last = _chunk_rowids(conn.execute("SELECT rowid FROM files WHERE path = ?", (p,)).fetchone()[0])
        conn.execute("DELETE FROM chunks WHERE rowid BETWEEN ? AND ?", (first, last))
        conn.executemany(
            "INSERT INTO chunks (rowid, path, loc_kind, loc_no, chunk_no, text) VALUES (?, ?, ?, ?, ?, ?)",
            [(first + n, p, c.loc_kind, c.loc_no, n, c.text) for n, c in chunks],
        )


def mark_complete(conn: sqlite3.Connection, path: Path | str, file_hash: str) -> None:
    """The file is fully indexed (vectors included), so later scans may skip it while unchanged."""
    with conn:
        conn.execute("UPDATE files SET hash = ? WHERE path = ?", (file_hash, str(path)))


def delete_file(conn: sqlite3.Connection, path: Path | str) -> None:
    with conn:
        row = conn.execute("SELECT rowid FROM files WHERE path = ?", (str(path),)).fetchone()
        if row:
            conn.execute("DELETE FROM chunks WHERE rowid BETWEEN ? AND ?", _chunk_rowids(row[0]))
            conn.execute("DELETE FROM files WHERE path = ?", (str(path),))


def indexed_paths(conn: sqlite3.Connection, root: Path | str | None = None) -> list[str]:
    if root is None:
        return [r[0] for r in conn.execute("SELECT path FROM files")]
    return [r[0] for r in conn.execute("SELECT path FROM files WHERE root = ?", (str(root),))]


def is_indexed(conn: sqlite3.Connection, path: Path | str) -> bool:
    return conn.execute("SELECT 1 FROM files WHERE path = ?", (str(path),)).fetchone() is not None


def file_chunks(conn: sqlite3.Connection, path: Path | str, budget: int = 6000) -> tuple[list[dict], bool]:
    """A file's chunks in reading order, as [{loc_kind, loc_no, text}], within `budget` characters.
    Over budget it takes chunks spread evenly across the file (not just the start) and returns
    sampled=True, so a small model still sees the whole document's range."""
    row = conn.execute("SELECT rowid FROM files WHERE path = ?", (str(path),)).fetchone()
    if row is None:
        return [], False
    first, last = _chunk_rowids(row[0])
    sizes = conn.execute("SELECT rowid, length(text) FROM chunks WHERE rowid BETWEEN ? AND ? ORDER BY rowid", (first, last)).fetchall()
    total = sum(s[1] for s in sizes)
    sampled = total > budget
    if sampled:
        per = max(1, budget // max(1, total // len(sizes)))  # how many average-sized chunks fit
        sizes = [sizes[int(i * len(sizes) / per)] for i in range(min(per, len(sizes)))]
    out, used = [], 0
    for rowid, size in sizes:
        r = conn.execute("SELECT loc_kind, loc_no, text FROM chunks WHERE rowid = ?", (rowid,)).fetchone()
        text = r[2][: max(0, budget - used)]
        if not text:
            break
        used += len(text)
        out.append({"loc_kind": r[0], "loc_no": int(r[1]), "text": text})
    return out, sampled


def first_texts(conn: sqlite3.Connection, root: str, chars: int = 200) -> dict[str, str]:
    """The start of each file's text in a folder, in one query (files with no text are absent)."""
    rows = conn.execute(
        "SELECT f.path, substr(c.text, 1, ?) FROM files f JOIN chunks c ON c.rowid = f.rowid * ? WHERE f.root = ?",
        (chars, CHUNKS_PER_FILE, root),
    )
    return {r[0]: r[1] or "" for r in rows}


def list_files(conn: sqlite3.Connection, root: str | None = None, limit: int = 5000) -> list[dict]:
    sql = "SELECT path, root, hash, size, mtime FROM files"
    args: tuple = ()
    if root:
        sql += " WHERE root = ?"
        args = (root,)
    sql += " ORDER BY path LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (*args, limit))]


def recent_files(conn: sqlite3.Connection, since: float, limit: int = 20) -> list[dict]:
    """Indexed files modified since `since` (epoch seconds), newest first."""
    rows = conn.execute("SELECT path, mtime FROM files WHERE mtime >= ? ORDER BY mtime DESC LIMIT ?", (since, limit))
    return [dict(r) for r in rows]


# ---- roots ------------------------------------------------------------------------

def add_root(conn: sqlite3.Connection, root: Path, hidden: bool = False) -> None:
    """hidden=True: an internal root (whole-PC access). It is never scanned by rglob, never watched,
    and not listed on the Index page; a background worker fills it."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO roots (path, added_at, last_scan) VALUES (?, ?, ?)",
                     (str(root), _now(), _now() if hidden else None))
        if hidden:
            conn.execute("UPDATE roots SET hidden = 1 WHERE path = ?", (str(root),))


def hidden_roots(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT path FROM roots WHERE hidden = 1")]


def file_root(conn: sqlite3.Connection, path: Path | str) -> str | None:
    row = conn.execute("SELECT root FROM files WHERE path = ?", (str(path),)).fetchone()
    return row[0] if row else None


def remove_root(conn: sqlite3.Connection, root: str) -> list[str]:
    """Forgets a folder and its index entries (never touches the files). Returns removed paths."""
    paths = indexed_paths(conn, root)
    with conn:
        for (file_id,) in conn.execute("SELECT rowid FROM files WHERE root = ?", (root,)).fetchall():
            conn.execute("DELETE FROM chunks WHERE rowid BETWEEN ? AND ?", _chunk_rowids(file_id))
        conn.execute("DELETE FROM files WHERE root = ?", (root,))
        conn.execute("DELETE FROM roots WHERE path = ?", (root,))
    return paths


def unscanned_roots(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT path FROM roots WHERE last_scan IS NULL AND hidden = 0 ORDER BY added_at")]


def mark_scanned(conn: sqlite3.Connection, root: Path) -> None:
    with conn:
        conn.execute("UPDATE roots SET last_scan = ? WHERE path = ?", (_now(), str(root)))


def list_roots(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.path, r.added_at, r.last_scan, COUNT(f.path) AS files, COALESCE(SUM(f.size), 0) AS bytes, "
        # A blank hash = indexed for keyword search but still without vectors (see mark_complete).
        "COALESCE(SUM(f.hash = ''), 0) AS keyword_only "
        "FROM roots r LEFT JOIN files f ON f.root = r.path WHERE r.hidden = 0 GROUP BY r.path ORDER BY r.added_at"
    )
    return [dict(r) for r in rows]


def root_paths(conn: sqlite3.Connection, include_hidden: bool = False) -> list[str]:
    """The folders the user added (what is listed and watched). include_hidden adds whole-PC roots."""
    sql = "SELECT path FROM roots" + ("" if include_hidden else " WHERE hidden = 0")
    return [r[0] for r in conn.execute(sql)]


def root_for(conn: sqlite3.Connection, path: Path) -> str | None:
    """The indexed folder that contains path, if any (longest match wins for nested folders)."""
    best = None
    for root in root_paths(conn, include_hidden=True):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if best is None or len(root) > len(best):
            best = root
    return best


def duplicate_groups(conn: sqlite3.Connection, root: str | None = None) -> list[list[dict]]:
    """Files with identical content (same sha256), oldest first in each group."""
    sql = "SELECT path, root, hash, size, mtime FROM files WHERE hash != ''"  # '' = retry pending
    args: tuple = ()
    if root:
        sql += " AND root = ?"
        args = (root,)
    groups: dict[str, list[dict]] = {}
    for r in conn.execute(sql, args):
        groups.setdefault(r["hash"], []).append(dict(r))
    return [sorted(g, key=lambda f: f["mtime"]) for g in groups.values() if len(g) > 1]

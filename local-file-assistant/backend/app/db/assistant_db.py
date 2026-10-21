"""assistant.db: conversations, memories, tasks. Separate from index.db because that one is
disposable (dropped on a schema bump); this one is migrated forward and never dropped.
Migrations are db/migrations/NNNN_*.sql, applied in order and tracked in PRAGMA user_version."""
import logging
import sqlite3
from pathlib import Path

from app.config import settings

MIGRATIONS = Path(__file__).parent / "migrations"
KEEP_BACKUPS = 2
log = logging.getLogger(__name__)


def connect() -> sqlite3.Connection:
    settings.assistant_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.assistant_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _backup(conn: sqlite3.Connection, version: int) -> None:
    """assistant.db.bak-<version> beside it, made with SQLite's backup API (consistent also in WAL mode), before a
    pending migration runs: 0011 rebuilds a table, and an app update is when migrations run. Only the newest
    KEEP_BACKUPS copies are kept. A failed copy is logged and the migration still runs (each runs in a transaction)."""
    db = settings.assistant_db_path
    try:
        dest = sqlite3.connect(db.with_name(f"{db.name}.bak-{version}"))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        def num(p: Path) -> int:
            tail = p.name.rsplit("-", 1)[-1]
            return int(tail) if tail.isdigit() else -1
        for old in sorted(db.parent.glob(f"{db.name}.bak-*"), key=num, reverse=True)[KEEP_BACKUPS:]:
            old.unlink(missing_ok=True)
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not copy assistant.db before migrating: %s", exc)


def migrate() -> int:
    """Apply pending migrations; returns the schema version afterwards."""
    conn = connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if version > 0 and any(int(f.name[:4]) > version for f in files):  # a new database has nothing to keep
            _backup(conn, version)
        for sql in files:
            n = int(sql.name[:4])
            if n > version:
                # executescript commits first, so the version bump is part of the script.
                conn.executescript(f"BEGIN;\n{sql.read_text(encoding='utf-8')}\nPRAGMA user_version={n};\nCOMMIT;")
                version = n
        return version
    finally:
        conn.close()

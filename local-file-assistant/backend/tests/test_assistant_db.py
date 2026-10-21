from app.config import settings
from app.core import prefs
from app.db import assistant_db


def test_migrate_is_idempotent_and_creates_tables():
    v = assistant_db.migrate()
    assert v >= 1 and assistant_db.migrate() == v
    conn = assistant_db.connect()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"conversations", "messages"} <= names


def test_deleting_a_conversation_cascades_to_messages():
    assistant_db.migrate()
    conn = assistant_db.connect()
    cid = conn.execute("INSERT INTO conversations(created_at, updated_at) VALUES (0, 0)").lastrowid
    conn.execute("INSERT INTO messages(conv_id, role, content, created_at) VALUES (?, 'user', 'hi', 0)", (cid,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE conv_id = ?", (cid,)).fetchone()[0] == 0
    conn.close()


def test_prefs_round_trip_keeps_falsy_values():
    assert prefs.get("nope", 5) == 5
    prefs.set("flag", False)
    assert prefs.get("flag", True) is False
    assert (settings.data_dir / "settings.json").exists()


def _db_at(version: int, tmp_path, monkeypatch):
    """A fresh assistant.db migrated only up to `version` (as an older app left it); returns a function that lets
    the real migrations run on it next."""
    import shutil

    monkeypatch.setattr(settings, "assistant_db_path", tmp_path / "assistant.db")
    older = tmp_path / "older-migrations"
    older.mkdir()
    for f in assistant_db.MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"):
        if int(f.name[:4]) <= version:
            shutil.copy(f, older / f.name)
    real = assistant_db.MIGRATIONS
    monkeypatch.setattr(assistant_db, "MIGRATIONS", older)
    assert assistant_db.migrate() == version
    return lambda: monkeypatch.setattr(assistant_db, "MIGRATIONS", real)


def test_duplicate_workspace_names_are_renamed_so_the_unique_index_applies(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from app.core.assistant import workspaces

    upgrade = _db_at(11, tmp_path, monkeypatch)
    conn = assistant_db.connect()
    for name in ("Study", "study", "Office", "STUDY"):
        conn.execute("INSERT INTO workspaces(name, created_at) VALUES (?, 0)", (name,))
    conn.commit()
    conn.close()
    upgrade()
    assert assistant_db.migrate() >= 12
    assert [w["name"] for w in sorted(workspaces.list_all(), key=lambda w: w["id"])] == ["Study", "study (2)", "Office", "STUDY (3)"]
    conn = assistant_db.connect()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO workspaces(name, created_at) VALUES ('office', 0)")
    conn.close()


def test_a_taken_workspace_name_is_refused_in_plain_words_ignoring_case(tmp_path, monkeypatch):
    import pytest

    from app.core.assistant import workspaces

    monkeypatch.setattr(settings, "assistant_db_path", tmp_path / "assistant.db")
    assistant_db.migrate()
    first = workspaces.create("Ünal Notes", [])
    other = workspaces.create("Office", [])
    for taken in ("ünal notes", "ÜNAL NOTES"):  # non-ASCII: SQLite's lower() wouldn't catch these
        with pytest.raises(ValueError, match="already a workspace called"):
            workspaces.create(taken, [])
    with pytest.raises(ValueError, match="Pick another name"):
        workspaces.update(other["id"], "ünal notes", [])
    assert workspaces.update(first["id"], "ünal notes", [])["name"] == "ünal notes"  # renaming itself is fine


def test_a_v10_database_is_copied_before_it_migrates_and_its_data_survives(tmp_path, monkeypatch):
    import sqlite3

    upgrade = _db_at(10, tmp_path, monkeypatch)
    conn = assistant_db.connect()
    conn.execute("INSERT INTO tool_runs(tool, args_json, risk, status, created_at) VALUES ('create_note', '{}', 'reversible', 'done', 1)")
    conn.execute("INSERT INTO workspaces(name, created_at) VALUES ('College', 0)")
    conn.commit()
    conn.close()
    for old in (7, 8):  # copies from earlier updates: only the newest two are kept
        (tmp_path / f"assistant.db.bak-{old}").write_bytes(b"old")
    upgrade()
    assert assistant_db.migrate() >= 11
    copy = sqlite3.connect(tmp_path / "assistant.db.bak-10")
    try:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 10
        assert copy.execute("SELECT tool FROM tool_runs").fetchall() == [("create_note",)]
    finally:
        copy.close()
    assert sorted(p.name for p in tmp_path.glob("assistant.db.bak-*")) == ["assistant.db.bak-10", "assistant.db.bak-8"]
    conn = assistant_db.connect()
    try:  # 0011 rebuilt tool_runs: the row is still there
        assert [tuple(r) for r in conn.execute("SELECT tool, risk, status FROM tool_runs")] == [("create_note", "reversible", "done")]
        assert [r[0] for r in conn.execute("SELECT name FROM workspaces")] == ["College"]
    finally:
        conn.close()
    assert assistant_db.migrate() >= 11 and len(list(tmp_path.glob("assistant.db.bak-*"))) == 2  # nothing pending: no new copy

"""Whole-PC access tier 2: file contents are read in the background into hidden roots of the main index."""
import os
import time
from pathlib import Path

import pytest

from app.api import routes_files
from app.config import settings
from app.core import indexer, pc_access, pc_content
from app.core.llm import idle
from app.core.search import hybrid
from app.db import sqlite_fts

REAL_PAUSE_REASON = pc_content.pause_reason


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "index.db")
    monkeypatch.setattr(settings, "vector_db_dir", tmp_path / "lance")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "drive" / "Users" / "me"))
    monkeypatch.setattr(pc_content, "pause_reason", lambda: "")
    drive = tmp_path / "drive"
    docs = drive / "Users" / "me" / "Documents"
    docs.mkdir(parents=True)
    (docs / "insurance.txt").write_text("Motor insurance policy number ZX9 renews in March.")
    (docs / "recipes.md").write_text("Grandma's rasgulla recipe needs fresh chenna.")
    (drive / "Other").mkdir()
    (drive / "Other" / "old_report.txt").write_text("Quarterly turbine vibration report.")
    (drive / "Users" / "me" / "passwords.txt").write_text("hunter2")
    (drive / "Users" / "me" / "server.pem").write_text("key")
    (drive / "Windows").mkdir()
    (drive / "Windows" / "sys.txt").write_text("system words")
    pc_access.set_config(enabled=True, drives=[str(drive)])
    pc_access.scan([str(drive)])
    pc_content.sync_roots()
    return drive


def drain(w=None):
    w = w or pc_content.ContentWorker()
    while w.run_once():
        pass
    return w


def indexed_names():
    c = sqlite_fts.connect(settings.db_path)
    try:
        return {Path(p).name for p in sqlite_fts.indexed_paths(c)}
    finally:
        c.close()


def test_reads_contents_and_never_credentials_or_system_folders(pc):
    drain()
    names = indexed_names()
    assert {"insurance.txt", "recipes.md", "old_report.txt"} <= names
    assert not names & {"passwords.txt", "server.pem", "sys.txt"}


def test_hidden_root_is_registered_but_not_listed_scanned_or_watched(pc):
    c = sqlite_fts.connect(settings.db_path)
    try:
        assert sqlite_fts.hidden_roots(c) == [str(pc.resolve())]
        assert sqlite_fts.list_roots(c) == []  # not on the Index page
        assert sqlite_fts.unscanned_roots(c) == []  # never rglob-scanned at startup
        assert sqlite_fts.root_paths(c) == []  # the watcher watches root_paths()
        assert sqlite_fts.root_for(c, pc / "Other" / "old_report.txt") == str(pc.resolve())
    finally:
        c.close()
    assert routes_files.roots()["roots"] == []


def test_questions_find_content_with_name_and_location(pc):
    drain()
    res = hybrid.hybrid_search("when does my motor insurance renew")["results"]
    assert res and Path(res[0]["path"]).name == "insurance.txt"
    assert Path(res[0]["path"]).parent.name == "Documents"  # location is in the index
    assert "March" in res[0]["text"]


def test_users_own_folders_come_first(pc):
    c = pc_content._conn()
    try:
        rows = pc_content.next_batch(c)
    finally:
        c.close()
    order = [Path(r["path"]).name for r in rows]
    assert order.index("insurance.txt") < order.index("old_report.txt")
    assert order.index("recipes.md") < order.index("old_report.txt")


def test_only_new_or_changed_files_are_read_again(pc):
    drain()
    c = pc_content._conn()
    try:
        assert pc_content.next_batch(c) == []
        assert pc_content.progress(c)["pending"] == 0
    finally:
        c.close()
    f = pc / "Users" / "me" / "Documents" / "insurance.txt"
    f.write_text("Motor insurance renews in JUNE now, policy changed.")
    os.utime(f, (time.time() + 5, time.time() + 5))
    (pc / "Users" / "me" / "Documents" / "fresh.txt").write_text("A brand new file about kayaks.")
    pc_access.scan([str(pc)])
    drain()
    assert "fresh.txt" in indexed_names()
    res = hybrid.hybrid_search("insurance renews")["results"]
    assert any("JUNE" in r["text"] for r in res)


def test_deleted_files_leave_the_index(pc):
    drain()
    (pc / "Other" / "old_report.txt").unlink()
    pc_access.scan([str(pc)])
    drain()
    assert "old_report.txt" not in indexed_names()


def test_orphan_sweep_waits_when_the_catalog_is_empty(pc):
    drain()
    c = pc_content._conn()
    try:
        c.execute("DELETE FROM catalog")  # looks like 'everything vanished', e.g. a catalog rebuild
        c.commit()
        assert pc_content.sweep_orphans(c, crawler_running=False) == 0
    finally:
        c.close()
    assert "insurance.txt" in indexed_names()


def test_files_inside_a_folder_the_user_added_are_left_to_that_folder(pc):
    docs = pc / "Users" / "me" / "Documents"
    c = sqlite_fts.connect(settings.db_path)
    sqlite_fts.add_root(c, docs.resolve())
    c.close()
    drain()
    assert "insurance.txt" not in indexed_names()  # not read twice under another root
    assert "old_report.txt" in indexed_names()


def test_images_are_off_until_asked(pc):
    assert ".png" not in pc_content.wanted_exts()
    from app.core.parsers import PARSERS

    if ".png" in PARSERS:  # the image parser only exists where OCR is installed
        assert ".png" in pc_content.wanted_exts(read_images=True)
    assert ".pdf" in pc_content.wanted_exts()
    assert ".py" not in pc_content.wanted_exts()  # code is catalogued, not bulk-read
    assert ".mp4" not in pc_content.wanted_exts()


def test_big_files_are_skipped(pc, monkeypatch):
    monkeypatch.setattr(pc_content, "MAX_BYTES", 10)
    drain()
    assert indexed_names() == set()


def test_pause_reasons(pc, monkeypatch):
    monkeypatch.setattr(pc_content, "pause_reason", REAL_PAUSE_REASON)
    idle.touch()
    assert pc_content.pause_reason() == "chat is busy"
    monkeypatch.setattr(idle, "recently_used", lambda s: False)
    monkeypatch.setattr(pc_content.ram, "is_low", lambda: True)
    assert pc_content.pause_reason() == "low memory"
    monkeypatch.setattr(pc_content.ram, "is_low", lambda: False)

    class Batt:
        power_plugged = False

    import psutil

    monkeypatch.setattr(psutil, "sensors_battery", lambda: Batt(), raising=False)
    assert pc_content.pause_reason() == "on battery"


def test_a_paused_worker_reads_nothing(pc, monkeypatch):
    monkeypatch.setattr(pc_content, "pause_reason", lambda: "on battery")
    pc_content.ContentWorker().run_once()
    assert indexed_names() == set()


def test_partial_files_are_retried_for_their_vectors(pc, monkeypatch):
    from tests.conftest import fake_embed

    def down(texts, timeout=None):
        raise ConnectionError("model server down")

    monkeypatch.setattr(indexer, "embed", down)
    drain()
    c = pc_content._conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM content_done WHERE state='partial'").fetchone()[0] >= 1
        assert pc_content.next_batch(c) == []  # not hammered straight away
        c.execute("UPDATE content_done SET at = at - 3600")
        c.commit()
        monkeypatch.setattr(indexer, "embed", fake_embed)
        assert len(pc_content.next_batch(c)) >= 1
    finally:
        c.close()


def test_off_means_results_are_hidden_but_nothing_is_deleted(pc):
    drain()
    assert hybrid.hybrid_search("motor insurance")["results"]
    pc_access.set_config(enabled=False)
    assert hybrid.hybrid_search("motor insurance")["results"] == []
    assert "insurance.txt" in indexed_names()
    pc_access.set_config(enabled=True)
    assert hybrid.hybrid_search("motor insurance")["results"]


def test_forget_removes_everything_read_and_resets_state(pc):
    drain()
    assert pc_content.forget() >= 3
    assert indexed_names() == set()
    c = pc_content._conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM content_done").fetchone()[0] == 0
    finally:
        c.close()
    c = sqlite_fts.connect(settings.db_path)
    try:
        assert sqlite_fts.hidden_roots(c) == []
    finally:
        c.close()


def test_a_drive_taken_off_the_list_is_forgotten(pc, tmp_path):
    drain()
    other = tmp_path / "second"
    other.mkdir()
    pc_access.set_config(drives=[str(other)])
    pc_content.sync_roots()
    assert indexed_names() == set()


def test_rebuilt_index_triggers_a_reread(pc):
    drain()
    c = sqlite_fts.connect(settings.db_path)
    c.execute("DELETE FROM files")
    c.commit()
    c.close()
    pc_content.ContentWorker().run_once()
    assert "insurance.txt" in indexed_names()


def test_status_reports_progress(pc):
    drain()
    pc_content.worker._counts_at = 0  # the worker caches its counts for a few seconds
    st = pc_access.status()["content"]
    assert st["eligible"] >= 3 and st["pending"] == 0 and st["read_content"] is True


def test_like_prefix_treats_percent_and_underscore_literally(pc, tmp_path):
    import sqlite3

    c = sqlite3.connect(":memory:")
    pat = pc_content._like_prefix(str(tmp_path / "a_b%c"))
    sep = os.sep
    base = os.path.normcase(str(tmp_path / "a_b%c"))
    assert c.execute("SELECT ? LIKE ? ESCAPE '\\'", (base + sep + "x.txt", pat)).fetchone()[0] == 1
    other = os.path.normcase(str(tmp_path / "aXbYc"))
    assert c.execute("SELECT ? LIKE ? ESCAPE '\\'", (other + sep + "x.txt", pat)).fetchone()[0] == 0


def test_macro_and_installer_files_are_never_opened_and_more_credentials_are_sensitive():
    from app.core.assistant.tools import builtin

    assert {".docm", ".xlsm", ".iso", ".chm", ".py"} <= builtin.BLOCKED_EXT
    for name in (".git-credentials", ".npmrc", "client.ovpn", "known_hosts", "NTUSER.DAT"):
        assert pc_access.is_sensitive(Path("C:/Users/me") / name), name


def test_stop_on_the_index_page_does_not_stall_or_spin_the_content_worker(pc):
    indexer.CANCEL.set()
    try:
        assert pc_content.ContentWorker().run_once() > 0  # still reads: CANCEL is for manual folder scans
        assert "insurance.txt" in indexed_names()
    finally:
        indexer.CANCEL.clear()


def test_pc_contents_cannot_crowd_out_normal_results_when_pc_access_is_off(pc, tmp_path):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "mine.txt").write_text("motor insurance notes of my own")
    for i in range(30):  # many strong PC hits that would fill the result slots
        (pc / "Users" / "me" / "Documents" / f"ins{i}.txt").write_text("motor insurance motor insurance motor insurance policy " * 3)
    pc_access.scan([str(pc)])
    drain()
    indexer.index_folder(folder, settings.db_path, settings.vector_db_dir)
    pc_access.set_config(enabled=False)
    names = [Path(r["path"]).name for r in hybrid.hybrid_search("motor insurance", limit=3)["results"]]
    assert names == ["mine.txt"]


def test_scanned_files_skipped_before_ocr_are_queued_again_once_ocr_arrives(pc, monkeypatch):
    from app.core.parsers import image_parser

    conn = pc_content._conn()
    try:
        conn.execute("INSERT INTO content_done(path,size,mtime,state,at) VALUES('C:/x/class 10.pdf',1,1,'skipped',1)")
        conn.execute("INSERT INTO content_done(path,size,mtime,state,at) VALUES('C:/x/notes.txt',1,1,'skipped',1)")
        conn.commit()
        w = pc_content.ContentWorker()
        monkeypatch.setattr(image_parser, "available", lambda: False)
        w._reset_if_ocr_arrived(conn)
        assert conn.execute("SELECT COUNT(*) FROM content_done").fetchone()[0] == 2  # no OCR: nothing changes
        monkeypatch.setattr(image_parser, "available", lambda: True)
        w._reset_if_ocr_arrived(conn)
        left = [r[0] for r in conn.execute("SELECT path FROM content_done")]
        assert left == ["C:/x/notes.txt"]  # only the scanned-type file is queued again
        conn.execute("INSERT INTO content_done(path,size,mtime,state,at) VALUES('C:/x/b.pdf',1,1,'skipped',1)")
        w._reset_if_ocr_arrived(conn)  # runs once only
        assert conn.execute("SELECT COUNT(*) FROM content_done").fetchone()[0] == 2
    finally:
        conn.close()

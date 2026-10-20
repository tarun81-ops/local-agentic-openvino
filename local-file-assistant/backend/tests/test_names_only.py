"""Names-only mode (the default outside tests): the app never stores the text of your files."""
import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import find_file, indexer, known_folders, pc_access, pc_content
from app.core.assistant import file_agent
from app.db import sqlite_fts

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def names_only(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "store_file_text", False)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "index.db")
    monkeypatch.setattr(settings, "vector_db_dir", tmp_path / "data" / "lancedb")
    (tmp_path / "data").mkdir()
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "internship_offer_letter.txt").write_text("Dear Tarun, we are pleased to offer you an internship.")
    (home / "Documents" / "notes.md").write_text("semiconductor notes " * 50)
    return home


def _stored_text_rows():
    if not settings.db_path.exists():
        return 0
    conn = sqlite_fts.connect(settings.db_path)
    try:
        return conn.execute("SELECT count(*) FROM files").fetchone()[0]
    finally:
        conn.close()


def test_no_folder_indexing_stores_anything(names_only):
    out = indexer.index_folder(names_only / "Documents", settings.db_path, settings.vector_db_dir)
    assert out["indexed"] == 0
    assert indexer.index_file(names_only / "Documents" / "notes.md", settings.db_path, settings.vector_db_dir) == "ignored"
    assert _stored_text_rows() == 0
    assert not settings.vector_db_dir.exists() or not any(settings.vector_db_dir.rglob("*.lance"))


def test_background_reading_and_auto_folders_are_off_whatever_was_saved(names_only):
    pc_access.set_config(enabled=True, drives=[str(names_only)], read_content=True)  # an old saved "on"
    known_folders.set_enabled(True)
    assert pc_access.config()["read_content"] is False
    assert known_folders.enabled() is False and known_folders.startup_keys() == []
    pc_access.scan([str(names_only)])
    pc_content.worker.run_once()
    assert _stored_text_rows() == 0
    pc_access.set_config(enabled=False)


def test_answers_read_files_live_without_keeping_a_copy(names_only, monkeypatch):
    monkeypatch.setattr(file_agent, "CACHE_IF_SLOWER_S", 0)  # with storage on, this read would be cached
    pc_access.set_config(enabled=True, drives=[str(names_only)])
    pc_access.scan([str(names_only)])
    chunks, problem = file_agent.read_chunks(str(names_only / "Documents" / "internship_offer_letter.txt"), "offer letter", ["offer"])
    assert problem == "" and any("internship" in c["text"] for c in chunks)
    conn = pc_access.connect()
    try:
        has_cache = conn.execute("SELECT name FROM sqlite_master WHERE name='parse_cache'").fetchone()
        cached = conn.execute("SELECT count(*) FROM parse_cache").fetchone()[0] if has_cache else 0
    finally:
        conn.close()
    assert cached == 0
    pc_access.set_config(enabled=False)


def test_find_still_finds_by_name(names_only):
    pc_access.set_config(enabled=True, drives=[str(names_only)])
    pc_access.scan([str(names_only)])
    out = find_file.find("my internship offer letter", now=datetime(2026, 10, 15))
    assert out["results"] and out["results"][0]["name"] == "internship_offer_letter.txt"
    pc_access.set_config(enabled=False)


def test_api_refuses_to_index_folders_but_opens_pc_files(names_only, monkeypatch):
    from app.api import routes_files
    from app.main import app

    opened = []
    monkeypatch.setattr(routes_files.subprocess, "Popen", lambda args, *a, **k: opened.append(args))
    monkeypatch.setattr(routes_files.os, "startfile", lambda p: opened.append(p), raising=False)
    pc_access.set_config(enabled=True, drives=[str(names_only)])
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        r = c.post("/files/roots", json={"folder": str(names_only / "Documents")}, headers=AUTH)
        assert r.status_code == 409 and "names only" in r.json()["detail"]
        assert c.post("/files/common", json={"keys": ["documents"]}, headers=AUTH).status_code == 409
        assert c.post("/files/index", json={"folder": str(names_only / "Documents")}, headers=AUTH).status_code == 409
        r = c.post("/files/open", json={"path": str(names_only / "Documents" / "notes.md")}, headers=AUTH)
        assert r.status_code == 200 and opened
        outside = names_only.parent / "elsewhere.txt"
        outside.write_text("x")
        assert c.post("/files/open", json={"path": str(outside)}, headers=AUTH).status_code == 404
    assert _stored_text_rows() == 0
    pc_access.set_config(enabled=False)


# ---------- end to end: the app reads your PC's files live and correctly ----------

CORPUS = __import__("pathlib").Path(__file__).resolve().parents[2] / "test_corpus"


@pytest.fixture
def pc_drive(names_only):
    """A fake drive with real documents a few folders deep, nothing indexed; whole-PC access on."""
    import shutil

    drive = names_only.parent / "drive"
    (drive / "Users" / "me" / "Documents" / "Bills").mkdir(parents=True)
    (drive / "Users" / "me" / "Downloads").mkdir(parents=True)
    shutil.copy(CORPUS / "invoice_notes.pdf", drive / "Users" / "me" / "Documents" / "Bills" / "acme_invoice.pdf")
    shutil.copy(CORPUS / "meeting_notes.docx", drive / "Users" / "me" / "Downloads" / "meeting_notes.docx")
    pc_access.set_config(enabled=True, drives=[str(drive)])
    pc_access.scan([str(drive)])
    yield drive
    pc_access.set_config(enabled=False)


def _drain(gen):
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def test_chat_finds_and_reads_a_pdf_live_with_a_page_to_cite(pc_drive, monkeypatch):
    from app.api import routes_chat

    def model_down(*a, **k):  # the planning/choosing model is optional: rules and names must do the work
        raise RuntimeError("model server down")

    monkeypatch.setattr(file_agent, "llm_json", model_down)
    events, (chunks, kind) = _drain(routes_chat._pc_lookup("acme invoice", "what is the total on my acme invoice?", "chat", []))
    assert kind == "files"
    hit = [c for c in chunks if c["name"] == "acme_invoice.pdf"]
    assert hit and "$4,250" in " ".join(c["text"] for c in hit)  # the real text, read from disk now
    assert hit[0]["loc_kind"] == "page" and int(hit[0]["loc_no"]) == 1  # a page to cite
    assert any("Read acme_invoice.pdf" in e for e in events)
    assert _stored_text_rows() == 0  # read live, nothing kept


def test_word_documents_are_read_live_too(pc_drive, monkeypatch):
    monkeypatch.setattr(file_agent, "llm_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    _events, found = _drain(file_agent.gather("open my meeting notes"))
    assert any(c["name"] == "meeting_notes.docx" and c["text"].strip() for c in found["chunks"])
    assert _stored_text_rows() == 0


def test_a_file_changed_on_disk_is_read_fresh_not_from_an_old_copy(pc_drive, monkeypatch):
    note = pc_drive / "Users" / "me" / "Documents" / "fees_receipt.txt"
    note.write_text("Hostel fees paid: 12,000")
    pc_access.scan([str(pc_drive)])
    first, _ = file_agent.read_chunks(str(note), "hostel fees", ["hostel", "fees"])
    note.write_text("Hostel fees paid: 15,500")  # the file changes after it was read once
    second, _ = file_agent.read_chunks(str(note), "hostel fees", ["hostel", "fees"])
    assert "12,000" in first[0]["text"] and "15,500" in second[0]["text"]


def test_questions_do_not_call_the_embedding_model_in_names_only_mode(names_only, monkeypatch):
    import app.core.search.vector_search as vs
    from app.core.search.hybrid import hybrid_search

    calls = []

    def boom(*a, **k):  # search swallows embedding errors, so record the call instead of raising
        calls.append(a)
        raise RuntimeError("embedding model called with nothing to search")

    # Like the laptop: an old semantic index is still on disk from before names-only mode.
    monkeypatch.setattr(settings, "store_file_text", True)
    indexer.index_folder(names_only / "Documents", settings.db_path, settings.vector_db_dir)
    conn = sqlite_fts.connect(settings.db_path)
    with conn:
        conn.execute("DELETE FROM chunks")  # its keyword text is gone too (index.db was deleted)
        conn.execute("DELETE FROM files")
    conn.close()
    monkeypatch.setattr(settings, "store_file_text", False)
    monkeypatch.setattr(vs, "embed", boom)
    out = hybrid_search("what is the total on my acme invoice?")
    assert calls == [] and out["results"] == [] and out["semantic"] is False


def test_credential_files_are_never_read_even_when_named(pc_drive, monkeypatch):
    secret = pc_drive / "Users" / "me" / "Documents" / "passwords.txt"
    secret.write_text("bank pin 1234")
    pc_access.scan([str(pc_drive)])
    chunks, problem = file_agent.read_chunks(str(secret), "passwords", ["passwords"])
    assert chunks == [] and problem


def test_form_upload_suggestions_come_from_pc_file_names(pc_drive):
    from app.core.assistant import forms

    (pc_drive / "Users" / "me" / "Documents" / "Tarun_Resume_2026.pdf").write_bytes(b"%PDF-1.4 resume")
    (pc_drive / "Users" / "me" / "Documents" / "resume_notes.txt").write_text("draft")
    pc_access.scan([str(pc_drive)])
    got = forms.suggest("Upload your resume (PDF)", accept=".pdf")
    assert [r["name"] for r in got] == ["Tarun_Resume_2026.pdf"]  # the .txt is filtered out by accept
    assert _stored_text_rows() == 0


def test_study_reads_the_file_live_and_cites_its_page(pc_drive, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import routes_study
    from app.main import app

    pdf = pc_drive / "Users" / "me" / "Documents" / "Bills" / "acme_invoice.pdf"
    seen = {}
    monkeypatch.setattr(routes_study.ram, "is_low", lambda: False)
    monkeypatch.setattr(routes_study.client, "llm_json", lambda p, max_tokens=300: seen.update(p=p) or {"items": [{"front": "Invoice total?", "back": "$4,250", "src": 1}]})
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        r = c.post("/study/generate", json={"path": str(pdf), "kind": "flashcards", "count": 5}, headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()["items"] == [{"front": "Invoice total?", "back": "$4,250", "loc_kind": "page", "loc_no": 1}]
        assert "$4,250" in seen["p"]  # the model got the real text, read from disk
        outside = pc_drive.parent / "not_covered.pdf"
        outside.write_bytes(pdf.read_bytes())
        assert c.post("/study/generate", json={"path": str(outside)}, headers=AUTH).status_code == 404
    assert _stored_text_rows() == 0

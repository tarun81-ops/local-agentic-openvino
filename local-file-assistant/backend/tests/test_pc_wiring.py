"""Whole-PC access wired into the rest of the app: indexing, chat and open_path."""
import pytest

from app.api import routes_chat
from app.config import settings
from app.core import indexer, pc_access
from app.core.assistant import file_agent
from app.core.assistant.tools import builtin
from app.core.assistant.tools.registry import ToolError


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    drive = tmp_path / "drive"
    (drive / "Docs").mkdir(parents=True)
    (drive / "Docs" / "bill.txt").write_text("amount due 100")
    (drive / "Docs" / "passwords.txt").write_text("hunter2")
    (drive / ".ssh").mkdir()
    (drive / ".ssh" / "notes.txt").write_text("key")
    pc_access.set_config(enabled=True, drives=[str(drive)])
    return drive


def test_credential_files_are_never_indexed_even_in_a_chosen_folder(pc):
    profile = {}
    assert indexer.is_allowed(pc / "Docs" / "bill.txt", pc, profile)
    assert not indexer.is_allowed(pc / "Docs" / "passwords.txt", pc, profile)
    assert not indexer.is_allowed(pc / ".ssh" / "notes.txt", pc, profile)


def _drain(gen):
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def _chunk(path, n=1):
    return {"path": str(path), "name": path.name, "loc_kind": "page", "loc_no": n, "chunk_no": n, "text": "amount due 100", "snippet": "amount due 100", "match": "pc"}


def test_chat_adds_pc_files_and_switches_to_files_route(pc, monkeypatch):
    def fake_gather(question, content_hits=None, within=()):
        yield {"step": "plan", "detail": "Looking for: bill"}
        return {"chunks": [_chunk(pc / "Docs" / "bill.txt")], "files": [str(pc / "Docs" / "bill.txt")]}

    monkeypatch.setattr(file_agent, "gather", fake_gather)
    events, (chunks, kind) = _drain(routes_chat._pc_lookup("my bill", "find my electricity bill document", "chat", []))
    assert kind == "files" and len(chunks) == 1
    assert events and events[0].startswith("event: pc_step")


def test_chat_does_not_duplicate_chunks_already_found(pc, monkeypatch):
    existing = _chunk(pc / "Docs" / "bill.txt")
    monkeypatch.setattr(file_agent, "gather", lambda q, content_hits=None, within=(): iter(()) if False else _gen({"chunks": [existing], "files": []}))
    _events, (chunks, kind) = _drain(routes_chat._pc_lookup("bill", "my bill file", "files", [existing]))
    assert len(chunks) == 1 and kind == "files"


def _gen(value):
    return value
    yield  # pragma: no cover  (makes this a generator that returns value)


def test_chat_is_untouched_when_pc_access_is_off(pc, monkeypatch):
    pc_access.set_config(enabled=False)
    called = []
    monkeypatch.setattr(file_agent, "gather", lambda *a, **k: called.append(1))
    _events, (chunks, kind) = _drain(routes_chat._pc_lookup("bill", "find my bill document", "chat", []))
    assert chunks == [] and kind == "chat" and not called


def test_chat_survives_an_agent_failure(pc, monkeypatch):
    def boom(question, content_hits=None, within=()):
        raise RuntimeError("model exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(file_agent, "gather", boom)
    _events, (chunks, kind) = _drain(routes_chat._pc_lookup("bill", "find my bill document", "files", []))
    assert chunks == [] and kind == "files"


def test_chat_ignores_non_document_questions(pc, monkeypatch):
    called = []
    monkeypatch.setattr(file_agent, "gather", lambda *a, **k: called.append(1))
    _e, (chunks, kind) = _drain(routes_chat._pc_lookup("hi", "how are you today", "chat", []))
    assert kind == "chat" and not called


def test_open_path_accepts_pc_files_but_not_credentials_or_outside(pc, tmp_path, monkeypatch):
    monkeypatch.setattr(builtin, "roots", lambda: [])
    monkeypatch.setattr(builtin, "_allowed_dirs", lambda: [])
    ok = builtin._open_path_validate({"path": str(pc / "Docs" / "bill.txt")})
    assert ok["path"].endswith("bill.txt")
    with pytest.raises(ToolError):
        builtin._open_path_validate({"path": str(pc / "Docs" / "passwords.txt")})
    outside = tmp_path / "other.txt"
    outside.write_text("x")
    with pytest.raises(ToolError):
        builtin._open_path_validate({"path": str(outside)})


def test_class_10_percentage_question_finds_and_reads_the_marksheet(tmp_path, monkeypatch):
    """The user's real case: no file word in the question, the file is called '10th Marksheet'."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    drive = tmp_path / "d"
    (drive / "Users" / "me" / "Documents" / "School").mkdir(parents=True)
    (drive / "Users" / "me" / "Documents" / "School" / "10th Marksheet.txt").write_text(
        "SECONDARY SCHOOL EXAMINATION 2022 Class X Marks obtained 468 out of 500 Percentage 93.6")
    (drive / "Users" / "me" / "Documents" / "School" / "12th Marksheet.txt").write_text("Class XII Marks obtained 400 out of 500")
    (drive / "Users" / "me" / "Documents" / "electricity bill.txt").write_text("amount due 500")
    pc_access.set_config(enabled=True, drives=[str(drive)])
    pc_access.scan([str(drive)])
    monkeypatch.setattr(file_agent, "llm_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))

    q = "what is my class 10 percentage"
    assert file_agent.agent_worthy(q)
    assert not file_agent.agent_worthy("what is the capital of France")
    events, (chunks, kind) = _drain(routes_chat._pc_lookup(q, q, "chat", []))
    assert kind == "files"
    names = {c["name"] for c in chunks}
    assert "10th Marksheet.txt" in names
    assert any("93.6" in c["text"] for c in chunks)
    assert events


def test_follow_up_naming_this_file_is_rewritten_from_the_chat():
    from app.core.assistant.router import needs_rewrite

    assert needs_rewrite("can you read the content inside this file")
    assert needs_rewrite("what is the total in that document please tell me")
    assert not needs_rewrite("find my electricity bill for september please now")


def test_marks_passage_wins_over_the_header_passage(tmp_path, monkeypatch):
    from app.core.assistant import file_agent

    header = ["Central Board of Secondary Education Marks Statement cum Certificate class 10 percentage"] + ["x"] * 5
    table = ["Subject Marks Obtained Total English 081 Mathematics 090 Science 088 Hindi 085 Total 424 of 500"]
    chunks = [type("C", (), {"text": " ".join(header)})(), type("C", (), {"text": " ".join(table)})()]
    f = tmp_path / "class 10.pdf"
    f.write_bytes(b"x")
    monkeypatch.setattr(file_agent.pc_access, "can_read", lambda p: (True, ""))
    monkeypatch.setattr(file_agent, "_parsers", lambda: {".pdf": lambda p: [type("C", (), {"text": c.text, "loc_kind": "page", "loc_no": 1})() for c in chunks]})
    monkeypatch.setattr(file_agent, "MAX_CHUNKS_PER_FILE", 1)
    got, why = file_agent.read_chunks(str(f), "what is my class 10 percentage", ["class", "10", "percentage"])
    assert why == "" and "Total 424" in got[0]["text"]


def _slow_parser(calls, delay=0.0):
    import time as _t

    from app.core.chunking import Chunk

    def parse(path):
        calls.append(1)
        _t.sleep(delay)
        return [Chunk("page", 1, "Marks Obtained Total 443 of 500")]

    return parse


def test_slow_reads_are_remembered_and_re_read_when_the_file_changes(tmp_path, monkeypatch):
    import os

    from app.core import pc_access
    from app.core.assistant import file_agent

    monkeypatch.setattr(pc_access.settings, "data_dir", tmp_path / "data", raising=False)
    f = tmp_path / "class 10.pdf"
    f.write_bytes(b"x")
    calls: list[int] = []
    monkeypatch.setattr(file_agent.pc_access, "can_read", lambda p: (True, ""))
    monkeypatch.setattr(file_agent, "_parsers", lambda: {".pdf": _slow_parser(calls, 0.05)})
    monkeypatch.setattr(file_agent, "CACHE_IF_SLOWER_S", 0.01)
    file_agent.read_chunks(str(f), "marks", ["marks"])
    got, _ = file_agent.read_chunks(str(f), "marks", ["marks"])
    assert len(calls) == 1 and "443" in got[0]["text"]  # second question did not parse again
    f.write_bytes(b"changed!")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    file_agent.read_chunks(str(f), "marks", ["marks"])
    assert len(calls) == 2  # an edited file is read again


def test_fast_reads_are_not_cached_and_forget_clears_the_cache(tmp_path, monkeypatch):
    from app.core import pc_access, pc_content
    from app.core.assistant import file_agent

    monkeypatch.setattr(pc_access.settings, "data_dir", tmp_path / "data", raising=False)
    f = tmp_path / "a.txt"
    f.write_text("x")
    calls: list[int] = []
    monkeypatch.setattr(file_agent.pc_access, "can_read", lambda p: (True, ""))
    monkeypatch.setattr(file_agent, "_parsers", lambda: {".txt": _slow_parser(calls)})
    file_agent.read_chunks(str(f), "q", ["marks"])
    file_agent.read_chunks(str(f), "q", ["marks"])
    assert len(calls) == 2  # instant parsers are never stored
    monkeypatch.setattr(file_agent, "CACHE_IF_SLOWER_S", 0.0)
    file_agent.read_chunks(str(f), "q", ["marks"])
    assert file_agent._cache_get(f, f.stat()) is not None
    pc_content.forget()
    assert file_agent._cache_get(f, f.stat()) is None

"""Study cards: cleaning, budgeted sampling, and the refusals. The model is always stubbed."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import routes_study
from app.config import settings
from app.db import sqlite_fts
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}
FILE = Path("C:/study/notes.pdf")


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        yield c


def _index(n_chunks: int, size: int = 400) -> None:
    conn = sqlite_fts.connect(settings.db_path)
    chunks = [(i, SimpleNamespace(loc_kind="page", loc_no=i + 1, text=f"chunk{i} " + "x" * size)) for i in range(n_chunks)]
    sqlite_fts.upsert_file(conn, FILE, FILE.parent, 1, 1.0, chunks)
    conn.close()


@pytest.fixture
def indexed():
    _index(3)
    yield
    conn = sqlite_fts.connect(settings.db_path)
    sqlite_fts.delete_file(conn, FILE)
    conn.close()


def test_clean_items_drops_empty_and_duplicates_and_caps():
    chunks = [{"loc_kind": "page", "loc_no": 4, "text": ""}]
    raw = [
        {"front": "What is X?", "back": "A thing", "src": 1},
        {"front": "what is x?", "back": "dup", "src": 1},
        {"front": "", "back": "no front"},
        {"front": "No back", "back": " "},
        {"front": "Bad src", "back": "ok", "src": 9},
        {"front": "L" * 500, "back": "B" * 900, "src": True},
        "junk",
        {"front": "Over the count", "back": "x"},
    ]
    out = routes_study.clean_items(raw, chunks, 3)
    assert [i["front"][:8] for i in out] == ["What is ", "Bad src", "LLLLLLLL"]
    assert out[0]["loc_kind"] == "page" and out[0]["loc_no"] == 4
    assert out[1]["loc_kind"] is None  # cited an excerpt that doesn't exist
    assert len(out[2]["front"]) == routes_study.FRONT_MAX and len(out[2]["back"]) == routes_study.BACK_MAX
    assert routes_study.clean_items("not a list", chunks, 5) == []


def test_small_file_is_read_whole_in_order(indexed):
    conn = sqlite_fts.connect(settings.db_path)
    chunks, sampled = sqlite_fts.file_chunks(conn, FILE, 6000)
    conn.close()
    assert not sampled and [c["loc_no"] for c in chunks] == [1, 2, 3]


def test_large_file_is_sampled_evenly_within_budget():
    _index(100)
    try:
        conn = sqlite_fts.connect(settings.db_path)
        chunks, sampled = sqlite_fts.file_chunks(conn, FILE, 2000)
        conn.close()
    finally:
        conn = sqlite_fts.connect(settings.db_path)
        sqlite_fts.delete_file(conn, FILE)
        conn.close()
    assert sampled and sum(len(c["text"]) for c in chunks) <= 2000
    pages = [c["loc_no"] for c in chunks]
    assert pages == sorted(pages) and pages[0] == 1 and pages[-1] > 50  # reaches late in the file, not just the start


def test_generate_returns_cleaned_cards_with_locations(client, indexed, monkeypatch):
    monkeypatch.setattr(routes_study.ram, "is_low", lambda: False)
    seen = {}
    monkeypatch.setattr(routes_study.client, "llm_json", lambda p, max_tokens=300, **k: seen.update(p=p) or {"items": [{"front": "Q1", "back": "A1", "src": 2}, {"front": "Q1", "back": "dup"}]})
    r = client.post("/study/generate", json={"path": str(FILE), "kind": "quiz", "count": 5}, headers=AUTH)
    body = r.json()
    assert r.status_code == 200 and body["items"] == [{"front": "Q1", "back": "A1", "loc_kind": "page", "loc_no": 2}]
    assert not body["sampled"] and body["note"] == "" and "quiz questions" in seen["p"]


def test_unindexed_path_bad_kind_and_bad_count_are_rejected(client, monkeypatch):
    monkeypatch.setattr(routes_study.client, "llm_json", lambda *a, **k: pytest.fail("model must not run"))
    assert client.post("/study/generate", json={"path": "C:/etc/passwd"}, headers=AUTH).status_code == 404
    assert client.post("/study/generate", json={"path": "x", "kind": "essay"}, headers=AUTH).status_code == 400
    assert client.post("/study/generate", json={"path": "x", "count": 4}, headers=AUTH).status_code == 422
    assert client.post("/study/generate", json={"path": "x", "count": 16}, headers=AUTH).status_code == 422


def test_low_ram_refuses_before_calling_the_model(client, indexed, monkeypatch):
    monkeypatch.setattr(routes_study.ram, "is_low", lambda: True)
    monkeypatch.setattr(routes_study.client, "llm_json", lambda *a, **k: pytest.fail("model must not run"))
    r = client.post("/study/generate", json={"path": str(FILE)}, headers=AUTH)
    assert r.status_code == 503 and "RAM" in r.json()["detail"]


def test_csv_export(client):
    r = client.post("/study/export", json={"items": [{"front": "Q, with comma", "back": "A", "loc_kind": "page", "loc_no": 3}, {"front": "Q2", "back": "A2", "loc_kind": None, "loc_no": None}]}, headers=AUTH)
    lines = r.text.splitlines()
    assert lines[0] == '"Q, with comma",A,page 3' and lines[1] == "Q2,A2,"

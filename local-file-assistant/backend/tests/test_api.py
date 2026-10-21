import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes_organize
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(monkeypatch):
    trashed = []
    monkeypatch.setattr(routes_organize, "trash", lambda p: (trashed.append(p), p.unlink()))
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        c.trashed = trashed
        yield c


def _add_and_wait(client, folder):
    r = client.post("/files/roots", json={"folder": str(folder)}, headers=AUTH)
    assert r.status_code == 200, r.text
    for _ in range(200):
        s = client.get("/files/index/status", headers=AUTH).json()
        if s["state"] in ("done", "error"):
            return s
        time.sleep(0.05)
    raise AssertionError("index run did not finish")


def test_health_is_public_everything_else_needs_the_token(client):
    assert client.get("/health").status_code == 200
    assert client.get("/search", params={"q": "x"}).status_code == 401
    assert client.get("/search", params={"q": "x"}, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_foreign_origin_is_refused_even_with_token(client):
    r = client.post("/organize/undo", json={}, headers={**AUTH, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_cors_preflight_from_the_app_is_answered(client):
    r = client.options(
        "/search",
        headers={"Origin": "null", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
    )
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "null"


def test_index_search_organize_undo_flow(client, corpus, monkeypatch):
    status = _add_and_wait(client, corpus)
    assert status["state"] == "done" and status["indexed"] >= 2

    roots = client.get("/files/roots", headers=AUTH).json()["roots"]
    root = next(r for r in roots if r["path"] == str(corpus.resolve()))
    assert root["state"] == "up_to_date" and root["files"] >= 2

    found = client.get("/search", params={"q": "What is the invoice total for Acme Corp?"}, headers=AUTH).json()
    assert found["results"][0]["name"] == "invoice_notes.pdf"
    assert found["results"][0]["snippet"]
    paths = [r["path"] for r in found["results"]]
    assert len(paths) == len(set(paths))  # one row per file, though both invoice pages match

    # Approved changes (as Tidy-up proposes them): a move and a Recycle Bin delete, applied then undone.
    (corpus / "invoice_copy.pdf").write_bytes((corpus / "invoice_notes.pdf").read_bytes())
    _add_and_wait(client, corpus)
    root_path = corpus.resolve()
    actions = [
        {"op": "move", "path": str(root_path / "meeting_notes.docx"), "to": str(root_path / "Meeting Notes" / "meeting_notes.docx")},
        {"op": "delete", "path": str(root_path / "invoice_copy.pdf")},
    ]
    assert client.post("/organize/plan", json={"root": str(root_path)}, headers=AUTH).status_code in (404, 405)  # AI plan removed

    applied = client.post("/organize/apply", json={"actions": actions}, headers=AUTH).json()
    assert all(r["ok"] for r in applied["results"]), applied
    assert (corpus / "Meeting Notes" / "meeting_notes.docx").exists()
    assert len(client.trashed) == 1

    undone = client.post("/organize/undo", json={"batch": applied["batch"]}, headers=AUTH).json()
    assert all(r["ok"] for r in undone["results"])
    assert (corpus / "meeting_notes.docx").exists()


def test_apply_refuses_paths_outside_indexed_folders(client, corpus, tmp_path):
    _add_and_wait(client, corpus)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch")
    r = client.post(
        "/organize/apply",
        json={"actions": [
            {"op": "move", "path": str(corpus / "invoice_notes.pdf"), "to": str(corpus / "Finance" / "invoice_notes.pdf")},
            {"op": "delete", "path": str(outside)},
        ]},
        headers=AUTH,
    )
    assert r.status_code == 400
    assert outside.exists() and (corpus / "invoice_notes.pdf").exists()  # nothing applied


def test_open_only_opens_indexed_files(client, tmp_path):
    stray = tmp_path / "evil.bat"
    stray.write_text("echo hi")
    assert client.post("/files/open", json={"path": str(stray)}, headers=AUTH).status_code == 404


def _events(body: str) -> list[tuple[str, dict]]:
    import json

    out = []
    for message in body.strip().split("\n\n"):  # SSE: "event: <kind>\ndata: <json>"
        kind, data = message.split("\n", 1)
        out.append((kind.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_chat_reports_a_search_failure_instead_of_hanging(client, monkeypatch):
    from app.api import routes_chat

    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(routes_chat, "hybrid_search", locked)
    events = _events(client.post("/chat", json={"message": "x"}, headers=AUTH).text)
    assert [kind for kind, _ in events] == ["error"] and "database is locked" in events[0][1]["detail"]


def test_chat_streams_results_tokens_then_a_checked_done(client, monkeypatch):
    from app.api import routes_chat

    chunk = {"path": "C:/d/invoice.pdf", "name": "invoice.pdf", "loc_kind": "page", "loc_no": 1, "match": "both",
             "snippet": "total $4,250", "text": "The invoice total for Acme Corp is $4,250."}
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda q, root=None, limit=6, within=None: {"results": [chunk], "semantic": True})

    def answer(question, chunks, history=None, model=None, style=""):
        yield "Acme Corp owes $4,250 "
        yield "(invoice.pdf, page 1)."
        return True  # the model hit its token cap

    monkeypatch.setattr(routes_chat, "answer_stream", answer)
    events = _events(client.post("/chat", json={"message": "total?", "mode": "files"}, headers=AUTH).text)
    kinds = [kind for kind, _ in events]
    assert kinds[:2] == ["route", "results"] and kinds.count("token") == 2 and kinds[-1] == "done"
    done = events[-1][1]
    assert done["all_verified"] and done["truncated"] and done["citations"][0]["status"] == "checked"

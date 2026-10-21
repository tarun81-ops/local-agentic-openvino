import json

import pytest
from fastapi.testclient import TestClient

from app.core.assistant import conversation
from app.core.assistant.router import needs_rewrite, route
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        yield c


def _events(body: str):
    return [(m.split("\n", 1)[0].removeprefix("event: "), json.loads(m.split("\n", 1)[1].removeprefix("data: "))) for m in body.strip().split("\n\n")]


CHUNK = {"path": "C:/d/invoice.pdf", "name": "invoice.pdf", "loc_kind": "page", "loc_no": 1, "match": "keyword",
         "snippet": "total", "text": "The invoice total is $4,250."}


def test_crud_and_cascade(client):
    cid = client.post("/conversations", headers=AUTH).json()["id"]
    assert client.patch(f"/conversations/{cid}", json={"title": "T", "pinned": True}, headers=AUTH).status_code == 200
    got = client.get(f"/conversations/{cid}", headers=AUTH).json()
    assert got["title"] == "T" and got["pinned"] and got["messages"] == []
    client.delete(f"/conversations/{cid}", headers=AUTH)
    assert client.get(f"/conversations/{cid}", headers=AUTH).status_code == 404
    assert client.patch("/conversations/99999", json={"title": "x"}, headers=AUTH).status_code == 404


def test_import_old_local_storage_chats(client):
    chats = [{"title": "Old", "at": 1_700_000_000_000, "turns": [{"q": "hi?", "answer": "yes", "citations": [], "uncited": []}, {"q": "no answer", "answer": ""}]}]
    assert client.post("/conversations/import", json={"chats": chats}, headers=AUTH).json() == {"imported": 1}
    conv = next(c for c in client.get("/conversations", headers=AUTH).json()["conversations"] if c["title"] == "Old")
    msgs = client.get(f"/conversations/{conv['id']}", headers=AUTH).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_window_limits_turns_budget_and_starts_on_a_user_turn():
    msgs = [{"role": r, "content": f"{r}{i}" * 10} for i in range(5) for r in ("user", "assistant")]
    w = conversation.window(msgs, turns=2, token_budget=1000)
    assert len(w) == 4 and w[0]["role"] == "user" and w[-1]["content"].startswith("assistant4")
    tiny = conversation.window(msgs, turns=5, token_budget=40)  # ~160 chars: only the newest pair fits
    assert len(tiny) == 2 and tiny[0]["role"] == "user"


def test_router_rules():
    assert route("files", "hi") == "files" and route("chat", "invoice", [CHUNK]) == "chat"
    assert route("auto", "what is the capital of France", [CHUNK]) == "chat"  # keyword-only hit
    assert route("auto", "find my invoice", [CHUNK]) == "files"  # file word
    assert route("auto", "acme total", [{**CHUNK, "match": "both"}]) == "files"  # both searches agree
    assert route("auto", "invoice", []) == "chat"
    assert needs_rewrite("and the second one?") and not needs_rewrite("what is the invoice total for Acme Corp last year")


def test_chat_persists_and_follow_up_gets_history(client, monkeypatch):
    from app.api import routes_chat

    seen = []

    def fake_chat(message, history=None, model=None, style=""):
        seen.append(history)
        yield "Paris."
        return False

    monkeypatch.setattr(routes_chat, "chat_stream", fake_chat)
    ev = _events(client.post("/chat", json={"message": "capital of France?", "mode": "chat"}, headers=AUTH).text)
    assert [k for k, _ in ev] == ["route", "token", "conversation", "done"] and ev[0][1] == {"route": "chat"}
    cid = ev[2][1]["id"]
    _events(client.post("/chat", json={"message": "and Spain?", "mode": "chat", "conversation_id": cid}, headers=AUTH).text)
    assert seen[0] == [] and [m["role"] for m in seen[1]] == ["user", "assistant"]
    msgs = client.get(f"/conversations/{cid}", headers=AUTH).json()["messages"]
    assert len(msgs) == 4 and msgs[1]["meta"]["route"] == "chat"
    assert client.post("/chat", json={"message": "x", "conversation_id": 99999}, headers=AUTH).status_code == 404


def test_auto_with_no_file_hit_answers_as_chat_and_files_mode_reports_no_results(client, monkeypatch):
    from app.api import routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda q, root=None, limit=6, within=None: {"results": [], "semantic": False})
    monkeypatch.setattr(routes_chat, "chat_stream", lambda m, h=None, model=None, style="": iter(["ok"]))
    kinds = [k for k, _ in _events(client.post("/chat", json={"message": "hello"}, headers=AUTH).text)]
    assert kinds[0] == "route" and "results" not in kinds
    ev = _events(client.post("/chat", json={"message": "hello", "mode": "files"}, headers=AUTH).text)
    assert ev[-1][1]["no_results"] and [k for k, _ in ev][:2] == ["route", "results"]

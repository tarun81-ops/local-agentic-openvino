import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.assistant import conversation, memory_store
from app.core.assistant.router import memory_command
from app.db import assistant_db
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def clean():
    assistant_db.migrate()
    memory_store.wipe()
    conn = assistant_db.connect()  # other test files leave idle chats that extraction would also pick up
    conn.execute("DELETE FROM conversations")
    conn.commit()
    conn.close()
    yield
    memory_store.wipe()


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        yield c


def _events(body: str):
    return [(m.split("\n", 1)[0].removeprefix("event: "), json.loads(m.split("\n", 1)[1].removeprefix("data: "))) for m in body.strip().split("\n\n")]


def test_add_dedupes_and_recall_ranks_the_relevant_fact_first():
    assert memory_store.add("My sister Priya lives in Pune")
    assert memory_store.add("I prefer dark mode in every editor")
    assert memory_store.add("my sister priya lives in pune") is None  # same fact
    top = memory_store.recall("where does my sister live", k=2)
    assert top[0]["text"].startswith("My sister Priya")
    assert memory_store.recall("quantum chromodynamics") == []


def test_delete_and_wipe_remove_facts_from_recall_and_the_search_index():
    a = memory_store.add("I drink green tea every morning")
    memory_store.delete(a["id"])
    assert memory_store.recall("green tea") == []
    memory_store.add("I run on weekends")
    assert memory_store.wipe() == 1 and memory_store.list_all() == []


def test_changing_the_embedding_model_re_embeds(monkeypatch):
    memory_store.add("I use Linux at work")
    monkeypatch.setattr(settings, "embedding_model", "another-model")
    assert memory_store.recall("Linux")  # recall refreshes vectors first
    conn = assistant_db.connect()
    assert conn.execute("SELECT embed_model FROM memories").fetchone()[0] == "another-model"
    conn.close()


def test_edit_updates_text_and_keyword_index():
    m = memory_store.add("I live in Delhi")
    assert memory_store.update(m["id"], text="I live in Mumbai", pinned=True)
    assert memory_store.recall("Mumbai")[0]["pinned"] and memory_store.recall("Delhi") == []


def test_remember_and_forget_commands_through_chat(client):
    assert memory_command("remember that my cat is called Mia") == ("remember", "my cat is called Mia")
    ev = _events(client.post("/chat", json={"message": "Remember that my cat is called Mia."}, headers=AUTH).text)
    assert ev[0] == ("route", {"route": "memory"}) and "Remembered" in ev[1][1]["delta"]
    assert memory_store.list_all()[0]["text"] == "my cat is called Mia"
    ev = _events(client.post("/chat", json={"message": "forget my cat"}, headers=AUTH).text)
    assert "Forgot" in ev[1][1]["delta"] and memory_store.list_all() == []


def test_recalled_facts_reach_the_prompt_and_are_reported(client, monkeypatch):
    from app.api import routes_chat

    memory_store.add("The user's sister is called Priya")
    seen = []

    def fake_chat(message, history=None, model=None, style=""):
        seen.append(message)
        yield "ok"
        return False

    monkeypatch.setattr(routes_chat, "chat_stream", fake_chat)
    ev = _events(client.post("/chat", json={"message": "what is my sister called", "mode": "chat"}, headers=AUTH).text)
    assert "Priya" in seen[0] and "never cite them as files" in seen[0]
    used = next(d for k, d in ev if k == "memory_used")["memories"]
    assert used[0]["text"].endswith("Priya")
    cid = next(d for k, d in ev if k == "conversation")["id"]
    assert client.get(f"/conversations/{cid}", headers=AUTH).json()["messages"][1]["meta"]["memory_used"] == used


def test_memory_api(client):
    made = client.post("/memory", json={"text": "I like chess"}, headers=AUTH).json()
    assert client.post("/memory", json={"text": "I like chess"}, headers=AUTH).status_code == 409
    assert client.patch(f"/memory/{made['id']}", json={"pinned": True}, headers=AUTH).status_code == 200
    assert client.patch("/memory/99999", json={"pinned": True}, headers=AUTH).status_code == 404
    assert client.get("/memory/export", headers=AUTH).json()["memories"][0]["pinned"]
    assert client.post("/memory/wipe", headers=AUTH).json() == {"deleted": 1}


# ---------- memories come only from the user: no background guessing, no review queue ----------

def test_nothing_is_learned_from_chats_in_the_background():
    import app.main as main

    assert not hasattr(main, "memory_extract")  # the background extraction job is gone
    conv = conversation.create("t")
    conversation.add_messages(conv["id"], [("user", "I am asking about my class 10 marksheet", {}), ("assistant", "ok", {})], at=time.time() - 10_000)
    assert memory_store.list_all() == []


def test_old_review_queue_is_cleared_at_startup():
    memory_store.add("user's job or project information is not mentioned", status="pending")  # left by an old version
    kept = memory_store.add("I study ETE in Raipur")
    with TestClient(app, base_url="http://127.0.0.1:8756"):  # app startup
        pass
    assert [m["text"] for m in memory_store.list_all()] == [kept["text"]]
    assert memory_store.counts() == {"active": 1, "pending": 0}


def test_remember_command_saves_straight_away(client):
    client.post("/chat", json={"message": "remember that I study ETE in Raipur"}, headers=AUTH)
    assert any(m["text"].startswith("I study ETE") for m in memory_store.list_all(status="active"))


def test_memory_api_has_no_review_endpoints(client):
    m = memory_store.add("Likes tea")
    assert client.post(f"/memory/{m['id']}/approve", headers=AUTH).status_code in (404, 405)
    assert client.post("/memory/approve-all", headers=AUTH).status_code in (404, 405)
    got = client.get("/memory", headers=AUTH).json()
    assert [x["text"] for x in got["memories"]] == ["Likes tea"] and got["counts"] == {"active": 1, "pending": 0}


def test_file_answers_get_no_memories_and_show_the_cited_line(client, monkeypatch):
    """Regression: "class 10 roll no" was answered "The user is in Class 10…" from an old "user is in
    class 12" memory. File answers come from files only; the source line is the one with the number."""
    from app.api import routes_chat
    from tests.test_verifier import ROLL_PAGE

    memory_store.add("user is in class 12")
    chunk = {"path": "C:/d/class 10.pdf", "name": "class 10.pdf", "loc_kind": "page", "loc_no": 1, "match": "both",
             "snippet": ROLL_PAGE[:120], "text": ROLL_PAGE}
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda q, root=None, limit=6, within=None: {"results": [chunk], "semantic": True})
    seen = []

    def answer(question, chunks, history=None, model=None, style=""):
        seen.append(question)
        yield "Your roll no. is 12127198 (class 10.pdf, page 1)."
        return False

    monkeypatch.setattr(routes_chat, "answer_stream", answer)
    ev = _events(client.post("/chat", json={"message": "class 10 roll no", "mode": "files"}, headers=AUTH).text)
    assert "class 12" not in seen[0] and not any(k == "memory_used" for k, _ in ev)
    cit = ev[-1][1]["citations"][0]
    assert "Roll No. 12127198" in cit["snippet"] and cit["status"] == "checked"

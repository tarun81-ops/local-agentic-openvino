"""RAM guard, the chat client settings and the OpenVINO Model Server embedding path."""
import httpx

from app.config import settings
from app.core import indexer, ram
from app.core.search import vector_search
from app.db import sqlite_fts


def test_low_memory_indexes_keywords_only_and_retries_later(corpus, db_paths, monkeypatch):
    db, vdb = db_paths
    monkeypatch.setattr(ram, "is_low", lambda: True)
    counts = indexer.index_folder(corpus, db, vdb)
    assert counts["indexed"] == 2
    assert counts["keyword_only"] == 2 and not counts["semantic"]  # the scan says what it skipped
    conn = sqlite_fts.connect(db)
    try:
        hashes = {r[0] for r in conn.execute("SELECT hash FROM files")}
        assert sqlite_fts.list_roots(conn)[0]["keyword_only"] == 2
    finally:
        conn.close()
    assert hashes == {""}  # blank = vectors still owed; next scan retries
    assert any("low memory" in e["error"] for e in indexer.RECENT_ERRORS)

    monkeypatch.setattr(ram, "is_low", lambda: False)
    again = indexer.index_folder(corpus, db, vdb)
    assert again["indexed"] == 2 and again["skipped"] == 0
    assert again["keyword_only"] == 0 and again["semantic"]


def test_chat_client_has_a_timeout_and_no_silent_retries():
    from app.core.llm.client import get_client

    client = get_client()
    assert client.max_retries == 0
    assert client.timeout.read == settings.llm_timeout_s and client.timeout.connect == 5


def test_chat_requests_switch_thinking_off(monkeypatch):
    from app.core.llm.client import _without_thinking, get_client

    calls = []
    create = _without_thinking(lambda *a, **kw: calls.append(kw))
    create(model="chat", messages=[])
    assert calls[-1]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    create(model="chat", messages=[], extra_body={"top_k": 5})
    assert calls[-1]["extra_body"] == {"top_k": 5, "chat_template_kwargs": {"enable_thinking": False}}  # caller's top_k kept
    monkeypatch.setattr(settings, "ov_disable_thinking", False)
    create(model="chat", messages=[])
    assert "chat_template_kwargs" not in calls[-1]["extra_body"]  # top_k is still sent
    assert get_client().chat.completions.create.__name__ == "create_no_think"


def test_ovms_embeddings(monkeypatch):
    monkeypatch.setattr(settings, "ov_base_url", "http://127.0.0.1:9000/v3")
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]})

    real_client = httpx.Client
    monkeypatch.setattr(vector_search.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    from tests.conftest import REAL_EMBED

    out = REAL_EMBED(["a", "b"])
    assert seen["url"] == "http://127.0.0.1:9000/v3/embeddings"
    assert out == [[1.0], [2.0]]  # re-ordered by index


def test_memory_status_shape():
    s = ram.status()
    assert set(s) == {"free_mb", "total_mb", "low"}

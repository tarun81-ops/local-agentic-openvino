"""End-to-end check against a real OpenVINO Model Server: index the test corpus, ask a question,
verify the citations. Skipped unless OVMS is running and serves the configured models.
Run on its own with:  .venv\\Scripts\\python.exe -m pytest tests\\test_live_ovms.py -s"""
import time

import pytest

import app.core.indexer as indexer_mod
import app.core.search.vector_search as vs_mod
from app.core.llm.client import current_model, server_status
from tests.conftest import REAL_EMBED


def test_answer_is_cited_and_verified(corpus, db_paths, monkeypatch):
    status = server_status()
    if not status["connected"] or current_model() not in status["models"]:
        pytest.skip("OVMS not running, or it doesn't serve the configured model")
    monkeypatch.setattr(indexer_mod, "embed", REAL_EMBED)
    monkeypatch.setattr(vs_mod, "embed", REAL_EMBED)

    from app.config import settings
    from app.core.llm.answerer import answer_stream
    from app.core.llm.verifier import verify
    from app.core.search.hybrid import hybrid_search

    db, vdb = db_paths
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "vector_db_dir", vdb)
    indexer_mod.index_folder(corpus, db, vdb)
    question = "What is the invoice total for Acme Corp?"
    chunks = hybrid_search(question)["results"]
    t0 = time.perf_counter()
    text = "".join(answer_stream(question, chunks))
    print(f"\n{current_model()} answered in {time.perf_counter() - t0:.1f}s:\n{text}")
    result = verify(text, chunks)
    print(result)
    assert result["citations"], "the answer should cite at least one file"


def test_chat_endpoint_streams_a_verified_answer(corpus, db_paths, monkeypatch):
    """The full /chat path the app uses: SSE events results -> token... -> done."""
    import json

    from fastapi.testclient import TestClient

    status = server_status()
    if not status["connected"] or current_model() not in status["models"]:
        pytest.skip("OVMS not running, or it doesn't serve the configured model")
    monkeypatch.setattr(indexer_mod, "embed", REAL_EMBED)
    monkeypatch.setattr(vs_mod, "embed", REAL_EMBED)
    from app.config import settings
    from app.main import app

    db, vdb = db_paths
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "vector_db_dir", vdb)
    indexer_mod.index_folder(corpus, db, vdb)

    t0 = time.perf_counter()
    with TestClient(app, base_url="http://127.0.0.1:8756") as client:
        r = client.post("/chat", json={"message": "What is the invoice total for Acme Corp?", "mode": "files"}, headers={"Authorization": "Bearer test-token"})
    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1].removeprefix("data: ")))
        for block in r.text.strip().split("\n\n")
    ]
    kinds = [k for k, _ in events]
    print(f"\n/chat took {time.perf_counter() - t0:.1f}s, {kinds.count('token')} tokens")
    assert kinds[:2] == ["route", "results"] and kinds[-1] == "done" and "token" in kinds
    done = events[-1][1]
    assert done["citations"], done["answer"]

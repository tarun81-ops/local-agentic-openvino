"""FILES ONLY mode refuses instead of answering from general knowledge, and a file dropped on the chat is read live
(OCR for images), answered with a page citation, never indexed unless kept, and refused if it looks like credentials."""
import json

import pytest
from fastapi.testclient import TestClient

from app.api import routes_chat
from app.config import settings
from app.core import pc_access
from app.db import sqlite_fts
from app.main import app
from tests.conftest import CORPUS

AUTH = {"Authorization": "Bearer test-token"}
CHUNK = {"path": "C:/d/invoice.pdf", "name": "invoice.pdf", "loc_kind": "page", "loc_no": 1, "match": "keyword",
         "snippet": "total", "text": "The invoice total for Acme Corp is $4,250."}


def _events(body: str):
    return [(m.split("\n", 1)[0].removeprefix("event: "), json.loads(m.split("\n", 1)[1].removeprefix("data: "))) for m in body.strip().split("\n\n")]


@pytest.fixture
def client(monkeypatch):
    real = pc_access.config
    monkeypatch.setattr(pc_access, "config", lambda: {**real(), "enabled": False})  # dropped files must not need PC access
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        yield c


def _model(*parts):
    def answer(question, chunks, history=None, model=None, style=""):
        yield from parts
    return answer


def _ask(client, **body):
    return _events(client.post("/chat", json=body, headers=AUTH).text)


# ---------- FILES ONLY ----------

def test_files_only_with_nothing_found_reports_no_results(client, monkeypatch):
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: {"results": [], "semantic": False})
    ev = _ask(client, message="What is the capital of France?", mode="files_only")
    assert ev[-1][0] == "done" and ev[-1][1]["no_results"] and "token" not in [k for k, _ in ev]


def test_files_only_replaces_an_answer_from_general_knowledge(client, monkeypatch):
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: {"results": [dict(CHUNK)], "semantic": True})
    monkeypatch.setattr(routes_chat, "answer_stream", _model("The capital of France is Paris."))
    ev = _ask(client, message="What is the capital of France?", mode="files_only")
    tokens = [d["delta"] for k, d in ev if k == "token"]
    assert tokens == [routes_chat.FILES_ONLY_REFUSAL]  # "Paris" was never shown, not even while streaming
    assert ev[-1][1]["answer"] == routes_chat.FILES_ONLY_REFUSAL and ev[-1][1]["citations"] == []


def test_files_only_keeps_an_answer_the_files_back(client, monkeypatch):
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: {"results": [dict(CHUNK)], "semantic": True})
    monkeypatch.setattr(routes_chat, "answer_stream", _model("Acme Corp owes $4,250 ", "(invoice.pdf, page 1)."))
    ev = _ask(client, message="invoice total?", mode="files_only")
    done = ev[-1][1]
    assert done["answer"].startswith("Acme Corp owes $4,250") and done["citations"][0]["verified"]
    assert [d["delta"] for k, d in ev if k == "token"] == [done["answer"]]  # shown once, after the check


def test_files_only_rule():
    unsupported = {"citations": [{"verified": False}], "uncited": ["x"], "all_verified": False}
    assert routes_chat.files_only("Paris.", unsupported)[0] == routes_chat.FILES_ONLY_REFUSAL
    assert routes_chat.files_only("Paris.", {"citations": [], "uncited": []})[0] == routes_chat.FILES_ONLY_REFUSAL
    assert routes_chat.files_only("The files don't mention it.", unsupported)[0] == "The files don't mention it."
    backed = {"citations": [{"verified": True}], "uncited": [], "all_verified": True}
    assert routes_chat.files_only("4250 (a.pdf, page 1)", backed)[0] == "4250 (a.pdf, page 1)"


# ---------- a file dropped on the chat ----------

def _indexed(path) -> bool:
    conn = sqlite_fts.connect(settings.db_path)
    try:
        return conn.execute("SELECT 1 FROM files WHERE path = ?", (str(path),)).fetchone() is not None
    finally:
        conn.close()


def _corpus_pdf(tmp_path):
    pdf = tmp_path / "invoice_notes.pdf"
    pdf.write_bytes((CORPUS / "invoice_notes.pdf").read_bytes())
    return pdf


def _cite_first(question, chunks, history=None, model=None, style=""):
    c = chunks[0]
    yield f"{c['text'][:60]} ({c['name']}, page {c['loc_no']})."


def test_a_dropped_pdf_is_answered_with_a_page_citation_and_not_indexed(client, monkeypatch, tmp_path):
    pdf = _corpus_pdf(tmp_path)
    seen = {}

    def answer(question, chunks, history=None, model=None, style=""):
        seen["chunks"] = chunks
        yield from _cite_first(question, chunks)

    monkeypatch.setattr(routes_chat, "answer_stream", answer)
    done = _ask(client, message="What is the invoice total?", attach=[str(pdf)], mode="files")[-1][1]
    assert seen["chunks"] and all(c["path"] == str(pdf) and c["loc_kind"] == "page" for c in seen["chunks"])
    assert done["citations"] and done["citations"][0]["verified"]
    assert not _indexed(pdf)  # read for this answer only


def test_a_dropped_image_is_read_with_ocr(client, monkeypatch, tmp_path):
    from app.core.parsers import image_parser

    if not image_parser.available():
        pytest.skip("OCR (rapidocr-onnxruntime) is not installed")
    import fitz

    png = tmp_path / "invoice photo.png"
    with fitz.open(CORPUS / "invoice_notes.pdf") as doc:
        doc[0].get_pixmap(dpi=150).save(png)
    seen = {}

    def answer(question, chunks, history=None, model=None, style=""):
        seen["text"] = " ".join(c["text"] for c in chunks)
        yield "ok"

    monkeypatch.setattr(routes_chat, "answer_stream", answer)
    _ask(client, message="What is the invoice total?", attach=[str(png)], mode="files")
    assert "4,250" in seen["text"] or "4250" in seen["text"]


def test_a_dropped_credentials_file_is_refused(client, monkeypatch, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("API_TOKEN=abc123", encoding="utf-8")

    def never(*a, **k):
        raise AssertionError("the model must not see it")

    monkeypatch.setattr(routes_chat, "answer_stream", never)
    ev = _ask(client, message="what is in this file?", attach=[str(secret)])
    text = "".join(d["delta"] for k, d in ev if k == "token")
    assert "credentials" in text and "abc123" not in json.dumps(ev)


def test_a_follow_up_reads_the_dropped_file_again(client, monkeypatch, tmp_path):
    pdf = _corpus_pdf(tmp_path)
    monkeypatch.setattr(routes_chat, "answer_stream", _cite_first)
    first = _ask(client, message="What is the invoice total?", attach=[str(pdf)], mode="files")
    conv = next(d["id"] for k, d in first if k == "conversation")
    again = _ask(client, message="recheck this pdf", conversation_id=conv)
    assert any(k == "results" and d["results"] and d["results"][0]["path"] == str(pdf) for k, d in again)


def test_keep_in_index(client, tmp_path, monkeypatch):
    pdf = _corpus_pdf(tmp_path)
    monkeypatch.setattr(settings, "store_file_text", False)
    r = client.post("/files/keep", json={"path": str(pdf)}, headers=AUTH)
    assert r.status_code == 409 and "names only" in r.json()["detail"]  # names-only: nothing is ever stored
    monkeypatch.setattr(settings, "store_file_text", True)
    assert client.post("/files/keep", json={"path": str(pdf)}, headers=AUTH).status_code == 200
    assert _indexed(pdf)
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN KEY-----", encoding="utf-8")
    assert client.post("/files/keep", json={"path": str(secret)}, headers=AUTH).status_code == 400

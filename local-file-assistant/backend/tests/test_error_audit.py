"""Error audit (Oct 2026): one test per kind of wrong answer the app gave or can give, from the app's own chat
history and log, and from published RAG failure taxonomies (Barnett et al. 2024, "Seven failure points"):

  A  wrong person's document                 (covered in test_whose_document.py)
  B  vague answer: a marksheet "summary" with no numbers in it          (FP6 wrong specificity / FP4 not extracted)
  C  a number in the answer that is in none of the files read           (FP1 answering anyway)
  D  one file holds class 10 AND class 12: the wrong page is read       (FP3 not in context)
  E  password-protected PDFs (e-Aadhaar, PAN) fail with a code error    (ingestion)
  F  phone / cloud files (CrossDevice) fail with "[Errno 22]" and a traceback every scan
  G  the model server is not running: the user sees "Connection error"
  H  too many passages for the model's context window                  (Ollama cuts the prompt silently)
  I  the model wraps its JSON in ``` fences or <think> text             (planning and choosing fall back)
  J  "this is not mine" is searched as a new question
"""
import json
import re

import pytest

from app.api import routes_chat
from app.config import settings
from app.core import pc_access, personalize
from app.core.assistant import file_agent
from app.core.chunking import chunk_located

TENTH = ("CENTRAL BOARD OF SECONDARY EDUCATION SECONDARY SCHOOL EXAMINATION (CLASS X) 2023 MARKS STATEMENT\n"
         "Roll No 141006\nName of Candidate TARUN VERMA\nSchool JNV MANA CAMP\n"
         "184 ENGLISH LNG & LIT. 088 EIGHTY EIGHT B1\n002 HINDI COURSE-A 092 NINETY TWO A1\n041 MATHEMATICS STANDARD 085 EIGHTY FIVE B1\n"
         "086 SCIENCE 090 NINETY A2\n087 SOCIAL SCIENCE 080 EIGHTY B1\nResult PASS")
TWELFTH = ("CENTRAL BOARD OF SECONDARY EDUCATION SENIOR SCHOOL CERTIFICATE EXAMINATION (CLASS XII) 2025 MARKS STATEMENT\n"
           "Roll No 26139847\nName of Candidate TARUN VERMA\n"
           "301 ENGLISH CORE 081 EIGHTY ONE B1\n042 PHYSICS 070 020 090 NINETY A2\n043 CHEMISTRY 068 020 088 EIGHTY EIGHT A2\n"
           "041 MATHEMATICS 077 EIGHTY SEVEN B1\n083 COMPUTER SCIENCE 095 NINETY FIVE A1\nResult PASS")


def _chunk(path, text, page=1):
    return {"path": str(path), "name": path.name if hasattr(path, "name") else str(path).split("/")[-1], "loc_kind": "page",
            "loc_no": page, "chunk_no": page, "text": text, "snippet": text[:80], "match": "pc"}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    """POST /chat with a stand-in model. Returns ask(message, model_reply) -> (reply text, done event)."""
    from fastapi.testclient import TestClient

    from app.core.assistant import chat_recall
    from app.db import assistant_db
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "index.db")
    assistant_db.migrate()
    monkeypatch.setattr(chat_recall, "schedule_embed", lambda: None)
    monkeypatch.setattr(routes_chat, "_recall", lambda m: [])
    monkeypatch.setattr(routes_chat.pc_access, "enabled", lambda: False)
    client = TestClient(app, base_url="http://127.0.0.1:8756")
    state = {"conv": None, "results": []}
    monkeypatch.setattr(routes_chat, "hybrid_search", lambda *a, **k: {"results": list(state["results"]), "semantic": False})

    def ask(message, model_reply="", results=None):
        if results is not None:
            state["results"] = results

        def answer(prompt, chunks, history, **k):
            state["prompt"], state["chunks"] = prompt, chunks
            yield model_reply
            return False

        monkeypatch.setattr(routes_chat, "answer_stream", answer)
        body = {"message": message, **({"conversation_id": state["conv"]} if state["conv"] else {})}
        text = client.post("/chat", json=body, headers={"Authorization": "Bearer test-token"}).text
        if "event: conversation" in text:
            state["conv"] = json.loads(text.split("event: conversation\ndata: ")[1].split("\n")[0])["id"]
        reply = "".join(json.loads(line[6:]).get("delta", "") for block in text.split("\n\n") if block.startswith("event: token")
                        for line in block.split("\n") if line.startswith("data: "))
        done = json.loads(text.split("event: done\ndata: ")[1].split("\n")[0]) if "event: done" in text else {}
        errors = [json.loads(b.split("data: ", 1)[1])["detail"] for b in text.split("\n\n") if b.startswith("event: error")]
        return reply, done, errors

    ask.state = state
    return ask


# ---------- B: a summary must carry the facts ----------

def test_B_marksheet_summary_states_the_numbers_not_just_that_they_exist(chat, tmp_path):
    f = tmp_path / "class 10.pdf"
    reply, done, _ = chat("summarise my class 10 result",
                          model_reply="The class 10 result for TARUN VERMA shows his roll number, school and grades (class 10.pdf, page 1).",
                          results=[_chunk(f, TENTH)])
    assert "141006" in reply                      # the roll number itself
    assert "Hindi 92" in reply or "HINDI" in reply.upper() and "92" in reply
    assert "87" in reply and "%" in reply          # 435 / 500 = 87%, worked out by the app


# ---------- C: numbers the files don't contain ----------

def test_C_a_number_that_is_in_no_file_is_flagged(chat, tmp_path):
    f = tmp_path / "10th&12th.pdf"
    reply, done, _ = chat("what is my school name", model_reply="Your roll number is 555123 (10th&12th.pdf, page 1).",
                          results=[_chunk(f, TENTH)])
    assert "555123" in reply and re.search(r"couldn.t find 555123|not in the file", reply)


def test_C_numbers_that_are_in_the_file_are_not_flagged(chat, tmp_path):
    f = tmp_path / "10th&12th.pdf"
    reply, _, _ = chat("what is my school name", model_reply="Your school is JNV MANA CAMP and your roll no is 141006 (10th&12th.pdf, page 1).",
                       results=[_chunk(f, TENTH)])
    assert "couldn't find" not in reply


# ---------- D: class 10 and class 12 in one file ----------

def test_D_class_12_question_reads_the_class_12_page_of_a_combined_file(tmp_path, monkeypatch):
    f = tmp_path / "10th&12th.pdf"
    f.write_bytes(b"x")
    monkeypatch.setattr(file_agent, "_parsers", lambda: {".pdf": lambda p: chunk_located("page", 1, TENTH) + chunk_located("page", 2, TWELFTH)})
    monkeypatch.setattr(pc_access, "can_read", lambda p, explicit=False: (True, ""))
    got, problem = file_agent.read_chunks(str(f), "what is my class 12 roll no", ["class", "12", "roll"])
    pages = {c["loc_no"] for c in got}
    assert 2 in pages and 1 not in pages


def test_D_class_10_question_reads_the_class_10_page(tmp_path, monkeypatch):
    f = tmp_path / "10th&12th.pdf"
    f.write_bytes(b"x")
    monkeypatch.setattr(file_agent, "_parsers", lambda: {".pdf": lambda p: chunk_located("page", 1, TENTH) + chunk_located("page", 2, TWELFTH)})
    monkeypatch.setattr(pc_access, "can_read", lambda p, explicit=False: (True, ""))
    got, _ = file_agent.read_chunks(str(f), "my 10th roll number", ["10th", "roll"])
    assert {c["loc_no"] for c in got} == {1}


# ---------- E: password-protected PDFs ----------

def _locked_pdf(path, password="TARU2007"):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Unique Identification Authority of India  Aadhaar  TARUN VERMA  DOB 01/01/2007")
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "x")
    doc.close()


def test_E_a_locked_pdf_says_it_needs_a_password(tmp_path, monkeypatch):
    f = tmp_path / "EAadhaarcard.pdf"
    _locked_pdf(f)
    monkeypatch.setattr(pc_access, "can_read", lambda p, explicit=False: (True, ""))
    got, problem = file_agent.read_chunks(str(f), "my aadhaar", ["aadhaar"])
    assert got == [] and "password" in problem.lower()
    assert "ValueError" not in problem


def test_E_the_user_can_unlock_it_in_chat_and_the_password_is_not_saved(chat, tmp_path, monkeypatch):
    f = tmp_path / "EAadhaarcard.pdf"
    _locked_pdf(f)
    monkeypatch.setattr(pc_access, "can_read", lambda p, explicit=False: (True, ""))
    monkeypatch.setattr(routes_chat.pc_access, "enabled", lambda: True)

    def gather(question, content_hits=None, **k):
        got, problem = file_agent.read_chunks(str(f), question, ["aadhaar"])
        yield {"step": "skip" if problem else "read", "detail": problem or "read"}
        return {"chunks": got, "files": [str(f)] if got else [], "locked": [str(f)] if "password" in problem.lower() else []}

    monkeypatch.setattr(file_agent, "gather", gather)
    reply, _, _ = chat("what is my aadhaar dob", model_reply="", results=[])
    assert "password" in reply.lower() and "EAadhaarcard.pdf" in reply
    reply, _, _ = chat("the password is TARU2007", model_reply="Your date of birth is 01/01/2007 (EAadhaarcard.pdf, page 1).")
    assert "01/01/2007" in reply
    assert "TARU2007" not in json.dumps(personalize.get_all())
    assert "TARU2007" not in chat.state["prompt"]


# ---------- F: files on the phone (CrossDevice) / not downloaded from the cloud ----------

def test_F_an_unreachable_file_gets_a_plain_reason_not_errno(tmp_path, monkeypatch):
    f = tmp_path / "UNIT-1 CPL notes.pdf"
    f.write_bytes(b"x")
    monkeypatch.setattr(pc_access, "can_read", lambda p, explicit=False: (True, ""))

    def boom(p):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(file_agent, "_parsers", lambda: {".pdf": boom})
    got, problem = file_agent.read_chunks(str(f), "cpl notes", ["cpl"])
    assert got == [] and "Errno" not in problem and re.search(r"phone|offline|not downloaded|can't be opened", problem)


def test_F_the_indexer_logs_one_line_not_a_traceback(tmp_path, monkeypatch, caplog):
    from app.core import indexer

    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    indexer._record_error(f, OSError(22, "Invalid argument"))
    text = caplog.text
    assert "Traceback" not in text and "skipped" in text and "Errno" not in text


# ---------- G: the model server is down ----------

def test_G_model_down_says_what_is_happening(chat, tmp_path, monkeypatch):
    """OpenVINO build: core/llm/ovms_runner starts the server and the message says so (not "Connection error")."""
    import httpx
    import openai

    from app.core.llm import ovms_runner

    def down(*a, **k):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://127.0.0.1:8000/v3/chat/completions"))
        yield  # pragma: no cover

    monkeypatch.setattr(routes_chat, "chat_stream", down)
    monkeypatch.setattr(routes_chat, "answer_stream", down)
    monkeypatch.setattr(ovms_runner, "ensure_running_async", lambda: None)
    monkeypatch.setattr(ovms_runner, "status", lambda: {"status": "stopped", "detail": ""})
    from fastapi.testclient import TestClient

    from app.main import app

    text = TestClient(app, base_url="http://127.0.0.1:8756").post("/chat", json={"message": "hello there", "mode": "chat"},
                                                               headers={"Authorization": "Bearer test-token"}).text
    detail = json.loads(text.split("event: error\ndata: ")[1].split("\n")[0])["detail"]
    assert "OpenVINO" in detail and "Connection error" not in detail


# ---------- H: context window ----------

def test_H_passages_are_trimmed_to_fit_the_context_window():
    from app.core.llm import answerer

    big = [{"path": f"/f/{i}.pdf", "loc_kind": "page", "loc_no": 1, "text": "word " * 2000} for i in range(12)]
    msgs = answerer._messages("what is the total", big, [{"role": "user", "content": "hi " * 3000}, {"role": "assistant", "content": "ok"}])
    size = sum(len(m["content"]) for m in msgs)
    assert size <= settings.max_prompt_chars
    assert "what is the total" in msgs[-1]["content"]  # the question is never cut


# ---------- I: JSON from a small model ----------

@pytest.mark.parametrize("raw", ['```json\n{"pick": [2]}\n```', '<think>hmm</think>{"pick": [2]}', 'Sure! {"pick": [2]} hope that helps'])
def test_I_json_is_found_inside_fences_or_chatter(raw, monkeypatch):
    from app.core.llm import client

    class R:
        def __init__(self, text):
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**k):
                    return R(raw)

    monkeypatch.setattr(client, "get_client", lambda: Fake)
    assert client.llm_json("x") == {"pick": [2]}


def test_I_thinking_never_reaches_the_user():
    from app.core.llm.client import ThinkFilter

    f = ThinkFilter()
    out = "".join(f.feed(x) for x in ["<thi", "nk>let me see</th", "ink>Your roll no is 5."]) + f.flush()
    assert out == "Your roll no is 5."


# ---------- J: "not mine" ----------

def test_J_not_mine_re_asks_without_that_file(chat, tmp_path):
    a, b = tmp_path / "12th.pdf", tmp_path / "krish22 12 result.pdf"
    first = [_chunk(a, "Roll No 12626405 class 12"), _chunk(b, "Roll No 7777777 class 12")]
    chat("what is my class 12 roll no.", model_reply="Your roll number is 12626405 (12th.pdf, page 1).", results=first)
    reply, done, _ = chat("this is not mine", model_reply="Your roll number is 7777777 (krish22 12 result.pdf, page 1).")
    assert all(c["path"] != str(a) for c in chat.state["chunks"])


def test_a_model_that_is_still_loading_is_said_in_plain_words():
    from app.api import routes_chat

    class NotFound(Exception):
        status_code = 404

    msg = routes_chat._model_error(NotFound("Error code: 404 - {'error': 'Mediapipe graph definition with requested name is not found'}"))
    assert "still loading" in msg and "Mediapipe" not in msg and "404" not in msg

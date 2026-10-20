"""IFSC codes are checked against the bank named; 'which file?' reuses the previous answer's source."""
import json

from app.core import facts
from app.core.llm.verifier import verify


def chunk(path, text, no=1):
    return {"path": path, "name": path.split("/")[-1], "loc_kind": "page", "loc_no": no, "chunk_no": 0, "text": text, "snippet": text[:50], "match": "pc"}


SBI = chunk("C:/u/Downloads/passbook k (1).pdf", "State Bank of India Account No 123 IFSC Code : SBIN0004572 Branch Raipur")
BOB = chunk("C:/u/Documents/bob passbook.pdf", "Bank of Baroda Savings IFSC BARB0RAIPUR Branch Raipur")


def test_finds_ifsc_and_repairs_an_ocr_o_for_zero():
    assert facts.find_ifsc("IFSC: SBIN0004572 and some other text 12345") == ["SBIN0004572"]
    assert facts.find_ifsc("IFSC BARBOKHAMHA") == ["BARB0KHAMHA"]  # 0 read as O


def test_bank_named():
    assert facts.bank_named("what is IFSC code of my bank of baroda passbook") == "BARB"
    assert facts.bank_named("ifsc of my sbi account") == "SBIN"
    assert facts.bank_named("what is my ifsc") is None


def test_another_banks_code_is_never_given_as_the_answer():
    out = facts.ifsc_reply("what is IFSC code of my bank of baroda passbook", [SBI])
    assert "SBIN0004572" in out and "could not find a Bank of Baroda IFSC" in out and "won't give them as your answer" in out
    assert "The IFSC code of your" not in out


def test_matching_bank_gives_a_cited_answer():
    out = facts.ifsc_reply("what is IFSC code of my bank of baroda passbook", [SBI, BOB])
    assert out == "The IFSC code of your Bank of Baroda account is BARB0RAIPUR [bob passbook.pdf, p. 1]."
    done = verify(out, [SBI, BOB])
    assert done["citations"][0]["verified"] is True


def test_no_ifsc_anywhere_falls_through_to_the_normal_path():
    assert facts.ifsc_reply("ifsc code", [chunk("C:/u/a.pdf", "nothing here")]) is None


def test_citation_with_brackets_in_the_file_name_is_recognised():
    ans = "The code is SBIN0004572 [passbook k (1).pdf, page 1]."
    done = verify(ans, [SBI])
    assert done["citations"] and done["citations"][0]["verified"] is True and not done["uncited"]


def test_source_question_detector_needs_to_point_back_at_the_answer():
    assert facts.asks_source("can you mention the file from which you have extracted this info")
    assert facts.asks_source("which file is this from")
    assert facts.asks_source("where did you get that")
    assert not facts.asks_source("show me the statement")
    assert not facts.asks_source("find my bank of baroda passbook")


def test_source_reply_names_the_one_original_file_and_its_location():
    msgs = [
        {"role": "user", "content": "q", "meta": {}},
        {"role": "assistant", "content": "a", "meta": {"citations": [
            {"path": "C:/u/Downloads/passbook k (1).pdf", "loc_kind": "page", "loc_no": 1, "status": "checked", "verified": True, "snippet": "s"},
            {"path": "C:/u/Downloads/passbook k (1).pdf", "loc_kind": "page", "loc_no": 1, "status": "checked", "verified": True},
        ]}},
    ]
    text, cits = facts.source_reply(msgs)
    assert len(cits) == 1 and cits[0]["path"].endswith("passbook k (1).pdf") and cits[0]["snippet"] == "s"
    assert "C:/u/Downloads/passbook k (1).pdf" in text and text[cits[0]["span"][0]:cits[0]["span"][1]] == "[passbook k (1).pdf, p. 1]"
    assert facts.source_reply([{"role": "assistant", "content": "a", "meta": {}}]) is None


def test_chat_route_follow_up_does_not_search_again(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import routes_chat
    from app.config import settings
    from app.core.assistant import chat_recall, conversation
    from app.db import assistant_db
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "index.db")
    assistant_db.migrate()
    monkeypatch.setattr(chat_recall, "schedule_embed", lambda: None)
    conv = conversation.create("t")
    conversation.add_messages(conv["id"], [("user", "ifsc?", {}), ("assistant", "x", {"citations": [
        {"path": "C:/u/Downloads/passbook k (1).pdf", "loc_kind": "page", "loc_no": 1, "status": "checked", "verified": True}]})])

    def boom(*a, **k):
        raise AssertionError("a source question must not search again")

    monkeypatch.setattr(routes_chat, "hybrid_search", boom)
    r = TestClient(app, base_url="http://127.0.0.1:8756").post(
        "/chat", json={"message": "can you mention the file from which you have extracted this info", "conversation_id": conv["id"]},
        headers={"Authorization": "Bearer test-token"})
    done = json.loads(r.text.split("event: done\ndata: ")[1].split("\n")[0])
    assert r.status_code == 200 and "passbook k (1).pdf" in r.text and len(done["citations"]) == 1


# ---------- page counts and follow-ups about the same file ----------
def _pdf(path, pages):
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1} of a test book")
    doc.save(str(path))
    doc.close()
    return str(path)


def test_page_questions_are_recognised():
    for q in ["how many pages this pdf contains", "Number of pages in it?", "page count of gunaho ka devta",
              "is pdf me kitne page hai", "pages kitne hain", "how many slides are there"]:
        assert facts.asks_pages(q), q
    for q in ["what is on page 5", "summarise the first pages", "find my invoice"]:
        assert not facts.asks_pages(q), q


def test_pages_are_counted_from_the_file(tmp_path):
    book = _pdf(tmp_path / "gunaho ka devata.pdf", 3)
    text, cits = facts.pages_reply([str(tmp_path / "missing.pdf"), book])
    assert "gunaho ka devata.pdf has 3 pages" in text
    assert cits[0]["path"] == book and cits[0]["verified"]
    assert text[cits[0]["span"][0]:cits[0]["span"][1]] == "[gunaho ka devata.pdf, p. 1]"
    assert facts.pages_reply([str(tmp_path / "notes.docx")]) is None


def test_follow_ups_that_point_back_are_recognised():
    for q in ["this is wrong recheck pdf", "how many pages this pdf contains", "check again", "that's wrong",
              "galat hai", "is pdf me kya likha hai", "read it again please"]:
        assert facts.refers_back(q), q
    for q in ["find my bank passbook", "what is the invoice total", "how many pages does my resume have"]:
        assert not facts.refers_back(q), q


def test_last_files_come_from_the_latest_file_answer_only():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "meta": {"route": "files", "citations": [{"path": "C:/b.pdf"}], "files": ["C:/a.pdf", "C:/b.pdf"]}}]
    assert facts.last_files(msgs) == ["C:/b.pdf", "C:/a.pdf"]  # cited file first
    assert facts.last_files(msgs + [{"role": "assistant", "content": "hi", "meta": {"route": "chat"}}]) == []


def _chat_app(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.api import routes_chat
    from app.config import settings
    from app.core.assistant import chat_recall
    from app.db import assistant_db
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "index.db")
    assistant_db.migrate()
    monkeypatch.setattr(chat_recall, "schedule_embed", lambda: None)

    def no_search(*a, **k):
        raise AssertionError("a follow-up about the same file must not search again")

    monkeypatch.setattr(routes_chat, "hybrid_search", no_search)
    client = TestClient(app, base_url="http://127.0.0.1:8756")
    return routes_chat, lambda body: client.post("/chat", json=body, headers={"Authorization": "Bearer test-token"})


def _done(text):
    return json.loads(text.split("event: done\ndata: ")[1].split("\n")[0])


def test_page_count_follow_up_is_counted_not_guessed(monkeypatch, tmp_path):
    from app.core.assistant import conversation

    routes_chat, post = _chat_app(monkeypatch, tmp_path)
    book = _pdf(tmp_path / "gunaho ka devata.pdf", 4)
    conv = conversation.create("gunaho ke devata")
    conversation.add_messages(conv["id"], [("user", "gunaho ke devata", {}),
                                           ("assistant", "It is a novel.", {"route": "files", "citations": [], "files": [book]})])
    monkeypatch.setattr(routes_chat, "_title_chunks", lambda path, msg: [chunk(path, "page 1 of a test book")])

    def no_model(*a, **k):
        raise AssertionError("page counts must not come from the model")

    monkeypatch.setattr(routes_chat, "answer_stream", no_model)
    r = post({"message": "how many pages this pdf contains", "conversation_id": conv["id"]})
    done = _done(r.text)
    assert "has 4 pages" in done["answer"] and done["all_verified"]
    saved = conversation.get(conv["id"])["messages"][-1]["meta"]
    assert saved["files"] == [book]  # the next follow-up can stay on it too


def test_recheck_stays_on_the_previous_file(monkeypatch, tmp_path):
    from app.core.assistant import conversation

    routes_chat, post = _chat_app(monkeypatch, tmp_path)
    book = _pdf(tmp_path / "gunaho ka devata.pdf", 2)
    conv = conversation.create("t")
    conversation.add_messages(conv["id"], [("user", "how many pages", {}),
                                           ("assistant", "31 pages", {"route": "files", "citations": [], "files": [book]})])
    monkeypatch.setattr(routes_chat, "_title_chunks", lambda path, msg: [chunk(path, "Chander and Sudha")])
    seen = {}

    def fake_answer(prompt, chunks, history, model=None, style=""):
        seen["paths"] = {c["path"] for c in chunks}
        yield "It is about Chander and Sudha [gunaho ka devata.pdf, p. 1]."
        return False

    monkeypatch.setattr(routes_chat, "answer_stream", fake_answer)
    r = post({"message": "this is wrong recheck pdf", "conversation_id": conv["id"]})
    assert seen["paths"] == {book}
    assert "Same file as before: gunaho ka devata.pdf" in r.text
    assert _done(r.text)["files"] == [book]


# ---------- named pages, summaries, file size / dates / folder ----------
def test_named_pages_and_summaries_are_recognised():
    assert facts.wanted_page("what is written on page 12") == 12
    assert facts.wanted_page("12th page pe kya hai") == 12
    assert facts.wanted_page("panna 5 padho") == 5
    assert facts.wanted_page("summarise the last page") == "last"
    assert facts.wanted_page("what is the invoice total") is None
    for q in ["summarize this book", "what is this pdf about", "ye kitab kis baare me hai", "give me the key points"]:
        assert facts.asks_overview(q), q
    assert not facts.asks_overview("what is the total on the invoice")


def _parsed(pages):
    from app.core.chunking import Chunk

    return list(enumerate(Chunk(text=f"text of page {i}", loc_kind="page", loc_no=i) for i in range(1, pages + 1)))


def test_a_named_page_is_read_not_the_best_word_match():
    from app.core.assistant.file_agent import focused

    parsed = _parsed(40)
    assert [c.loc_no for _, c in focused("what is on page 12", parsed)] == [12]
    assert [c.loc_no for _, c in focused("read the last page", parsed)] == [40]


def test_a_summary_reads_the_whole_book_spread_out():
    from app.core.assistant.file_agent import focused

    got = [c.loc_no for _, c in focused("summarize this book", _parsed(40))]
    assert len(got) == 6 and got[0] == 1 and got[-1] > 30
    assert focused("what is the total", _parsed(40)) is None  # normal word ranking


def test_a_page_past_the_end_is_answered_from_the_file(tmp_path):
    book = _pdf(tmp_path / "notes.pdf", 3)
    text, cits = facts.page_out_of_range("what is on page 9", [book])
    assert text.startswith("There is no page 9.") and "has 3 pages" in text
    assert text[cits[0]["span"][0]:cits[0]["span"][1]] == "[notes.pdf, p. 1]"
    assert facts.page_out_of_range("what is on page 2", [book]) is None


def test_size_date_and_folder_come_from_the_file_system(tmp_path):
    book = _pdf(tmp_path / "gunaho ka devata.pdf", 2)
    for q in ["how big is this pdf", "when was it last modified", "where is this file saved", "iska size kitna hai", "ye kab download hua"]:
        assert facts.asks_file_info(q), q
    for q in ["what is the size of the plot in the sale deed", "how big is the hall in the rent agreement", "when was Akbar born"]:
        assert not facts.asks_file_info(q), q  # questions about the text, not the file
    text, cits = facts.file_info_reply("how big is it and where is it saved", [book])
    assert "KB" in text and str(tmp_path) in text and cits[0]["path"] == book
    text, _ = facts.file_info_reply("when was it last modified", [book])
    assert "last modified on" in text


def test_short_pronoun_follow_ups_stay_on_the_file():
    for q in ["who wrote it?", "iska author kaun hai", "isme kya likha hai", "ye kis baare me hai"]:
        assert facts.refers_back(q), q
    assert not facts.refers_back("how big is invoice.pdf, is it large?")  # names its own file
    assert not facts.refers_back("tell me everything you know about the history of the Mughal empire in India")  # long, no pointer


def test_file_size_follow_up_is_not_asked_of_the_model(monkeypatch, tmp_path):
    from app.core.assistant import conversation

    routes_chat, post = _chat_app(monkeypatch, tmp_path)
    book = _pdf(tmp_path / "gunaho ka devata.pdf", 2)
    conv = conversation.create("t")
    conversation.add_messages(conv["id"], [("user", "gunaho ke devata", {}),
                                           ("assistant", "A novel.", {"route": "files", "citations": [], "files": [book]})])
    monkeypatch.setattr(routes_chat, "_title_chunks", lambda path, msg: [chunk(path, "Chander and Sudha")])

    def no_model(*a, **k):
        raise AssertionError("file size must not come from the model")

    monkeypatch.setattr(routes_chat, "answer_stream", no_model)
    done = _done(post({"message": "iska size kitna hai", "conversation_id": conv["id"]}).text)
    assert "gunaho ka devata.pdf is" in done["answer"] and "KB" in done["answer"]


"""Fuzzy file-name matching: titles typed by sound ("gunaho ke devata"), Devanagari names, similar files,
and the chat fallback that answers from a file named in the message."""
import os
from datetime import datetime

import pytest

from app.config import settings
from app.core import find_file, fuzzy, indexer, pc_access
from tests.test_api import AUTH, _events, client  # noqa: F401  (client is a fixture)

NOW = datetime(2026, 10, 14, 12, 0)


# ---- the matcher ------------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [("gunaho", "Gunahon"), ("devata", "Devta"), ("jindagi", "Zindagi"), ("geeta", "Gita"),
                                 ("devata", "देवता"), ("gunahon", "गुनाहों"), ("elecricity", "electricity")])
def test_spelling_variants_sound_alike(a, b):
    assert fuzzy.word_sim(a, b) >= fuzzy.CUTOFF


@pytest.mark.parametrize("a,b", [("bill", "ball"), ("card", "cord"), ("2025", "2026"), ("ke", "ka"), ("cv", "cb")])
def test_different_words_do_not(a, b):
    assert fuzzy.word_sim(a, b) < fuzzy.CUTOFF


@pytest.mark.parametrize("name", ["Gunahon Ka Devta.pdf", "GunahonKaDevta.pdf", "गुनाहों का देवता.pdf", "Gunahon_Ka_Devta - Dharamvir Bharati.pdf"])
def test_title_typed_by_sound_matches_every_spelling_of_the_name(name):
    score, hits = fuzzy.name_match(fuzzy.title_words("gunaho ke devata"), name)
    assert score >= 0.8 and len(hits) == 2


def test_a_word_is_not_found_across_word_boundaries():
    assert fuzzy.name_match(["letter"], "Electricity_Bill-Jun2026.txt")[0] == 0.0


def test_title_words_drop_commands_and_glue():
    assert fuzzy.title_words("summarise the book gunaho ke devata") == ["gunaho", "devata"]


def test_vocab_expands_a_typed_word_to_real_spellings():
    v = fuzzy.Vocab(["gunahon", "devta", "zindagi", "electricity", "bill"])
    assert v.close("devata") == ["devta"] and v.close("jindagi") == ["zindagi"] and v.close("bil") == []


def test_similar_names():
    assert fuzzy.name_similarity("Electricity_Bill-Sep2026.pdf", "Electricity_Bill-Jun2026.pdf") >= 0.7
    assert fuzzy.name_similarity("notes.txt", "Offer_Letter.txt") == 0.0


# ---- Find, Search, similar files --------------------------------------------------------------

@pytest.fixture
def books(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "index.db")
    monkeypatch.setattr(settings, "vector_db_dir", tmp_path / "data" / "lancedb")
    (tmp_path / "data").mkdir()
    root = tmp_path / "Documents"
    (root / "Books").mkdir(parents=True)
    (root / "Bills").mkdir()
    files = {
        "Books/Gunahon_Ka_Devta.txt": "Chander and Sudha in Allahabad. A novel by Dharamvir Bharati about love and sacrifice.",
        "Books/Godan.txt": "Hori and Dhania, a farmer's life. Premchand.",
        "Bills/Electricity_Bill-Sep2026.txt": "CSPDCL electricity bill. Amount due 1,240.",
        "Bills/Electricity_Bill-Jun2026.txt": "CSPDCL electricity bill. Amount due 980.",
    }
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    indexer.index_folder(root, settings.db_path, settings.vector_db_dir)
    pc_access.set_config(enabled=False)
    return root


def test_find_gets_a_title_typed_by_sound(books):
    out = find_file.find("gunaho ke devata", now=NOW)
    assert out["results"][0]["name"] == "Gunahon_Ka_Devta.txt"
    assert any("sounds like" in w for w in out["results"][0]["why"])


def test_find_suggests_similar_files(books):
    out = find_file.find("electricity bill september", now=NOW)
    assert out["results"][0]["name"] == "Electricity_Bill-Sep2026.txt"
    shown = {r["name"] for r in out["results"]}
    assert all(r["name"] not in shown for r in out["similar"])


def test_similar_files_by_name_folder_and_type(books):
    sep = str(books / "Bills" / "Electricity_Bill-Sep2026.txt")
    sim = find_file.similar_files(sep)
    assert sim[0]["name"] == "Electricity_Bill-Jun2026.txt" and "similar name" in sim[0]["why"]
    assert all(r["path"] != sep for r in sim)


def test_name_matches_and_strict_title_hits(books):
    assert [r["name"] for r in find_file.name_matches("gunaho ke devata")] == ["Gunahon_Ka_Devta.txt"]
    assert find_file.title_hits("summarise gunaho ke devta")[0]["name"] == "Gunahon_Ka_Devta.txt"
    assert find_file.title_hits("hii") == [] and find_file.title_hits("what is OVMS") == []
    assert find_file.title_hits("tell me about the history of the roman empire in detail please") == []


def test_similar_endpoint_refuses_files_outside_the_index(client, books, tmp_path):  # noqa: F811
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    assert client.get("/find/similar", params={"path": str(outside)}, headers=AUTH).status_code == 404
    ok = client.get("/find/similar", params={"path": str(books / "Books" / "Godan.txt")}, headers=AUTH)
    assert ok.status_code == 200 and "results" in ok.json()


def test_search_lists_files_whose_names_sound_alike(client, books):  # noqa: F811
    data = client.get("/search", params={"q": "gunaho ke devata"}, headers=AUTH).json()
    names = [r["name"] for r in data["results"]] + [r["name"] for r in data["name_matches"]]
    assert "Gunahon_Ka_Devta.txt" in names


# ---- chat ---------------------------------------------------------------------------------

def test_chat_answers_from_a_file_named_by_sound(client, books, monkeypatch):  # noqa: F811
    from app.api import routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda q, root=None, limit=6, within=None: {"results": [], "semantic": False})
    seen = {}

    def answer(question, chunks, history=None, model=None, style=""):
        seen["paths"] = {c["path"] for c in chunks}
        yield "Chander and Sudha (Gunahon_Ka_Devta.txt, page 1)."
        return False

    def no_chat(*a, **k):
        raise AssertionError("must not fall back to a general answer")

    monkeypatch.setattr(routes_chat, "answer_stream", answer)
    monkeypatch.setattr(routes_chat, "chat_stream", no_chat)
    events = _events(client.post("/chat", json={"message": "gunaho ke devata"}, headers=AUTH).text)
    routes = [d["route"] for k, d in events if k == "route"]
    assert routes[-1] == "files"
    assert seen["paths"] == {str(books / "Books" / "Gunahon_Ka_Devta.txt")}
    assert any(k == "pc_step" and "Gunahon_Ka_Devta.txt" in d["detail"] for k, d in events)


def test_small_talk_still_goes_to_chat(client, books, monkeypatch):  # noqa: F811
    from app.api import routes_chat

    monkeypatch.setattr(routes_chat, "hybrid_search", lambda q, root=None, limit=6, within=None: {"results": [], "semantic": False})
    monkeypatch.setattr(routes_chat, "chat_stream", lambda *a, **k: iter(["Hi!"]))
    events = _events(client.post("/chat", json={"message": "hii"}, headers=AUTH).text)
    assert [d["route"] for k, d in events if k == "route"][-1] == "chat"

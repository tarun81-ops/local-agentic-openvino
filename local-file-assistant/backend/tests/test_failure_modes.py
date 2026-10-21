"""Failure modes from the research pass: Hindi words split by the tokenizers, Qwen3-Embedding query instruction,
sampling defaults, leaked <think> text, over-long prompts, unparseable JSON, and Hinglish questions that share
no word with the file."""
import pytest

from app.config import settings


# ---------- Hindi words stay whole ----------
def test_words_keep_devanagari_vowel_signs():
    from app.core.search.fts_search import terms
    from app.core.words import strip_non_word, words

    assert words("गुनाहों का देवता, विद्यालय") == ["गुनाहों", "का", "देवता", "विद्यालय"]
    assert terms("गुनाहों का देवता") == ["गुनाहों", "का", "देवता"]
    assert strip_non_word("देवता!") == "देवता"


def test_content_index_matches_whole_hindi_words_only(tmp_path):
    from app.core.search.fts_search import build_match
    from app.db import sqlite_fts

    conn = sqlite_fts.connect(tmp_path / "index.db")
    try:
        conn.execute("INSERT INTO chunks(path, loc_kind, loc_no, chunk_no, text) VALUES ('a.pdf', 'page', 1, 0, 'गुनाहों का देवता उपन्यास')")
        conn.execute("INSERT INTO chunks(path, loc_kind, loc_no, chunk_no, text) VALUES ('b.pdf', 'page', 1, 0, 'दवा और दुकान')")

        def hits(q):
            return [r[0] for r in conn.execute("SELECT path FROM chunks WHERE chunks MATCH ?", (build_match(q),))]

        assert hits("देवता") == ["a.pdf"]
        assert hits("द") == []  # a lone letter fragment no longer matches every Hindi text
    finally:
        conn.close()


def test_memory_and_chat_recall_indexes_are_rebuilt_with_the_hindi_tokenizer(monkeypatch, tmp_path):
    from app.db import assistant_db

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assistant_db.migrate()
    conn = assistant_db.connect()
    try:
        sql = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE name IN ('memories_fts', 'chat_index_fts')").fetchall())
    finally:
        conn.close()
    assert len(sql) == 2 and all("Mn Mc" in s for s in sql.values())


def test_keywords_for_passage_picking_keep_hindi_words():
    from app.core import pc_access

    assert pc_access.keywords("गुनाहों का देवता me Chander")[:3] == ["गुनाहों", "का", "देवता"]


# ---------- embeddings ----------
def test_queries_get_the_qwen3_instruction_and_documents_do_not(monkeypatch):
    from app.core.search import vector_search

    sent = []
    monkeypatch.setattr(vector_search, "embed", lambda texts, timeout=120: sent.extend(texts) or [[0.0]] * len(texts))
    vector_search.embed_query(["iska author kaun hai"])
    assert sent[-1].startswith("Instruct: ") and sent[-1].endswith("\nQuery:iska author kaun hai")
    monkeypatch.setattr(settings, "embed_query_instruct", "")
    vector_search.embed_query(["x"])
    assert sent[-1] == "x"


def test_embedding_inputs_are_capped(monkeypatch):
    import json

    import httpx

    from app.core.search import vector_search
    from tests.conftest import REAL_EMBED

    seen = {}

    def handler(request):
        seen["input"] = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    real = httpx.Client
    monkeypatch.setattr(vector_search.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "embed_max_chars", 10)
    REAL_EMBED(["a" * 50])
    assert seen["input"] == ["a" * 10]


# ---------- chat requests ----------
def test_every_chat_call_gets_non_thinking_sampling_unless_overridden():
    from app.core.llm.client import _without_thinking

    calls = []
    create = _without_thinking(lambda *a, **kw: calls.append(kw))
    create(model="chat", messages=[])
    kw = calls[-1]
    assert (kw["temperature"], kw["top_p"], kw["presence_penalty"]) == (0.7, 0.8, 1.0)
    assert kw["extra_body"]["top_k"] == 20 and kw["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    create(model="chat", messages=[], temperature=0.2)
    assert calls[-1]["temperature"] == 0.2


def test_leaked_thinking_is_removed_from_text_and_streams():
    from app.core.llm.client import ThinkFilter, strip_think

    assert strip_think("<think>hmm, the user wants</think>\n\nThe total is 450.") == "The total is 450."
    assert strip_think("<think>ran out of tokens while thinking") == ""
    f = ThinkFilter()
    pieces = ["<thi", "nk>let me see", " more</th", "ink>\n\nThe ", "answer <is> 7."]
    assert "".join(f.feed(p) for p in pieces) + f.flush() == "The answer <is> 7."
    g = ThinkFilter()
    assert "".join(g.feed(p) for p in ["No ", "thinking here."]) + g.flush() == "No thinking here."


class _TooLong(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - prompt of 9000 tokens exceeds model max length"


def test_an_over_long_prompt_is_retried_with_fewer_passages(monkeypatch):
    from app.core.llm import answerer

    sizes = []

    def fake_stream(messages, model):
        n = messages[-1]["content"].count("[f")
        sizes.append((n, len(messages)))
        if n > 2:
            raise _TooLong()
        yield "ok"
        return False

    monkeypatch.setattr(answerer, "_stream", fake_stream)
    chunks = [{"path": f"f{i}.pdf", "loc_kind": "page", "loc_no": 1, "text": "t"} for i in range(6)]
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert list(answerer.answer_stream("question", chunks, history)) == ["ok"]
    assert sizes[0][0] == 6 and sizes[-1][0] <= 2 and sizes[-1][1] == 2  # fewer passages, history dropped


def test_other_errors_are_not_retried(monkeypatch):
    from app.core.llm import answerer

    def boom(messages, model):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(answerer, "_stream", boom)
    with pytest.raises(RuntimeError):
        list(answerer.answer_stream("q", [{"path": "a.pdf", "loc_kind": "page", "loc_no": 1, "text": "t"}] * 4))


def test_json_answers_are_parsed_even_with_fences_or_thinking(monkeypatch):
    from types import SimpleNamespace

    from app.core.llm import client

    replies = iter(['<think>plan</think>```json\n{"moves": [1, 2]}\n```', 'Sure! {"ok": true} Hope that helps', "no json at all"])

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(replies)))])

    monkeypatch.setattr(client, "get_client", lambda local=False: Fake)
    assert client.llm_json("p") == {"moves": [1, 2]}
    assert client.llm_json("p") == {"ok": True}
    assert client.llm_json("p") == {}


# ---------- Hinglish question, no shared words ----------
def test_passages_are_picked_by_meaning_when_no_word_matches(monkeypatch):
    from app.core.assistant import file_agent
    from app.core.chunking import Chunk
    from app.core.search import vector_search

    parsed = list(enumerate(Chunk(loc_kind="page", loc_no=i, text=f"chapter text number {i}") for i in range(1, 31)))
    # page 20 is the one that "means" the same as the question
    monkeypatch.setattr(vector_search, "embed", lambda texts, timeout=120: [[1.0, 0.0] if "number 20" in t else [0.0, 1.0] for t in texts])
    monkeypatch.setattr(vector_search, "embed_query", lambda texts, timeout=120: [[1.0, 0.0]])
    picked = file_agent.by_meaning("Sudha ki shaadi kisse hui", parsed)
    assert 20 in [c.loc_no for _, c in picked]


def test_meaning_pick_falls_back_quietly_without_an_embedder(monkeypatch):
    from app.core.assistant import file_agent
    from app.core.chunking import Chunk
    from app.core.search import vector_search

    def down(*a, **k):
        raise ConnectionError("OVMS not running")

    monkeypatch.setattr(vector_search, "embed", down)
    assert file_agent.by_meaning("kuch bhi", [(0, Chunk(loc_kind="page", loc_no=1, text="x"))]) is None

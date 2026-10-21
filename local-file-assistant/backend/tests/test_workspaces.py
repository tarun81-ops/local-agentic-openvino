"""Workspaces: chat search stays inside the workspace's folders (index search and the live file agent), chats are
listed per workspace, existing chats land in "All files", and deleting a workspace never deletes a file."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes_chat
from app.config import settings
from app.core import pc_access
from app.core.assistant import conversation, file_agent, workspaces
from app.core.search import fts_search, hybrid
from app.core.search.hybrid import hybrid_search
from app.db import sqlite_fts
from app.main import app
from tests.test_api import _add_and_wait

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:  # the lifespan runs the migrations
        yield c


@pytest.fixture
def two_folders(tmp_path):
    a, b = tmp_path / "College", tmp_path / "Office"
    for d, who in ((a, "college"), (b, "office")):
        d.mkdir()
        (d / f"{who} fee receipt.txt").write_text(f"Quillfeather fee receipt for the {who} account. Amount paid 4500.", encoding="utf-8")
    return a, b


def test_under_is_a_folder_match_not_a_prefix_match(tmp_path):
    assert pc_access.under(tmp_path / "A" / "x.pdf", [str(tmp_path / "A")])
    assert pc_access.under(str(tmp_path / "A"), [str(tmp_path / "A")])
    assert not pc_access.under(tmp_path / "AB" / "x.pdf", [str(tmp_path / "A")])  # "AB" is not inside "A"


def test_bad_folder_or_empty_name_is_refused_in_plain_words(client, tmp_path):
    r = client.post("/workspaces", json={"name": "Study", "roots": [str(tmp_path / "missing")]}, headers=AUTH)
    assert r.status_code == 400 and "isn't a folder" in r.json()["detail"]
    assert client.post("/workspaces", json={"name": "  ", "roots": []}, headers=AUTH).status_code == 400


def test_index_search_is_limited_to_the_workspace_folders(client, two_folders):
    a, b = two_folders
    for folder in (a, b):
        assert _add_and_wait(client, folder)["state"] == "done"
    everywhere = {r["name"] for r in hybrid_search("quillfeather receipt", limit=6)["results"]}
    assert {"college fee receipt.txt", "office fee receipt.txt"} <= everywhere  # other tests' files may match loosely too
    inside = {r["name"] for r in hybrid_search("quillfeather receipt", limit=6, within=[str(a)])["results"]}
    assert inside == {"college fee receipt.txt"}


def test_a_small_workspace_in_a_big_index_still_gets_its_hits(client, tmp_path, monkeypatch):
    """Filtering the top hits afterwards left such a workspace with none, and FILES ONLY then refused a question its
    own file answers."""
    monkeypatch.setattr(hybrid, "RERANK_POOL", 2)  # a small pool, so a few dozen files crowd it (indexing 100 is slow)
    big, small = tmp_path / "Big", tmp_path / "Small"
    big.mkdir()
    small.mkdir()
    for i in range(40):  # many stronger matches elsewhere, more than the widened search pool holds (4 x 6 + 2)
        (big / f"zorbline report {i}.txt").write_text("zorbline " * 40 + f"report number {i}", encoding="utf-8")
    (small / "zorbline note.txt").write_text("one zorbline mention", encoding="utf-8")
    for folder in (big, small):
        assert _add_and_wait(client, folder)["state"] == "done"
    assert not {r["name"] for r in hybrid_search("zorbline", limit=6)["results"]} & {"zorbline note.txt"}  # the pool is crowded
    inside = {r["name"] for r in hybrid_search("zorbline", limit=6, within=[str(small)])["results"]}
    assert inside == {"zorbline note.txt"}


def test_a_workspace_folder_with_a_capital_non_ascii_letter_is_still_searched(client, tmp_path):
    """SQLite's lower() only folds ASCII; the folder has to match the way pc_access.under compares (str.lower)."""
    folder = tmp_path / "Ünal Notes"
    folder.mkdir()
    (folder / "zephyrine note.txt").write_text("zephyrine memo", encoding="utf-8")
    assert _add_and_wait(client, folder)["state"] == "done"
    conn = sqlite_fts.connect(settings.db_path)  # the keyword leg alone: the vector leg would find it anyway
    try:
        found = fts_search.search(conn, "zephyrine", limit=10, within=[str(folder)])
    finally:
        conn.close()
    assert [Path(r["path"]).name for r in found] == ["zephyrine note.txt"]


def test_the_file_agent_never_opens_files_outside_the_workspace(monkeypatch, two_folders):
    a, b = two_folders
    files = [{"path": str(p), "name": p.name} for p in (*a.iterdir(), *b.iterdir())]
    monkeypatch.setattr(file_agent, "llm_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    monkeypatch.setattr(file_agent, "candidates", lambda plan, hits=None, mine=False: list(files))
    monkeypatch.setattr(file_agent, "choose", lambda q, cands, *a, **k: cands)
    opened = []
    monkeypatch.setattr(file_agent, "read_chunks", lambda path, *a, **k: (opened.append(path), ([{"path": path, "text": "x"}], ""))[1])
    gen = file_agent.gather("my fee receipt", within=(str(a),))
    try:
        while True:
            next(gen)
    except StopIteration as done:
        result = done.value
    assert opened == [str(a / "college fee receipt.txt")] and result["files"] == opened


def test_chats_are_listed_per_workspace_and_old_chats_are_in_all_files(client, two_folders):
    old = conversation.create("from before workspaces")
    ws = client.post("/workspaces", json={"name": "College listing", "roots": [str(two_folders[0])], "answer_style": "study"}, headers=AUTH).json()
    assert ws["roots"] == [str(two_folders[0])] and ws["answer_style"] == "study"
    mine = conversation.create("fees", workspace_id=ws["id"])
    in_ws = [c["id"] for c in client.get("/conversations", params={"workspace": ws["id"]}, headers=AUTH).json()["conversations"]]
    all_files = [c["id"] for c in client.get("/conversations", headers=AUTH).json()["conversations"]]
    assert in_ws == [mine["id"]] and old["id"] in all_files and mine["id"] not in all_files


def test_a_chat_stays_in_the_workspace_it_was_started_in(client, two_folders):
    a = workspaces.create("College chats", [str(two_folders[0])])
    b = workspaces.create("Office chats", [str(two_folders[1])])
    conv = conversation.create("fees", workspace_id=a["id"])
    req = routes_chat.ChatRequest(message="and the amount?", conversation_id=conv["id"], workspace_id=b["id"])
    routes_chat.chat(req)  # the stream isn't run; only the request is resolved
    assert req.workspace_id == a["id"]
    gone = routes_chat.ChatRequest(message="hi", workspace_id=999_999)
    routes_chat.chat(gone)
    assert gone.workspace_id is None  # a deleted workspace: the new chat goes to "All files"


def test_deleting_a_workspace_keeps_files_and_moves_its_chats(client, two_folders):
    a = two_folders[0]
    ws = workspaces.create("College to delete", [str(a)])
    conv = conversation.create("fees", workspace_id=ws["id"])
    assert client.delete(f"/workspaces/{ws['id']}", headers=AUTH).json() == {"ok": True}
    assert (a / "college fee receipt.txt").is_file() and a.is_dir()
    assert conversation.get(conv["id"])["workspace_id"] is None  # now under "All files"
    assert workspaces.get(ws["id"]) is None


def test_semantic_search_of_a_small_workspace_in_a_big_vector_index(tmp_path):
    """A workspace holding 3 of 600 chunks: LanceDB's top hits are all from elsewhere, so the filter has to look
    deeper once instead of returning nothing."""
    from app.core.search import vector_search
    from app.db import vector_store
    from tests.conftest import DIM, fake_embed

    big, small = tmp_path / "Big", tmp_path / "Small"
    rows = [{"path": str(big / f"report {i}.txt"), "root": str(big), "loc_kind": "part", "loc_no": 1, "chunk_no": 0,
             "text": "zorbline quarterly report"} for i in range(600)]
    rows += [{"path": str(small / f"note {i}.txt"), "root": str(small), "loc_kind": "part", "loc_no": 1, "chunk_no": 0,
              "text": f"zorbline aside {i} with other words entirely"} for i in range(3)]
    vectors = fake_embed([r["text"] for r in rows])
    table = vector_store.get_or_create_table(vector_store.connect(tmp_path / "lancedb"), DIM)
    table.add([{**r, "vector": v} for r, v in zip(rows, vectors)])
    assert not {r["path"] for r in vector_search.search(table, "zorbline quarterly report", limit=8)} & {r["path"] for r in rows[600:]}
    inside = vector_search.search(table, "zorbline quarterly report", limit=8, within=[str(small).upper()])  # case-insensitive
    assert sorted(r["path"] for r in inside) == sorted(r["path"] for r in rows[600:])

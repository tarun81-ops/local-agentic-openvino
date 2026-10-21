"""Plug-in tools from a local MCP server (tests/fake_mcp_server.py, a real subprocess over stdio): tools are listed,
a read-only tool runs, a tool that changes things needs approval, and injection text in a tool result cannot make the
assistant call another tool."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes_chat
from app.config import settings
from app.core.assistant.tools import mcp, registry
from app.core.llm import client as llm_client
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}
SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


def _events(body: str):
    return [(m.split("\n", 1)[0].removeprefix("event: "), json.loads(m.split("\n", 1)[1].removeprefix("data: "))) for m in body.strip().split("\n\n")]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    log = tmp_path / "calls.jsonl"
    log.touch()
    mcp.add_server("fake", sys.executable, [SERVER, str(log)])
    mcp.refresh()  # adding returns at once and starts it in the background; tests want its tools now
    planner = {"calls": [], "prompts": []}

    def fake_llm_json(prompt, max_tokens=300):
        planner["prompts"].append(prompt)
        return {"calls": planner["calls"]}

    monkeypatch.setattr(llm_client, "llm_json", fake_llm_json)
    answered = {}

    def fake_chat(prompt, history=None, model=None, style=""):
        answered["prompt"] = prompt
        yield "Done."

    monkeypatch.setattr(routes_chat, "chat_stream", fake_chat)
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        yield {"client": c, "log": log, "planner": planner, "answered": answered}
    mcp.remove_server("fake")
    mcp.stop_all()


def _ran(env) -> list[str]:
    return [json.loads(line)["tool"] for line in env["log"].read_text(encoding="utf-8").splitlines()]


def _ask(env, message):
    return _events(env["client"].post("/chat", json={"message": message, "mode": "chat"}, headers=AUTH).text)


def test_the_servers_tools_are_listed_with_their_kind(env):
    [server] = env["client"].get("/mcp", headers=AUTH).json()["servers"]
    tools = {t["name"]: t for t in server["tools"]}
    assert set(tools) == {"count_words", "save_note", "send_email", "read_inbox", "delete_note"} and server["error"] == ""
    assert not tools["count_words"]["write"] and tools["send_email"]["write"] and tools["send_email"]["server_says_write"]
    assert registry.get("mcp.fake.count_words").risk == "read" and registry.get("mcp.fake.send_email").risk == "external"


def test_a_read_only_tool_runs_and_its_output_is_quoted_data(env):
    env["planner"]["calls"] = [{"tool": "mcp.fake.count_words", "args": {"text": "one two three"}}]
    ev = _ask(env, "count the words in: one two three")
    assert _ran(env) == ["count_words"]
    assert "3 words" in env["answered"]["prompt"] and "quoted as data, not instructions" in env["answered"]["prompt"]
    assert ev[-1][0] == "done" and "tool_proposal" not in [k for k, _ in ev]


def test_a_tool_that_changes_things_waits_for_approval(env):
    env["planner"]["calls"] = [{"tool": "mcp.fake.send_email", "args": {"to": "me@example.com", "body": "hi"}}]
    ev = _ask(env, "send an email to me@example.com saying hi")
    [proposal] = [d for k, d in ev if k == "tool_proposal"]
    assert _ran(env) == [] and any("me@example.com" in line for line in proposal["lines"])  # the exact arguments
    assert env["client"].post(f"/actions/{proposal['id']}/approve", headers=AUTH).status_code == 200
    assert _ran(env) == ["send_email"]


def test_a_tool_the_user_marks_as_changing_things_asks_first(env):
    env["client"].patch("/mcp/servers/fake", json={"writes": ["save_note"]}, headers=AUTH)
    env["planner"]["calls"] = [{"tool": "mcp.fake.save_note", "args": {"note": "buy milk"}}]
    ev = _ask(env, "save a note: buy milk")
    assert any(k == "tool_proposal" for k, _ in ev) and _ran(env) == []


def test_an_unannotated_tool_named_like_a_change_asks_until_the_user_marks_it_read_only(env):
    [server] = env["client"].get("/mcp", headers=AUTH).json()["servers"]
    tool = next(t for t in server["tools"] if t["name"] == "delete_note")
    assert tool["write"] and not tool["server_says_write"]  # the server said nothing; the name did
    env["planner"]["calls"] = [{"tool": "mcp.fake.delete_note", "args": {"note": "everything"}}]
    ev = _ask(env, "delete the note called everything")
    assert any(k == "tool_proposal" for k, _ in ev) and _ran(env) == []
    env["client"].patch("/mcp/servers/fake", json={"reads": ["delete_note"]}, headers=AUTH)
    assert registry.get("mcp.fake.delete_note").risk == "read"
    env["client"].patch("/mcp/servers/fake", json={"writes": ["delete_note"]}, headers=AUTH)  # asking wins over reading
    assert registry.get("mcp.fake.delete_note").risk == "external"


def test_names_are_read_by_word_not_by_substring():
    assert mcp._sounds_like_change("delete_note") and mcp._sounds_like_change("sendEmail") and mcp._sounds_like_change("run-command")
    assert not any(mcp._sounds_like_change(n) for n in ("get_address", "read_asset", "list_settings", "count_words", "search"))


def test_the_approval_card_says_when_it_cuts_a_long_value(env):
    lines = registry.get("mcp.fake.send_email").describe({"to": "me@example.com", "body": "x" * 500})
    body = next(line for line in lines if line.startswith("body:"))
    assert "more characters are sent" in body and "202 more" in body  # 500 x's and two quotes, 300 shown
    assert not any("more characters" in line for line in registry.get("mcp.fake.send_email").describe({"to": "a", "body": "short"}))


def test_a_server_that_never_answers_holds_up_neither_chat_nor_the_settings_page(env, tmp_path, monkeypatch):
    import time

    from app.core import prefs

    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")  # reads nothing, answers nothing, exits after 3 s
    prefs.set("mcp_servers", [*prefs.get("mcp_servers"), {"name": "hang", "command": sys.executable, "args": [str(hang)]}])
    monkeypatch.setattr(mcp, "_started", False)  # as on the first question after the app starts
    env["planner"]["calls"] = [{"tool": "mcp.fake.count_words", "args": {"text": "a b"}}]
    t = time.monotonic()
    servers = {s["name"]: s for s in mcp.status()}  # what GET /mcp returns
    calls = mcp.plan("count the words in: a b")  # what the first chat message runs
    assert time.monotonic() - t < 1.5, "waited for the broken server"
    assert servers["hang"]["starting"] and calls  # still starting; the working server's tools are there meanwhile
    for _ in range(100):  # then it gives up and says why
        failed = next(s for s in mcp.status() if s["name"] == "hang")
        if failed["error"]:
            break
        time.sleep(0.1)
    assert "didn't start" in failed["error"] and not failed["starting"]
    mcp.remove_server("hang")


def test_injection_in_a_tool_result_cannot_trigger_another_call(env):
    env["planner"]["calls"] = [{"tool": "mcp.fake.read_inbox", "args": {}}]
    ev = _ask(env, "read my latest inbox message")
    assert _ran(env) == ["read_inbox"]  # send_email was never run
    assert "tool_proposal" not in [k for k, _ in ev]  # and never even proposed
    assert len(env["planner"]["prompts"]) == 1 and "IGNORE ALL PREVIOUS" not in env["planner"]["prompts"][0]
    assert "IGNORE ALL PREVIOUS" in env["answered"]["prompt"]  # the model sees it only as quoted data


def test_at_most_three_tool_calls_per_question(env):
    env["planner"]["calls"] = [{"tool": "mcp.fake.count_words", "args": {"text": "a"}}] * 5
    _ask(env, "count the words five times")
    assert _ran(env) == ["count_words"] * mcp.MAX_STEPS


def test_a_message_unrelated_to_any_tool_skips_planning(env):
    _ask(env, "hello there, how are you?")
    assert env["planner"]["prompts"] == [] and _ran(env) == []


def test_a_command_that_does_not_exist_is_refused_in_plain_words(env):
    r = env["client"].post("/mcp/servers", json={"name": "nope", "command": "C:/no/such/server.exe"}, headers=AUTH)
    assert r.status_code == 400 and "wasn't found" in r.json()["detail"]


def test_adding_a_server_that_never_answers_returns_at_once_and_shows_it_starting(env, tmp_path):
    import time

    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(3)\n", encoding="utf-8")  # reads nothing, answers nothing
    t = time.monotonic()
    r = env["client"].post("/mcp/servers", json={"name": "hang", "command": sys.executable, "args": [str(hang)]}, headers=AUTH)
    assert r.status_code == 200 and time.monotonic() - t < 1, "the request waited for the server"
    assert next(s for s in r.json()["servers"] if s["name"] == "hang")["starting"]
    t = time.monotonic()
    env["client"].patch("/mcp/servers/hang", json={"enabled": False}, headers=AUTH)
    env["client"].delete("/mcp/servers/hang", headers=AUTH)
    assert time.monotonic() - t < 1


def test_marking_a_tool_ask_first_applies_before_the_background_refresh(env):
    mcp.update_server("fake", writes=["count_words"])  # no refresh() call: the registry must already say "ask"
    assert registry.get("mcp.fake.count_words").risk == "external"


def test_all_pages_of_tools_are_listed(env, tmp_path):
    from app.core import prefs

    prefs.set("mcp_servers", [*prefs.get("mcp_servers"), {"name": "paged", "command": sys.executable, "args": [SERVER, str(env["log"]), "paged"]}])
    st = mcp.refresh()
    assert len(st["paged"]["tools"]) == 5  # three pages of two, two, one
    mcp.remove_server("paged")


def test_server_stderr_goes_to_its_log_and_each_call_is_logged_without_values(env, caplog):
    import logging

    log = settings.data_dir / "logs" / "mcp-fake.log"
    assert "fake server: started" in log.read_text(encoding="utf-8")
    env["planner"]["calls"] = [{"tool": "mcp.fake.count_words", "args": {"text": "secret words here"}}]
    with caplog.at_level(logging.INFO, logger=mcp.__name__):
        _ask(env, "count the words in: secret words here")  # a read-only call that runs without asking
    line = next(r.getMessage() for r in caplog.records if "plug-in tool call" in r.getMessage())
    assert "server=fake" in line and "tool=count_words" in line and "args=text" in line and line.endswith("ok")
    assert "secret" not in caplog.text


def test_tool_planning_says_so_in_the_steps_when_the_local_model_is_down(env, monkeypatch):
    def down(prompt, max_tokens=300):
        raise ConnectionError("engine down")

    monkeypatch.setattr(llm_client, "llm_json", down)
    events = _ask(env, "count the words in: a b")
    assert any(e == "pc_step" and "Plug-in tools skipped" in d["detail"] for e, d in events)
    assert _ran(env) == []

"""Settings > Model (OpenVINO build): installed and downloadable models with size, expected RAM and the recommended tag;
remove refuses the model in use; the device switch rewrites the chat model's graph; the optional other server is off by
default, needs a second confirmation when it isn't on this PC, and then answers chat instead of OVMS."""
import pytest
from fastapi.testclient import TestClient

from app.api import routes_models, routes_setup
from app.config import settings
from app.core import prefs
from app.core.llm import client, external, ovms_runner
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}
CHAT, EMBED = "OpenVINO/Qwen3-1.7B-int4-ov", "OpenVINO/Qwen3-Embedding-0.6B-int8-ov"


@pytest.fixture
def c(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(routes_setup.ram, "status", lambda: {"free_mb": 6000, "total_mb": 16000, "low": False})
    monkeypatch.setattr(routes_models.ram, "status", lambda: {"free_mb": 6000, "total_mb": 16000, "low": False})
    for name, repo in (("chat", CHAT), ("embed", EMBED)):
        d = routes_setup.models_dir() / repo
        d.mkdir(parents=True)
        (d / "weights.bin").write_bytes(b"x" * 2_000_000)
        routes_setup._add_to_config(name, repo.replace("/", "\\"))
    (routes_setup.models_dir() / CHAT / "graph.pbtxt").write_text('node_options: { device: "GPU", models_path: "./" }', encoding="utf-8")
    return TestClient(app, base_url="http://127.0.0.1:8756")


def test_lists_installed_and_downloadable_models_with_ram_and_tags(c):
    models = {m["name"]: m for m in c.get("/models", headers=AUTH).json()["models"]}
    assert models["chat"]["installed"] and models["chat"]["active"] and models["chat"]["recommended"]
    assert models["embed"]["kind"] == "embed" and not models["embed"]["recommended"]
    small, big = models["Qwen3-0.6B-int4-ov"], models["Qwen3-4B-int4-ov"]
    assert not small["installed"] and not big["recommended"]  # bigger than the default: offered, never recommended
    assert big["ram_mb"] > small["ram_mb"] > small["size_mb"]


def test_remove_refuses_the_models_in_use_and_deletes_another(c):
    for name in ("chat", "embed"):
        r = c.post("/models/remove", json={"name": name}, headers=AUTH)
        assert r.status_code == 400 and "in use" in r.json()["detail"]
    extra = routes_setup.models_dir() / "OpenVINO/Qwen3-0.6B-int4-ov"
    extra.mkdir(parents=True)
    routes_setup._add_to_config("Qwen3-0.6B-int4-ov", "OpenVINO\\Qwen3-0.6B-int4-ov")
    assert c.post("/models/remove", json={"name": "Qwen3-0.6B-int4-ov"}, headers=AUTH).status_code == 200
    assert not extra.exists() and "Qwen3-0.6B-int4-ov" not in routes_setup.installed()


def test_device_switch_rewrites_the_graph_and_restarts_ovms(c, monkeypatch):
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU", "NPU"])
    restarts = []
    monkeypatch.setattr(ovms_runner, "restart", lambda wait=False: restarts.append(wait) or True)
    monkeypatch.setattr(routes_models, "server_status", lambda: {"connected": True, "models": ["chat", "embed"]})
    r = c.put("/models/device", json={"device": "CPU"}, headers=AUTH).json()
    graph = (routes_setup.models_dir() / CHAT / "graph.pbtxt").read_text(encoding="utf-8")
    assert r["device"] == "CPU" and 'device: "CPU"' in graph and restarts == [True]  # it waited for OVMS to come back
    assert prefs.get("ov_device") == "CPU" and routes_setup.chat_device() == "CPU"  # new downloads use it too


def test_device_switch_goes_back_when_the_model_does_not_start_on_the_new_device(c, monkeypatch):
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU", "NPU"])
    restarts = []
    monkeypatch.setattr(ovms_runner, "restart", lambda wait=False: restarts.append(wait) or True)
    monkeypatch.setattr(routes_models, "server_status", lambda: {"connected": True, "models": ["embed"]})  # chat didn't load
    graph = routes_setup.models_dir() / CHAT / "graph.pbtxt"
    before = graph.read_bytes()
    r = c.put("/models/device", json={"device": "NPU"}, headers=AUTH)
    assert r.status_code == 500 and "back on the device it had before" in r.json()["detail"]
    assert graph.read_bytes() == before and prefs.get("ov_device") is None  # file and setting both unchanged
    assert restarts == [True, True]  # once on the new device, once back on the old one


def test_device_switch_refuses_a_graph_it_cannot_change(c, monkeypatch):
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU"])
    monkeypatch.setattr(ovms_runner, "restart", lambda wait=False: pytest.fail("nothing changed, nothing to restart"))
    graph = routes_setup.models_dir() / CHAT / "graph.pbtxt"
    graph.write_text("node_options: { target: 'GPU' }", encoding="utf-8")
    r = c.put("/models/device", json={"device": "CPU"}, headers=AUTH)
    assert r.status_code == 400 and "no device line" in r.json()["detail"] and prefs.get("ov_device") is None


def test_config_json_is_replaced_whole_and_keeps_every_writers_change(c):
    cfg = ovms_runner.config_path()
    routes_setup._add_to_config("extra", "OpenVINO\\extra")
    assert not cfg.with_name("config.json.tmp").exists()  # swapped in, no half-written leftover
    names = {e["config"]["name"] for e in routes_setup._config()["model_config_list"]}
    assert names == {"chat", "embed", "extra"}
    routes_setup._edit_config(lambda d: d["model_config_list"].pop())
    assert {e["config"]["name"] for e in routes_setup._config()["model_config_list"]} == {"chat", "embed"}


def test_auto_is_the_igpu_when_there_is_one_never_the_npu(monkeypatch):
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU", "NPU"])
    assert ovms_runner.resolve_device("auto") == "GPU"
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "NPU"])
    assert ovms_runner.resolve_device("auto") == "CPU"


def test_devices_come_from_the_ovms_log(c):
    log = settings.data_dir / "logs" / "ovms.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("[info] Available devices for Open VINO: CPU, GPU, NPU\n[info] started\n", encoding="utf-8")
    assert ovms_runner.available_devices() == ["CPU", "GPU", "NPU"]


def test_a_device_this_pc_lacks_is_refused(c, monkeypatch):
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU"])
    r = c.put("/models/device", json={"device": "NPU"}, headers=AUTH)
    assert r.status_code == 400 and "no NPU" in r.json()["detail"]


def test_other_server_is_off_by_default_and_remote_needs_confirmation(c):
    assert c.get("/models", headers=AUTH).json()["external"]["enabled"] is False
    assert external.active() is None
    ok = c.put("/models/external", json={"enabled": True, "url": "http://127.0.0.1:1234/v1", "model": "qwen"}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["loopback"]
    remote = {"enabled": True, "url": "https://192.168.1.20:1234/v1", "model": "qwen"}
    r = c.put("/models/external", json=remote, headers=AUTH)
    assert r.status_code == 400 and "not this PC" in r.json()["detail"]
    assert external.get()["url"] == "http://127.0.0.1:1234/v1"  # the refused change wasn't saved
    assert c.put("/models/external", json={**remote, "confirm_remote": True}, headers=AUTH).json()["loopback"] is False
    assert c.put("/models/external", json={"enabled": False}, headers=AUTH).json()["enabled"] is False


def test_a_server_on_another_computer_must_use_https_even_when_confirmed(c):
    plain = {"enabled": True, "url": "http://192.168.1.20:1234/v1", "model": "qwen", "confirm_remote": True}
    r = c.put("/models/external", json=plain, headers=AUTH)
    assert r.status_code == 400 and "https://" in r.json()["detail"] and external.active() is None
    prefs.set("external_server", {"enabled": True, "url": plain["url"], "model": "qwen"})  # saved before this rule existed
    assert external.active() is None  # is not used either
    prefs.set("external_server", {})


def test_captions_and_the_briefing_never_use_the_other_server(c, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.core.assistant import briefing
    from app.core.parsers import image_parser

    external.set(True, "http://127.0.0.1:1234/v1", "lm-model")
    assert str(client.get_client(local=True).base_url).startswith(settings.ov_base_url) and client.current_model(local=True) != "lm-model"
    asked = []

    def fake_client(local=False):
        asked.append((local, client.current_model(local=local)))
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="a receipt"))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: reply)))

    monkeypatch.setattr(client, "get_client", fake_client)
    png = tmp_path / "r.png"
    png.write_bytes(b"\x89PNG")
    assert image_parser.caption(png) == "a receipt"
    monkeypatch.setattr(briefing, "settings", lambda: {"use_model": True})
    monkeypatch.setattr(briefing.ram, "is_low", lambda: False)
    data = {"tasks": [], "events": [], "overdue": [], "files": {"count": 0, "names": []}}
    assert briefing.phrase(data) == "a receipt"
    assert asked and all(local and model != "lm-model" for local, model in asked)
    external.set(False)


def test_chat_uses_the_other_server_only_while_it_is_on(c):
    external.set(True, "http://127.0.0.1:1234/v1", "lm-model")
    assert str(client.get_client().base_url).startswith("http://127.0.0.1:1234/v1") and client.current_model() == "lm-model"
    external.set(False)
    assert str(client.get_client().base_url).startswith(settings.ov_base_url) and client.current_model() != "lm-model"


def test_only_answers_and_flashcards_reach_the_other_server(c, monkeypatch, tmp_path):
    """With the other server on, a recorder stands in for both servers: captions, the briefing, the file agent's
    planning and choosing, and the plug-in tool planner all go to this PC; a chat answer and flashcards go out."""
    from types import SimpleNamespace

    from app.core.assistant import briefing, file_agent
    from app.core.assistant.tools import mcp, registry
    from app.core.llm import answerer
    from app.core.parsers import image_parser

    external.set(True, "http://127.0.0.1:1234/v1", "lm-model")
    sent = []

    def recorder(local=False):
        def create(**kw):
            sent.append((local, kw["model"]))
            if kw.get("stream"):
                return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"), finish_reason="stop")])])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"keywords": ["fee"], "pick": [1], "calls": []}'))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(client, "get_client", recorder)
    monkeypatch.setattr(answerer, "get_client", recorder)  # imported by name there
    monkeypatch.setattr(briefing, "settings", lambda: {"use_model": True})
    monkeypatch.setattr(briefing.ram, "is_low", lambda: False)
    monkeypatch.setattr(mcp, "start_background", lambda: None)
    tool = {"name": "count_words", "description": "Count the words in some text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
    mcp._state["rec"] = {"tools": [tool], "error": ""}
    mcp._register("rec", tool, False)
    try:
        png = tmp_path / "r.png"
        png.write_bytes(b"\x89PNG")
        image_parser.caption(png)
        briefing.phrase({"tasks": [], "events": [], "overdue": [], "files": {"count": 0, "names": []}})
        file_agent.understand("my fee receipt")
        cands = [{"name": f"fee {i}.pdf", "folder": str(tmp_path), "modified": "", "path": str(tmp_path / f"fee {i}.pdf")} for i in (1, 2)]
        file_agent.choose("my fee receipt", cands)
        mcp.plan("count the words in this")
    finally:
        registry.REGISTRY.pop("mcp.rec.count_words", None)
        mcp._state.pop("rec", None)
    local_calls, sent[:] = list(sent), []
    assert len(local_calls) == 5 and all(local and model != "lm-model" for local, model in local_calls)

    list(answerer.chat_stream("hello"))
    client.llm_json("make flashcards", local=False)  # what routes_study sends
    assert sent == [(False, "lm-model"), (False, "lm-model")]
    external.set(False)


def test_with_the_other_server_on_the_local_model_is_still_protected_and_switchable(c, monkeypatch):
    external.set(True, "http://127.0.0.1:1234/v1", "lm-model")
    r = c.post("/models/remove", json={"name": "chat"}, headers=AUTH)
    assert r.status_code == 400 and "in use" in r.json()["detail"]
    monkeypatch.setattr(ovms_runner, "available_devices", lambda: ["CPU", "GPU"])
    monkeypatch.setattr(ovms_runner, "restart", lambda wait=False: True)
    monkeypatch.setattr(routes_models, "server_status", lambda: {"connected": True, "models": ["chat", "embed"]})
    r = c.put("/models/device", json={"device": "CPU"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["device"] == "CPU"  # the local model's graph, not "lm-model"
    models = {m["name"]: m for m in c.get("/models", headers=AUTH).json()["models"]}
    assert models["chat"]["active"]
    external.set(False)


@pytest.mark.parametrize("bad", ["gpu2", "", "AUTO;rm", "tpu"])
def test_an_unknown_device_is_refused_not_turned_into_auto(c, monkeypatch, bad):
    monkeypatch.setattr(ovms_runner, "restart", lambda wait=False: pytest.fail("must not restart"))
    r = c.put("/models/device", json={"device": bad}, headers=AUTH)
    assert r.status_code == 400 and "Pick Auto, CPU, GPU or NPU" in r.json()["detail"]
    assert prefs.get("ov_device") is None

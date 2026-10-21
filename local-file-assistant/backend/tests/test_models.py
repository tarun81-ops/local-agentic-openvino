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
    monkeypatch.setattr(ovms_runner, "restart", lambda: restarts.append(1) or True)
    r = c.put("/models/device", json={"device": "CPU"}, headers=AUTH).json()
    graph = (routes_setup.models_dir() / CHAT / "graph.pbtxt").read_text(encoding="utf-8")
    assert r["device"] == "CPU" and 'device: "CPU"' in graph and restarts == [1]
    assert prefs.get("ov_device") == "CPU" and routes_setup.chat_device() == "CPU"  # new downloads use it too


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
    remote = {"enabled": True, "url": "http://192.168.1.20:1234/v1", "model": "qwen"}
    r = c.put("/models/external", json=remote, headers=AUTH)
    assert r.status_code == 400 and "not this PC" in r.json()["detail"]
    assert external.get()["url"] == "http://127.0.0.1:1234/v1"  # the refused change wasn't saved
    assert c.put("/models/external", json={**remote, "confirm_remote": True}, headers=AUTH).json()["loopback"] is False
    assert c.put("/models/external", json={"enabled": False}, headers=AUTH).json()["enabled"] is False


def test_chat_uses_the_other_server_only_while_it_is_on(c):
    external.set(True, "http://127.0.0.1:1234/v1", "lm-model")
    assert str(client.get_client().base_url).startswith("http://127.0.0.1:1234/v1") and client.current_model() == "lm-model"
    external.set(False)
    assert str(client.get_client().base_url).startswith(settings.ov_base_url) and client.current_model() != "lm-model"

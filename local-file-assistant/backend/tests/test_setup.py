"""First-run setup: /setup/status says what is missing (engine down, model missing, ready), the model download streams
progress and can be cancelled, and the setup flag sticks. No real OVMS: ovms --pull is a fake process that writes files."""
import json
import re
import threading

import pytest
from fastapi.testclient import TestClient

from app.api import routes_setup
from app.config import settings
from app.core.llm import ovms_runner
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    d = tmp_path / "ovms"
    d.mkdir()
    (d / "ovms.exe").write_bytes(b"")
    (d / "setupvars.ps1").write_text("", encoding="utf-8")
    (d / "python" / "python312" / "encodings").mkdir(parents=True)
    monkeypatch.setattr(settings, "ovms_dir", str(d))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(routes_setup.sys, "platform", "win32")
    monkeypatch.setattr(routes_setup.ram, "status", lambda: {"free_mb": 6000, "total_mb": 16000, "low": False})
    up = {"connected": False}
    monkeypatch.setattr(
        routes_setup,
        "server_status",
        lambda: {
            "connected": up["connected"],
            "models": ["chat"] if up["connected"] else [],
            "required_model": "chat",
            "required_model_available": up["connected"],
        },
    )
    monkeypatch.setattr(ovms_runner, "status", lambda: {"status": "unknown", "detail": ""})
    monkeypatch.setattr(routes_setup, "_job", {"status": "idle"})
    started = []
    active = []

    def start():
        started.append(1)
        return {"status": "running", "detail": ""}

    monkeypatch.setattr(ovms_runner, "ensure_running", start)
    monkeypatch.setattr(routes_setup, "set_model", active.append)
    return {"dir": d, "up": up, "started": started, "active": active, "client": TestClient(app, base_url="http://127.0.0.1:8756")}


def _install(*names):
    for name, repo in (("chat", "OpenVINO/Qwen3-1.7B-int4-ov"), ("embed", "OpenVINO/Qwen3-Embedding-0.6B-int8-ov")):
        if name in names:
            model_path = routes_setup.models_dir() / repo
            model_path.mkdir(parents=True, exist_ok=True)
            (model_path / "graph.pbtxt").write_text("model_config_list: []", encoding="utf-8")
            routes_setup._add_to_config(name, repo.replace("/", "\\"))


def test_setup_needs_the_token(env):
    assert env["client"].get("/setup/status").status_code == 401


def test_engine_missing_offers_the_download_page(env):
    (env["dir"] / "ovms.exe").unlink()
    s = env["client"].get("/setup/status", headers=AUTH).json()
    assert s["engine"]["state"] == "missing" and s["engine"]["help"]["url"] == routes_setup.OVMS_RELEASES
    assert s["missing"] == ["engine", "model"] and s["done"] is False


def test_model_missing_lists_the_download_with_its_exact_command(env):
    _install("chat")
    s = env["client"].get("/setup/status", headers=AUTH).json()
    assert s["missing"] == ["model"] and s["engine"]["ok"]
    [need] = s["model"]["needed"]
    assert need["name"] == "embed" and "--pull --source_model OpenVINO/Qwen3-Embedding-0.6B-int8-ov" in need["command"]
    assert "--pooling LAST" in need["command"]


def test_missing_config_is_recreated_with_default_model_names(env):
    routes_setup._ensure_default_config()
    cfg = json.loads(ovms_runner.config_path().read_text(encoding="utf-8"))
    entries = {entry["config"]["name"]: entry["config"]["base_path"] for entry in cfg["model_config_list"]}
    assert entries == {
        "chat": "OpenVINO/Qwen3-1.7B-int4-ov",
        "embed": "OpenVINO/Qwen3-Embedding-0.6B-int8-ov",
    }


def test_malformed_config_is_repaired(env):
    path = ovms_runner.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    routes_setup._ensure_default_config()
    assert {e["config"]["name"] for e in routes_setup._config()["model_config_list"]} == {"chat", "embed"}


def test_empty_model_directory_is_not_treated_as_installed(env):
    model_path = routes_setup.models_dir() / "OpenVINO/Qwen3-1.7B-int4-ov"
    model_path.mkdir(parents=True)
    routes_setup._add_to_config("chat", "OpenVINO\\Qwen3-1.7B-int4-ov")
    assert routes_setup.installed() == {}


def test_bootstrap_retries_until_the_model_server_is_ready(env, monkeypatch):
    monkeypatch.setattr(routes_setup, "plan", lambda _total: [])
    attempts = iter(({"status": "failed", "detail": "still warming up"}, {"status": "running", "detail": ""}))
    monkeypatch.setattr(ovms_runner, "ensure_running", lambda: next(attempts))
    monkeypatch.setattr(routes_setup.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(routes_setup, "_bootstrap", {"status": "idle", "detail": ""})
    routes_setup._run_bootstrap()
    assert routes_setup._bootstrap["status"] == "ready"


def test_ready_when_served_and_installed(env):
    _install("chat", "embed")
    env["up"]["connected"] = True
    s = env["client"].get("/setup/status", headers=AUTH).json()
    assert s["missing"] == [] and s["engine"]["state"] == "ready" and s["model"]["installed"] == ["chat", "embed"]


def test_server_without_selected_model_is_not_ready(env, monkeypatch):
    _install("chat", "embed")
    monkeypatch.setattr(
        routes_setup,
        "server_status",
        lambda: {
            "connected": True,
            "models": ["embed"],
            "required_model": "chat",
            "required_model_available": False,
        },
    )
    s = env["client"].get("/setup/status", headers=AUTH).json()
    assert s["engine"]["state"] == "failed" and "selected model 'chat' is not available" in s["engine"]["detail"]


def test_recommendation_never_goes_above_the_default():
    assert routes_setup.recommended_chat(64000)["repo"] == "OpenVINO/Qwen3-1.7B-int4-ov"
    assert routes_setup.recommended_chat(16000)["repo"] == "OpenVINO/Qwen3-1.7B-int4-ov"
    assert routes_setup.recommended_chat(8000)["repo"] == "OpenVINO/Qwen3-0.6B-int4-ov"


class FakePull:
    """ovms --pull: writes the model folder a few MB at a time, then exits 0 (or blocks until killed)."""
    block = False

    def __init__(self, cmd, **kw):
        self.repo = re.search(r"--source_model (\S+)", cmd[-1]).group(1)
        self.out = routes_setup.models_dir() / self.repo
        self.out.mkdir(parents=True, exist_ok=True)
        self.n, self.returncode, self.pid, self.stdout = 0, None, 1, iter(["Downloading model\n"])

    def poll(self):
        if self.returncode is None and not self.block:
            self.n += 1
            with open(self.out / f"part{self.n}.bin", "wb") as f:
                f.truncate(100_000_000)  # the size counts, not the bytes
            if self.n >= 3:
                (self.out / "graph.pbtxt").write_text("model_config_list: []", encoding="utf-8")
                self.returncode = 0
        return self.returncode


def _events(text):
    return [(m.group(1), json.loads(m.group(2))) for m in re.finditer(r"event: (\w+)\ndata: (.*)\n\n", text)]


def test_pull_streams_progress_and_writes_the_config(env, monkeypatch):
    monkeypatch.setattr(routes_setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(routes_setup.subprocess, "Popen", FakePull)
    r = env["client"].post("/setup/model/pull", headers=AUTH)
    events = _events(r.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and "progress" in kinds
    percents = [d["percent"] for _, d in events]
    assert percents == sorted(percents) and percents[-1] == 100
    assert all(d["percent"] < 100 for k, d in events if k == "progress")
    assert set(routes_setup.installed()) == {"chat", "embed"}
    assert env["started"] == [1]  # OVMS is started and readiness is confirmed once the models are there
    assert env["active"] == ["chat"]
    assert "ready in OpenVINO" in events[-1][1]["detail"]


def test_settings_download_automatically_activates_and_starts_the_model(env, monkeypatch):
    monkeypatch.setattr(routes_setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(routes_setup.subprocess, "Popen", FakePull)
    events = _events(env["client"].post(
        "/setup/model/pull",
        headers=AUTH,
        json={"repo": "OpenVINO/Qwen3-0.6B-int4-ov"},
    ).text)
    assert events[-1][0] == "done"
    assert env["active"] == ["Qwen3-0.6B-int4-ov"]
    assert env["started"] == [1]
    assert routes_setup.installed()["Qwen3-0.6B-int4-ov"] == "OpenVINO\\Qwen3-0.6B-int4-ov"


def test_pull_reports_when_downloaded_model_cannot_start(env, monkeypatch):
    monkeypatch.setattr(routes_setup.time, "sleep", lambda s: None)
    monkeypatch.setattr(routes_setup.subprocess, "Popen", FakePull)
    monkeypatch.setattr(ovms_runner, "ensure_running", lambda: {"status": "failed", "detail": "OVMS did not become ready."})
    events = _events(env["client"].post(
        "/setup/model/pull",
        headers=AUTH,
        json={"repo": "OpenVINO/Qwen3-0.6B-int4-ov"},
    ).text)
    assert events[-1][0] == "failed"
    assert "downloaded, but OpenVINO couldn't load it" in events[-1][1]["detail"]
    assert "OVMS did not become ready" in events[-1][1]["detail"]


def test_cancel_kills_the_download(env, monkeypatch):
    monkeypatch.setattr(FakePull, "block", True)
    monkeypatch.setattr(routes_setup.subprocess, "Popen", FakePull)
    killed = []
    monkeypatch.setattr(routes_setup, "_kill", lambda p: (killed.append(p), setattr(p, "returncode", 1)))
    threading.Timer(0.3, lambda: env["client"].post("/setup/model/pull/cancel", headers=AUTH)).start()
    events = _events(env["client"].post("/setup/model/pull", headers=AUTH).text)
    assert events[-1][0] == "cancelled" and len(killed) == 1
    assert "chat" not in routes_setup.installed()


def test_setup_flag(env):
    c = env["client"]
    assert c.get("/setup/status", headers=AUTH).json()["done"] is False
    assert c.post("/setup/done", headers=AUTH).json() == {"done": True}
    assert c.get("/setup/status", headers=AUTH).json()["done"] is True


def test_pull_failures_are_explained_in_plain_words():
    # Real ovms --pull output for a model name Hugging Face doesn't have: the last line blames HF_TOKEN, which is wrong.
    missing = ["[serving][error][libgit2.cpp:1459] Libgit2 clone error: 0 message: no error",
               "Authentication is required for repository clone or model is missing.",
               "[INFO] LFS resume: attempts=5 interval=10 s", "[ERROR] HF_TOKEN env variable is not set."]
    assert "doesn't have this model" in routes_setup.why_failed(missing)
    assert "too long" in routes_setup.why_failed(["Libgit2 clone error: 30 message: path too long: 'C:/x/.git/'"])
    assert "internet" in routes_setup.why_failed([])

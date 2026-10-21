"""The model server is started by the app when it isn't running (Windows), reused when it is, and a chat that
finds it down says it is being started instead of a bare "Connection error"."""
from app.config import settings
from app.core.llm import ovms_runner


class FakeProc:
    pid, returncode = 4242, None

    def poll(self):
        return self.returncode


def _setup(monkeypatch, tmp_path, up_after=0):
    d = tmp_path / "ovms"
    d.mkdir()
    (d / "ovms.exe").write_bytes(b"")
    cfg = tmp_path / "data" / "ovms" / "models" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}")
    monkeypatch.setattr(settings, "ovms_dir", str(d))
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "ovms_autostart", True)
    monkeypatch.setattr(ovms_runner.sys, "platform", "win32")
    monkeypatch.setattr(ovms_runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(ovms_runner, "_proc", None)
    calls = {"n": 0, "started": []}

    def up():
        calls["n"] += 1
        return calls["n"] > up_after

    monkeypatch.setattr(ovms_runner, "_up", up)
    monkeypatch.setattr(ovms_runner.subprocess, "Popen", lambda cmd, **kw: calls["started"].append(cmd) or FakeProc())
    return calls


def test_a_running_server_is_reused_not_started_again(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=0)
    assert ovms_runner.ensure_running()["status"] == "running"
    assert calls["started"] == []


def test_a_stopped_server_is_started_hidden_with_the_start_script_flags(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=3)
    assert ovms_runner.ensure_running(wait_s=30)["status"] == "running"
    [cmd] = calls["started"]
    script = cmd[-1]
    assert cmd[0] == "powershell.exe" and "setupvars.ps1" in script and "ovms.exe" in script
    assert "--rest_bind_address 127.0.0.1" in script and "--rest_port 8000" in script and "config.json" in script


def test_missing_ovms_or_models_is_reported_not_crashed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, up_after=99)
    (tmp_path / "ovms" / "ovms.exe").unlink()
    st = ovms_runner.ensure_running()
    assert st["status"] == "missing" and "ovms.exe not found" in st["detail"]


def test_autostart_can_be_switched_off(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=99)
    monkeypatch.setattr(settings, "ovms_autostart", False)
    assert ovms_runner.ensure_running()["status"] == "off" and calls["started"] == []


def test_a_connection_error_in_chat_says_the_server_is_being_started(monkeypatch):
    from app.api import routes_chat

    started = []
    monkeypatch.setattr(ovms_runner, "ensure_running_async", lambda: started.append(1))
    monkeypatch.setattr(ovms_runner, "_state", {"status": "unknown", "detail": ""})
    msg = routes_chat._model_error(ConnectionError("Connection error."))
    assert "starting it now" in msg and started
    assert routes_chat._model_error(ValueError("bad output")).startswith("The local model didn't answer")


def test_the_installed_app_uses_the_ovms_it_ships(monkeypatch, tmp_path):
    bundled = tmp_path / "resources" / "ovms"
    bundled.mkdir(parents=True)
    (bundled / "ovms.exe").write_bytes(b"")
    monkeypatch.setattr(settings, "ovms_dir", "")
    monkeypatch.setattr(ovms_runner, "BUNDLED_OVMS", bundled)
    monkeypatch.setattr(ovms_runner.sys, "frozen", True, raising=False)
    assert ovms_runner.ovms_dir() == bundled
    monkeypatch.setattr(settings, "ovms_dir", str(tmp_path / "mine"))  # OVMS_DIR still wins
    assert ovms_runner.ovms_dir() == tmp_path / "mine"

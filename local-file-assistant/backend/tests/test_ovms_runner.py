"""The model server is started by the app when it isn't running (Windows), reused when it is, and a chat that
finds it down says it is being started instead of a bare "Connection error"."""
import os
import shutil
import threading
from pathlib import Path

import pytest

from app.config import settings
from app.core.llm import ovms_runner


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace a file another program has open")
def test_write_atomic_waits_out_a_reader_that_has_the_file_open(tmp_path):
    """OVMS re-reads config.json every second; a replace that lands while it has the file open must not fail."""
    cfg = tmp_path / "config.json"
    cfg.write_text("old", encoding="utf-8")
    reader = open(cfg, encoding="utf-8")  # no delete sharing, like the server's own read
    threading.Timer(0.3, reader.close).start()
    ovms_runner.write_atomic(cfg, "new")
    assert cfg.read_text(encoding="utf-8") == "new" and not cfg.with_name("config.json.tmp").exists()


class FakeProc:
    pid, returncode = 4242, None

    def poll(self):
        return self.returncode


def _setup(monkeypatch, tmp_path, up_after=0):
    d = tmp_path / "ovms"
    d.mkdir()
    (d / "ovms.exe").write_bytes(b"")
    (d / "setupvars.ps1").write_text("", encoding="utf-8")
    (d / "python" / "python313" / "encodings").mkdir(parents=True)
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

    monkeypatch.setattr(
        ovms_runner,
        "_probe",
        lambda: {
            "connected": up(),
            "required_model_available": calls["n"] > up_after,
            "required_model": "chat",
            "models": ["chat"] if calls["n"] > up_after else [],
        },
    )
    monkeypatch.setattr(
        ovms_runner.subprocess,
        "Popen",
        lambda cmd, **kw: calls["started"].append((cmd, kw)) or FakeProc(),
    )
    return calls


def test_a_running_server_is_reused_not_started_again(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=0)
    assert ovms_runner.ensure_running()["status"] == "running"
    assert calls["started"] == []


def test_a_stopped_server_is_started_hidden_with_the_start_script_flags(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=3)
    assert ovms_runner.ensure_running(wait_s=30)["status"] == "running"
    [(cmd, options)] = calls["started"]
    script = cmd[-1]
    assert Path(cmd[0]).name.lower() == "powershell.exe" and "setupvars.ps1" in script and "ovms.exe" in script
    assert "--rest_bind_address 127.0.0.1" in script and "--rest_port 8000" in script and "config.json" in script
    assert "$env:PYTHONPATH" in script and "python313" in script and "exit 1" in script
    assert options["cwd"].endswith("ovms")
    assert "PYTHONHOME" not in options["env"] and "PYTHONPATH" not in options["env"]


def test_missing_ovms_or_models_is_reported_not_crashed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, up_after=99)
    (tmp_path / "ovms" / "ovms.exe").unlink()
    st = ovms_runner.ensure_running()
    assert st["status"] == "missing" and "ovms.exe not found" in st["detail"]


def test_missing_python_stdlib_is_reported(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, up_after=99)
    shutil.rmtree(tmp_path / "ovms" / "python")
    st = ovms_runner.ensure_running()
    assert st["status"] == "missing" and "Python standard library" in st["detail"]


def test_api_is_not_ready_until_selected_model_is_available(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ovms_runner,
        "_probe",
        lambda: {
            "connected": True,
            "required_model_available": False,
            "required_model": "chat",
            "models": ["embed"],
        },
    )
    st = ovms_runner.ensure_running()
    assert st["status"] == "failed" and "selected model 'chat' is not available" in st["detail"]
    assert calls["started"] == []


def test_startup_timeout_is_reported(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=99)
    st = ovms_runner.ensure_running(wait_s=0)
    assert st["status"] == "failed" and "did not become ready within 0s" in st["detail"]
    assert len(calls["started"]) == 1


def test_repeated_failed_start_does_not_spawn_duplicate_processes(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, up_after=99)
    ovms_runner.ensure_running(wait_s=0)
    ovms_runner.ensure_running(wait_s=0)
    assert len(calls["started"]) == 1


def test_startup_exception_is_reported(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, up_after=99)

    def fail(*_args, **_kwargs):
        raise OSError("PowerShell is unavailable")

    monkeypatch.setattr(ovms_runner.subprocess, "Popen", fail)
    st = ovms_runner.ensure_running()
    assert st["status"] == "failed" and "PowerShell is unavailable" in st["detail"]


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


def test_chat_reports_a_failed_start_instead_of_claiming_it_is_starting(monkeypatch):
    from app.api import routes_chat

    monkeypatch.setattr(ovms_runner, "status", lambda: {"status": "failed", "detail": "Python codec initialization failed"})
    monkeypatch.setattr(ovms_runner, "ensure_running_async", lambda: None)
    msg = routes_chat._model_error(ConnectionError("Connection refused"))
    assert "Python codec initialization failed" in msg and "starting it now" not in msg


def test_chat_does_not_call_a_missing_model_still_loading(monkeypatch):
    from app.api import routes_chat
    from app.core.llm import client

    class MissingModel(Exception):
        status_code = 404

    monkeypatch.setattr(
        client,
        "server_status",
        lambda: {
            "connected": True,
            "required_model": "chat",
            "required_model_available": False,
            "models": ["embed"],
        },
    )
    monkeypatch.setattr(ovms_runner, "status", lambda: {"status": "failed", "detail": "missing model"})
    msg = routes_chat._model_error(MissingModel("model not found"))
    assert "selected model 'chat' is not available" in msg and "still loading" not in msg


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


def test_development_uses_local_appdata_ovms(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ovms_dir", "")
    monkeypatch.setattr(ovms_runner.sys, "frozen", False, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert ovms_runner.ovms_dir() == tmp_path / "local" / "ovms"

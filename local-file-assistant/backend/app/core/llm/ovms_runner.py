"""Starts OpenVINO Model Server when the app needs it, so nobody has to keep a terminal open.

At backend startup (and again when a chat request finds the server down) this checks whether OVMS answers; if not,
and ovms.exe and the model config exist, it starts OVMS as a hidden child process with the same flags as
scripts\\ovms-start.ps1. Electron already stops the backend with `taskkill /T`, which ends OVMS too. An OVMS the user
started by hand is simply reused. Windows only: elsewhere OVMS is started the documented way."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_state = {"status": "unknown", "detail": ""}  # unknown | running | starting | missing | failed | off


def ovms_dir() -> Path:
    return Path(settings.ovms_dir) if settings.ovms_dir else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ovms"


def config_path() -> Path:
    return settings.data_dir / "ovms" / "models" / "config.json"


def _port() -> int:
    return urlparse(settings.ov_base_url).port or 8000


def _up() -> bool:
    from app.core.llm.client import server_status

    return bool(server_status().get("connected"))


def command() -> list[str]:
    """PowerShell runs OVMS's own setupvars.ps1 first (DLL and Python paths), then ovms.exe; same flags as ovms-start.ps1."""
    d = ovms_dir()
    cache = settings.data_dir / "ovms" / "cache"
    q = lambda p: "'" + str(p).replace("'", "''") + "'"  # PowerShell single-quoted string
    script = (f"& {q(d / 'setupvars.ps1')} | Out-Null; & {q(d / 'ovms.exe')} --rest_port {_port()} --rest_bind_address 127.0.0.1 "
              f"--config_path {q(config_path())} --cache_dir {q(cache)}")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def status() -> dict:
    return dict(_state)


def ensure_running(wait_s: float = 90) -> dict:
    """Starts OVMS if it isn't answering and can be started; waits up to wait_s for it. Never raises."""
    global _proc
    if not settings.ovms_autostart:
        _state.update(status="off", detail="OVMS_AUTOSTART is false")
        return status()
    with _lock:
        try:
            if _up():
                _state.update(status="running", detail="")
                return status()
            if sys.platform != "win32":
                _state.update(status="missing", detail="start OpenVINO Model Server yourself (docs/OPENVINO.md)")
                return status()
            exe, cfg = ovms_dir() / "ovms.exe", config_path()
            if not exe.is_file():
                _state.update(status="missing", detail=f"ovms.exe not found in {exe.parent}")
                return status()
            if not cfg.is_file():
                _state.update(status="missing", detail=f"no models yet: {cfg} is missing")
                return status()
            if _proc is None or _proc.poll() is not None:
                (settings.data_dir / "ovms" / "cache").mkdir(parents=True, exist_ok=True)
                logs = settings.data_dir / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                out = open(logs / "ovms.log", "ab")
                _proc = subprocess.Popen(command(), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                log.info("started OpenVINO Model Server (pid %s)", _proc.pid)
            _state.update(status="starting", detail="loading the models (first start compiles them, up to a minute)")
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                if _proc.poll() is not None:
                    _state.update(status="failed", detail=f"OVMS exited with code {_proc.returncode}; see logs\\ovms.log")
                    return status()
                if _up():
                    _state.update(status="running", detail="")
                    return status()
                time.sleep(1)
            return status()  # still starting; a later check will see it
        except Exception as exc:
            log.exception("could not start OVMS")
            _state.update(status="failed", detail=str(exc))
            return status()


def ensure_running_async() -> None:
    """The same in the background, so app startup and a chat reply never wait for it."""
    threading.Thread(target=ensure_running, name="ovms-start", daemon=True).start()


def stop() -> None:
    """Stops the OVMS this backend started (and its PowerShell parent). One started by hand is left alone."""
    global _proc
    if _proc is None or _proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/pid", str(_proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            _proc.terminate()
    except Exception:
        log.warning("could not stop OVMS", exc_info=True)
    _proc = None

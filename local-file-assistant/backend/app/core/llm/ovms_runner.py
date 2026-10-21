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
config_lock = threading.RLock()  # config.json and graph.pbtxt: one read-modify-write (and restart) at a time
_proc: subprocess.Popen | None = None
_state = {"status": "unknown", "detail": ""}  # unknown | running | starting | missing | failed | off


# The installer ships OVMS next to the frozen backend: resources\backend\lfa-backend.exe and resources\ovms\ovms.exe.
BUNDLED_OVMS = Path(sys.executable).parent.parent / "ovms"


def ovms_dir() -> Path:
    """OVMS_DIR if set, else the copy the installer shipped, else %LOCALAPPDATA%\\ovms (a dev setup, docs/OPENVINO.md)."""
    if settings.ovms_dir:
        return Path(settings.ovms_dir)
    if getattr(sys, "frozen", False) and (BUNDLED_OVMS / "ovms.exe").is_file():
        return BUNDLED_OVMS
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ovms"


def config_path() -> Path:
    return settings.data_dir / "ovms" / "models" / "config.json"


def write_atomic(path: Path, text: str) -> None:
    """Writes beside the file, then swaps it in whole: OVMS re-reads config.json while it runs and must never see half a
    file, and a crash mid-write must not leave a broken one behind."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        for pause in (*[0.1] * 19, None):  # Windows refuses the swap while OVMS has the file open (it re-reads every second)
            try:
                return os.replace(tmp, path)
            except PermissionError:
                if pause is None:
                    raise
                time.sleep(pause)
    finally:
        tmp.unlink(missing_ok=True)


def _port() -> int:
    return urlparse(settings.ov_base_url).port or 8000


def _up() -> bool:
    from app.core.llm.client import server_status

    return bool(server_status().get("connected"))


def ps_quote(p) -> str:
    """A PowerShell single-quoted string."""
    return "'" + str(p).replace("'", "''") + "'"


def ovms_command(args: str) -> list[str]:
    """PowerShell runs OVMS's own setupvars.ps1 first (DLL and Python paths), then ovms.exe with args."""
    d = ovms_dir()
    script = f"& {ps_quote(d / 'setupvars.ps1')} | Out-Null; & {ps_quote(d / 'ovms.exe')} {args}"
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def command() -> list[str]:
    """The server itself, with the same flags as ovms-start.ps1."""
    cache = settings.data_dir / "ovms" / "cache"
    return ovms_command(f"--rest_port {_port()} --rest_bind_address 127.0.0.1 --config_path {ps_quote(config_path())} --cache_dir {ps_quote(cache)}")


def status() -> dict:
    return dict(_state)


DEVICES = ("CPU", "GPU", "NPU")


def available_devices() -> list[str]:
    """What OVMS found on this PC, from the line it logs at every start ("Available devices for Open VINO: CPU, GPU,
    NPU"). Before OVMS ever started there is no log: CPU and GPU are offered (NPU only once OVMS has seen one)."""
    try:
        text = (settings.data_dir / "logs" / "ovms.log").read_text(encoding="utf-8", errors="replace")
        line = text[text.rindex("Available devices for Open VINO:"):].splitlines()[0]
        found = [d.strip() for d in line.split(":", 1)[1].split(",")]
        return [d for d in DEVICES if d in found] or ["CPU"]
    except (OSError, ValueError, IndexError):
        return ["CPU", "GPU"]


def resolve_device(choice: str) -> str:
    """The device a chat model is compiled for. "auto" is the app's choice, not OpenVINO's AUTO (OVMS 2026.4 can't load
    an LLM on AUTO): the iGPU when there is one, else the CPU. Never the NPU: it needs an NPU-specific model export."""
    choice = (choice or "auto").upper()
    if choice in DEVICES:
        return choice
    return "GPU" if "GPU" in available_devices() else "CPU"


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


def restart(wait: bool = False) -> bool:
    """Stops the OVMS this backend started and starts it again: in the background, or with wait=True until it answers
    (or gives up; status() says which). False when OVMS wasn't started by the app (the user runs it by hand): then it
    is left alone."""
    if _proc is None or _proc.poll() is not None:
        return False
    stop()
    if wait:
        ensure_running(wait_s=180)  # the first start on another device compiles the model
    else:
        ensure_running_async()
    return True

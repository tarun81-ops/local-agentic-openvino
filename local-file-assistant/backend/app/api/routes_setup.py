"""First-run setup (ui/src/pages/setup.js): what is still missing before the app can answer, and the model download.

OpenVINO build: the engine is OpenVINO Model Server (core/llm/ovms_runner.py). Models come from OVMS's own
`ovms --pull`, which fetches a pre-converted OpenVINO model from Hugging Face by name, so no Python export step
is needed (scripts\\ovms-export.ps1 stays for models that have no OpenVINO build). The pulled models are put in
config.json under the names "chat" and "embed", the app's defaults. The packaged Windows app starts this same
download automatically on first launch; the setup page can also start it manually."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.core import pc_access, prefs, ram
from app.core.llm import ovms_runner
from app.core.llm.client import current_model, server_status, set_model

log = logging.getLogger(__name__)
router = APIRouter(prefix="/setup", tags=["setup"])

OVMS_RELEASES = "https://github.com/openvinotoolkit/model_server/releases"
# Chat models, biggest first: the first one whose RAM floor this PC meets is recommended. The current default
# (Qwen3-1.7B) is the top of the list, so setup never recommends anything bigger than what the app was tuned on.
CHAT_MODELS = [
    {"repo": "OpenVINO/Qwen3-1.7B-int4-ov", "label": "Qwen3 1.7B (int4)", "size_mb": 1210, "min_ram_mb": 11000},  # a "12 GB" PC reports ~11.7 GB usable
    {"repo": "OpenVINO/Qwen3-0.6B-int4-ov", "label": "Qwen3 0.6B (int4)", "size_mb": 460, "min_ram_mb": 0},
]
EMBED_MODEL = {"repo": "OpenVINO/Qwen3-Embedding-0.6B-int8-ov", "label": "Qwen3 Embedding 0.6B (int8)", "size_mb": 645}
# Bigger than the default: offered in Settings > Model for a PC with RAM to spare, never recommended (the app is tuned on
# the default and a bigger model is slower on every question).
EXTRA_CHAT_MODELS = [
    {"repo": "OpenVINO/Qwen3-4B-int4-ov", "label": "Qwen3 4B (int4)", "size_mb": 2290, "min_ram_mb": 15000},
]


def chat_device() -> str:
    """The device picked in Settings > Model (default Auto: the iGPU if there is one, else the CPU)."""
    return ovms_runner.resolve_device(prefs.get("ov_device", "auto"))


def chat_item(model: dict, name: str) -> dict:
    """One download: model is a CHAT_MODELS / EXTRA_CHAT_MODELS entry, served under `name`."""
    item = {"name": name, **model, "args": f"--task text_generation --target_device {chat_device()}"}
    item["command"] = f"ovms --pull --source_model {item['repo']} --model_repository_path \"{models_dir()}\" {item['args']} --overwrite_models"
    return item


def recommended_chat(total_mb: int) -> dict:
    """By total RAM, not free RAM: free RAM changes from minute to minute, and the recommendation must not."""
    return next(m for m in CHAT_MODELS if total_mb >= m["min_ram_mb"])


def models_dir() -> Path:
    return ovms_runner.config_path().parent


def _config() -> dict:
    try:
        return json.loads(ovms_runner.config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model_config_list": []}


def installed() -> dict[str, str]:
    """{name: base_path} of the models in config.json with an OVMS graph file. Works while OVMS is down."""
    out = {}
    for entry in _config().get("model_config_list", []):
        c = entry.get("config", {})
        model_path = models_dir() / c.get("base_path", "")
        if c.get("name") and c.get("base_path") and (model_path / "graph.pbtxt").is_file():
            out[c["name"]] = c["base_path"]
    return out


def _edit_config(edit) -> None:
    """Read-modify-write of config.json: one writer at a time (a download finishing while a model is removed must not
    lose either change), swapped in whole so OVMS never reads half a file."""
    with ovms_runner.config_lock:
        cfg = _config()
        edit(cfg)
        path = ovms_runner.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ovms_runner.write_atomic(path, json.dumps(cfg, indent=4))


def _add_to_config(name: str, base_path: str) -> None:
    """Adds or replaces one model in config.json; OVMS notices the change and loads it (no restart)."""
    def add(cfg: dict) -> None:
        rest = [e for e in cfg.get("model_config_list", []) if e.get("config", {}).get("name") != name]
        cfg["model_config_list"] = rest + [{"config": {"name": name, "base_path": base_path}}]

    _edit_config(add)


def plan(total_mb: int) -> list[dict]:
    """The downloads still needed: one per missing model name (chat, embed)."""
    have = installed()
    chat = recommended_chat(total_mb)
    out = []
    if settings.ov_chat_model not in have:
        out.append({"name": settings.ov_chat_model, **chat, "args": f"--task text_generation --target_device {chat_device()}"})
    if settings.embedding_model not in have:
        # Qwen3-Embedding needs LAST pooling; OVMS's guess (CLS) gives no error but much worse search (docs/OPENVINO.md).
        out.append({"name": settings.embedding_model, **EMBED_MODEL, "args": "--task embeddings --pooling LAST --target_device CPU"})
    for item in out:
        item["command"] = f"ovms --pull --source_model {item['repo']} --model_repository_path \"{models_dir()}\" {item['args']} --overwrite_models"
    return out


def engine() -> dict:
    """OVMS: installed? running? Its states map to what the wizard shows: ready / starting / warming up / missing / failed."""
    exe = ovms_runner.ovms_dir() / "ovms.exe"
    if sys.platform == "win32" and not exe.is_file():
        return {"name": "OpenVINO Model Server", "state": "missing", "ok": False,
                "detail": f"OpenVINO Model Server isn't installed. Download the Windows zip and unpack it to {exe.parent}, then click Check again.",
                "help": {"label": "Download OpenVINO Model Server", "url": OVMS_RELEASES}}
    if not installed():  # checked first: OVMS can't run without models, and asking a closed port costs ~2 s on Windows
        return {"name": "OpenVINO Model Server", "state": "installed", "ok": True,
                "detail": "Installed. It starts once the models are downloaded (next step)."}
    probe = server_status()
    if probe["connected"] and probe["required_model_available"]:
        return {"name": "OpenVINO Model Server", "state": "ready", "ok": True, "detail": ""}
    if probe["connected"]:
        return {
            "name": "OpenVINO Model Server",
            "state": "failed",
            "ok": False,
            "detail": f"OVMS responds, but the selected model '{probe['required_model']}' is not available. Check the model repository and config.json.",
        }
    run = ovms_runner.status()
    state = {"running": "ready", "starting": "warming up", "failed": "failed", "missing": "failed", "off": "off"}.get(run["status"], "starting")
    if state == "off":
        return {"name": "OpenVINO Model Server", "state": "off", "ok": False,
                "detail": "Starting it automatically is switched off (OVMS_AUTOSTART=false). Start it with scripts\\ovms-start.ps1, then click Check again."}
    return {"name": "OpenVINO Model Server", "state": state, "ok": state != "failed", "detail": run.get("detail", "")}


# ---------- the download job: one at a time, keeps running if the wizard is closed ----------

_lock = threading.Lock()
_job: dict = {"status": "idle"}  # idle | running | done | failed | cancelled
_proc: subprocess.Popen | None = None
_cancel = threading.Event()
_bootstrap_lock = threading.Lock()
_bootstrap_thread: threading.Thread | None = None
_bootstrap: dict = {"status": "idle", "detail": ""}


def _mb(path: Path) -> int:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // 1_000_000
    except OSError:
        return 0


def why_failed(lines: list[str]) -> str:
    """ovms --pull's output in plain words. Its last line is often unrelated (a missing model ends with
    "HF_TOKEN env variable is not set"), so known causes are matched anywhere; the raw output goes to the log."""
    text = " ".join(lines).lower()
    if "path too long" in text:
        return "the models folder path is too long for Windows. Set DATA_DIR in backend\\.env to a shorter folder, then try again."
    if "authentication is required" in text or "model is missing" in text:
        return "Hugging Face doesn't have this model (or it needs a login). Check the model name."
    if "no space" in text or "disk full" in text:
        return "the disk is full. Free some space, then click Download again."
    return "check the internet connection, then click Download to try again. (Details are in logs\\backend.log.)"


def _kill(proc: subprocess.Popen) -> None:
    """Ends ovms.exe and its PowerShell parent."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        proc.kill()


def _run(items: list[dict]) -> None:
    global _proc
    total = sum(i["size_mb"] for i in items) or 1
    done_mb = 0
    try:
        for n, item in enumerate(items, 1):
            _job.update(step=n, steps=len(items), model=item["label"], detail="connecting to huggingface.co")
            out = models_dir() / item["repo"]
            out.parent.mkdir(parents=True, exist_ok=True)
            proc = _proc = subprocess.Popen(
                ovms_runner.ovms_command(f"--pull --source_model {item['repo']} --model_repository_path {ovms_runner.ps_quote(models_dir())} {item['args']} --overwrite_models"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines: list[str] = []
            reader = threading.Thread(target=lambda: [lines.append(l.strip()) for l in proc.stdout if l.strip()], daemon=True)
            reader.start()
            # ponytail: progress is the size of the model folder against the size Hugging Face lists, because ovms --pull's
            # own output isn't a stable format; capped at 99% until OVMS says it finished.
            while proc.poll() is None:
                if _cancel.is_set():
                    _kill(proc)
                    _job.update(status="cancelled", detail="Download cancelled. Click Download to start again.")
                    return
                got = min(_mb(out), item["size_mb"])
                _job.update(mb=done_mb + got, total_mb=total, percent=min(99, (done_mb + got) * 100 // total),
                            detail=lines[-1][:200] if lines else _job["detail"])
                time.sleep(0.5)
            reader.join(timeout=5)
            if proc.returncode != 0:
                log.warning("ovms --pull %s failed (%s): %s", item["repo"], proc.returncode, " | ".join(lines[-8:]))
                _job.update(status="failed", detail=f"Downloading {item['label']} failed: {why_failed(lines)}")
                return
            _add_to_config(item["name"], str(Path(item["repo"])))
            done_mb += item["size_mb"]
        chat_items = [item for item in items if item["name"] != settings.embedding_model]
        if chat_items:
            set_model(chat_items[-1]["name"])
        _job.update(
            phase="starting",
            mb=total,
            total_mb=total,
            percent=100,
            detail=f"Downloaded. Starting OpenVINO and loading {current_model(local=True)}…",
        )
        ready = ovms_runner.ensure_running()
        if ready["status"] == "running":
            _job.update(
                status="done",
                phase="ready",
                detail=f"{current_model(local=True)} is ready in OpenVINO. You can start chatting.",
            )
        else:
            _job.update(
                status="failed",
                phase="failed",
                detail=(
                    f"The model downloaded, but OpenVINO couldn't load it. {ready.get('detail') or 'Check Settings > Model & backend and try again.'}"
                ),
            )
    except Exception as exc:
        log.exception("model download failed")
        _job.update(status="failed", detail=f"The download stopped: {exc}. Click Download to try again.")
    finally:
        _proc = None


def start_pull(items: list[dict] | None = None) -> dict:
    """items: what to download; None = whatever setup still needs (plan())."""
    with _lock:
        if _job["status"] == "running":
            return dict(_job)
        items = items if items is not None else plan(ram.status()["total_mb"])
        if not items:
            _job.clear()
            _job.update(status="done", percent=100, detail="The models are already downloaded.")
            return dict(_job)
        _cancel.clear()
        _job.clear()
        _job.update(status="running", phase="downloading", percent=0, mb=0, total_mb=sum(i["size_mb"] for i in items), detail="starting")
        threading.Thread(target=_run, args=(items,), name="model-pull", daemon=True).start()
        return dict(_job)


def _ensure_default_config() -> None:
    """Create the default aliases on a fresh install; setup/download keeps their paths in sync afterward."""
    path = ovms_runner.config_path()
    chat = recommended_chat(ram.status()["total_mb"])
    with ovms_runner.config_lock:
        try:
            original = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            original = {}
        cfg = dict(original) if isinstance(original, dict) else {}
        entries = cfg.get("model_config_list", [])
        if not isinstance(entries, list):
            entries = []
        entries = [
            entry for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("config"), dict)
            and isinstance(entry["config"].get("name"), str)
            and isinstance(entry["config"].get("base_path"), str)
            and entry["config"]["name"]
            and entry["config"]["base_path"]
        ]
        names = {entry["config"]["name"] for entry in entries}
        defaults = (
            (settings.ov_chat_model, chat["repo"]),
            (settings.embedding_model, EMBED_MODEL["repo"]),
        )
        for name, base_path in defaults:
            if name not in names:
                entries.append({"config": {"name": name, "base_path": base_path}})
        cfg["model_config_list"] = entries
        if path.is_file() and cfg == original:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ovms_runner.write_atomic(path, json.dumps(cfg, indent=4))
    log.info("created default OVMS model config at %s", path)


def _run_bootstrap() -> None:
    """Keep repairing/downloading/restarting until the packaged app has a usable local model engine."""
    while True:
        try:
            _bootstrap.update(status="checking", detail="Checking the local model engine…")
            ovms_home = ovms_runner.ovms_dir()
            missing = [
                path for path in (ovms_home / "ovms.exe", ovms_home / "setupvars.ps1")
                if not path.is_file()
            ]
            if ovms_runner.python_stdlib_dir(ovms_home) is None:
                missing.append(ovms_home / "python")
            if missing:
                detail = f"Bundled or installed OVMS is incomplete at {ovms_home}; missing {', '.join(map(str, missing))}."
                log.error(detail)
                _bootstrap.update(status="retrying", detail=f"{detail} Retrying automatically.")
                time.sleep(30)
                continue

            model_root = models_dir()
            model_root.mkdir(parents=True, exist_ok=True)
            (settings.data_dir / "ovms" / "cache").mkdir(parents=True, exist_ok=True)
            _ensure_default_config()

            pending = plan(ram.status()["total_mb"])
            if pending:
                _bootstrap.update(status="downloading", detail="Downloading the local models. This can take a while.")
                log.info("automatic OpenVINO setup needs %s", ", ".join(item["name"] for item in pending))
                job = start_pull(pending)
                while job["status"] == "running":
                    time.sleep(1)
                    job = dict(_job)
                if job["status"] != "done":
                    detail = job.get("detail") or "The model download did not finish."
                    log.error("automatic OpenVINO model setup failed: %s", detail)
                    _bootstrap.update(status="retrying", detail=f"{detail} Retrying automatically.")
                    time.sleep(30)
                    continue

            _bootstrap.update(status="starting", detail="Starting OpenVINO and loading the models…")
            ready = ovms_runner.ensure_running()
            if ready["status"] == "running":
                _bootstrap.update(status="ready", detail="")
                log.info("automatic OpenVINO setup is ready")
                return

            detail = ready.get("detail") or "OpenVINO is not ready yet."
            log.error("automatic OpenVINO startup failed: %s", detail)
            _bootstrap.update(status="retrying", detail=f"{detail} Retrying automatically.")
            time.sleep(15)
        except Exception as exc:
            log.exception("automatic OpenVINO setup failed")
            _bootstrap.update(status="retrying", detail=f"OpenVINO setup hit an error: {exc}. Retrying automatically.")
            time.sleep(30)


def start_bootstrap() -> None:
    """Starts the packaged Windows first-run setup once; development keeps its existing manual setup flow."""
    global _bootstrap_thread
    with _bootstrap_lock:
        if _bootstrap_thread and _bootstrap_thread.is_alive():
            return
        _bootstrap.update(status="checking", detail="Checking the local model engine…")
        _bootstrap_thread = threading.Thread(target=_run_bootstrap, name="openvino-bootstrap", daemon=True)
        _bootstrap_thread.start()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------- routes ----------

@router.get("/status")
def get_status():
    """Engine, models, PC access, and what is still missing (an empty `missing` means the app can answer)."""
    mem = ram.status()
    eng = engine()
    todo = plan(mem["total_mb"])
    missing = ([] if eng["ok"] else ["engine"]) + (["model"] if todo else [])
    pc = pc_access.config()
    # An install that already works and has PC access on was set up before this wizard existed: don't show it again.
    done = prefs.get("setup_done")
    if done is None:
        done = not missing and pc["enabled"]
    return {
        "done": bool(done),
        "engine": eng,
        "model": {
            "recommended": {**recommended_chat(mem["total_mb"]), "why": f"for a PC with {round(mem['total_mb'] / 1024)} GB of RAM"},
            "installed": sorted(installed()),
            "needed": [{k: i[k] for k in ("name", "label", "repo", "size_mb", "command")} for i in todo],
            "source": "huggingface.co",
            "pull": dict(_job),
        },
        "bootstrap": dict(_bootstrap),
        "folders": {"pc_access": pc["enabled"], "drives": pc["drives"]},
        "memory": mem,
        "missing": missing,
    }


class PullRequest(BaseModel):
    repo: str | None = None  # a catalog model (Settings > Model); none = what setup still needs


@router.post("/model/pull")
def pull(req: PullRequest | None = None):
    """Starts the download (or joins the running one) and streams `progress` events until it ends with
    `done`, `failed` or `cancelled`. Leaving the page doesn't stop it; POST /setup/model/pull/cancel does."""
    items = None
    if req and req.repo:
        model = next((m for m in CHAT_MODELS + EXTRA_CHAT_MODELS if m["repo"] == req.repo), None)
        if model is None:
            raise HTTPException(400, "That model isn't in the list this app can download.")
        items = [chat_item(model, req.repo.split("/")[-1])]  # served under its own name; "Use" switches to it
    first = start_pull(items)

    def stream():
        state = first
        while state["status"] == "running":
            yield _sse("progress", state)
            time.sleep(0.5)
            state = dict(_job)
        yield _sse(state["status"], state)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/model/pull/cancel")
def cancel():
    _cancel.set()
    return {"ok": True}


@router.post("/done")
def finish():
    prefs.set("setup_done", True)
    return {"done": True}

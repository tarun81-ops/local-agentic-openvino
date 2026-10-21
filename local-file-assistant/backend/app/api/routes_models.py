"""Settings > Model (OpenVINO build): the models on this PC with their size and expected RAM, the ones the app can
download, removing one, the device the chat model runs on, and the optional other OpenAI-compatible server.
Downloads reuse setup's job (POST /setup/model/pull {"repo"}); switching reuses PUT /settings/model. The app never
switches models by itself: a downloaded model is only used after "Use"."""
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core import prefs, ram
from app.core.llm import external, ovms_runner
from app.core.llm.client import current_model, server_status
from app.api import routes_setup

router = APIRouter(prefix="/models", tags=["models"])


def expected_ram_mb(size_mb: int) -> int:
    """ponytail: weights plus ~30% for the KV cache and runtime, plus 400 MB; measured roughly on Qwen3 int4 under OVMS.
    Replace with a measured figure per model if the guess proves far off."""
    return round(size_mb * 1.3 + 400)


def _entry(name: str, label: str, size_mb: int, kind: str, installed: bool, repo: str = "") -> dict:
    rec = routes_setup.recommended_chat(ram.status()["total_mb"])["repo"]
    return {"name": name, "label": label, "repo": repo, "size_mb": size_mb, "ram_mb": expected_ram_mb(size_mb), "kind": kind,
            "installed": installed, "active": installed and name == current_model(),
            "recommended": kind == "chat" and bool(repo) and repo.split("/")[-1] == rec.split("/")[-1]}


@router.get("")
def list_models():
    have = routes_setup.installed()
    out, have_repos = [], set()
    for name, base in have.items():
        leaf = Path(base).name
        have_repos.add(leaf)
        kind = "embed" if name == settings.embedding_model else "chat"
        out.append(_entry(name, leaf, routes_setup._mb(routes_setup.models_dir() / base), kind, True, base.replace("\\", "/")))
    for m in routes_setup.CHAT_MODELS + routes_setup.EXTRA_CHAT_MODELS:
        if m["repo"].split("/")[-1] not in have_repos:
            out.append(_entry(m["repo"].split("/")[-1], m["label"], m["size_mb"], "chat", False, m["repo"]))
    return {
        "models": out,
        "device": {"choice": prefs.get("ov_device", "auto"), "resolved": routes_setup.chat_device(),
                   "available": ovms_runner.available_devices()},
        "external": external.get(),
        "memory": ram.status(),
    }


class NameRequest(BaseModel):
    name: str


@router.post("/remove")
def remove_model(req: NameRequest):
    """Takes a downloaded model out of OVMS's config and deletes its folder (in the app's models folder only)."""
    have = routes_setup.installed()
    if req.name not in have:
        raise HTTPException(404, "That model isn't downloaded.")
    if req.name == current_model() or req.name == settings.embedding_model:
        raise HTTPException(400, "This model is in use. Switch to another model first (Use), then remove it.")
    def drop(cfg: dict) -> None:
        cfg["model_config_list"] = [e for e in cfg.get("model_config_list", []) if e.get("config", {}).get("name") != req.name]

    routes_setup._edit_config(drop)
    folder = (routes_setup.models_dir() / have[req.name]).resolve()
    if routes_setup.models_dir().resolve() in folder.parents and not any(have[n] == have[req.name] for n in have if n != req.name):
        shutil.rmtree(folder, ignore_errors=True)  # the app downloaded it; still used under another name = kept
    return {"removed": req.name}


class DeviceRequest(BaseModel):
    device: str  # auto | CPU | GPU | NPU


_DEVICE_LINE = re.compile(r'(\bdevice:\s*")[A-Z.:,]+(")')


@router.put("/device")
def set_device(req: DeviceRequest):
    """Recompiles the chat model for another device: rewrites `device:` in its graph.pbtxt and restarts the OVMS the app
    started (a hand-started one has to be restarted by hand). The first start on a new device compiles the model."""
    choice = req.device if req.device.upper() in ovms_runner.DEVICES else "auto"
    device = ovms_runner.resolve_device(choice)
    if device not in ovms_runner.available_devices():
        raise HTTPException(400, f"This PC has no {device} that OpenVINO can use.")
    base = routes_setup.installed().get(current_model())
    if base is None:
        raise HTTPException(400, "The chat model isn't downloaded yet. Finish setup first.")
    graph = routes_setup.models_dir() / base / "graph.pbtxt"
    with ovms_runner.config_lock:
        try:
            text = graph.read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(400, "This model has no graph.pbtxt to change; download it again from this page.")
        new, found = _DEVICE_LINE.subn(rf"\g<1>{device}\g<2>", text)
        if not found:  # a graph in another layout: saying "now runs on GPU" after changing nothing would be a lie
            raise HTTPException(400, "This model's graph.pbtxt has no device line the app can change; download the model again from this page.")
        ovms_runner.write_atomic(graph, new)
        restarted = ovms_runner.restart(wait=True)
        if restarted and current_model() not in server_status()["models"]:  # the new device didn't load it: the old file goes back
            ovms_runner.write_atomic(graph, text)
            ovms_runner.restart(wait=True)
            raise HTTPException(500, f"The chat model didn't start on {device}, so it is back on the device it had before. See logs\\ovms.log.")
    prefs.set("ov_device", choice.lower() if choice == "auto" else device)
    note = "" if restarted else " Restart OpenVINO Model Server to apply it (it was started outside the app)."
    warn = " NPU needs a model exported for it; if the chat stops working, switch back to Auto." if device == "NPU" else ""
    return {"device": device, "restarted": restarted, "detail": f"The chat model now runs on {device}.{note}{warn}"}


class ExternalRequest(BaseModel):
    enabled: bool
    url: str = ""
    model: str = ""
    confirm_remote: bool = False


@router.put("/external")
def set_external(req: ExternalRequest):
    try:
        return external.set(req.enabled, req.url, req.model, req.confirm_remote)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.core import ram
from app.core.llm import ovms_runner
from app.core.llm.client import current_model, server_status, set_model
from app.core.parsers import PARSERS
from app.core.search import reranker

router = APIRouter(prefix="/settings", tags=["settings"])


class ModelRequest(BaseModel):
    model: str


@router.get("")
def get_settings():
    return {
        "model": current_model(),
        "embedding_model": settings.embedding_model,
        "base_url": settings.ov_base_url,
        "data_dir": str(settings.data_dir),
        "file_types": sorted(PARSERS),
        "reranker": settings.rerank_model if reranker.available() else "",  # "" = off or not downloaded
        "server": {**server_status(), "autostart": ovms_runner.status()},
        "provider": "openvino",
        "memory": ram.status(),
    }


@router.put("/model")
def change_model(req: ModelRequest):
    status = server_status()
    if status["connected"] and req.model not in status["models"]:
        raise HTTPException(status_code=400, detail=f"{req.model} isn't served by OpenVINO Model Server. Export it with scripts\\ovms-export.ps1 and restart OVMS.")
    set_model(req.model)
    return {"model": req.model}

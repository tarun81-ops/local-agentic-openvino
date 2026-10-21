"""Settings > Plug-in tools: add, switch on/off and remove local MCP servers, and mark which of their tools change
things (those then ask before running). See core/assistant/tools/mcp.py."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.assistant.tools import mcp

router = APIRouter(prefix="/mcp", tags=["mcp"])


class NewServer(BaseModel):
    name: str
    command: str
    args: list[str] = []


class ServerPatch(BaseModel):
    enabled: bool | None = None
    writes: list[str] | None = None  # tool names that change things or send data: they ask first


@router.get("")
def list_servers():
    return {"servers": mcp.status(), "max_steps": mcp.MAX_STEPS}


@router.post("/servers")
def add(body: NewServer):
    try:
        mcp.add_server(body.name, body.command, body.args)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"servers": mcp.status()}


@router.patch("/servers/{name}")
def patch(name: str, body: ServerPatch):
    try:
        mcp.update_server(name, body.enabled, body.writes)
    except KeyError:
        raise HTTPException(404, "No such plug-in server.")
    return {"servers": mcp.status()}


@router.delete("/servers/{name}")
def remove(name: str):
    mcp.remove_server(name)
    return {"servers": mcp.status()}

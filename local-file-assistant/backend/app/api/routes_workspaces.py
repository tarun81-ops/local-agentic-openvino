"""Workspaces: create, list, edit, delete (core/assistant/workspaces.py). Deleting one never deletes files."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.assistant import workspaces

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class Workspace(BaseModel):
    name: str
    roots: list[str] = []  # [] = the whole PC
    answer_style: str = ""  # "" = the saved default


@router.get("")
def list_workspaces():
    return {"workspaces": workspaces.list_all()}


@router.post("")
def create_workspace(body: Workspace):
    try:
        return workspaces.create(body.name, body.roots, body.answer_style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/{ws_id}")
def update_workspace(ws_id: int, body: Workspace):
    if workspaces.get(ws_id) is None:
        raise HTTPException(404, "That workspace no longer exists.")
    try:
        return workspaces.update(ws_id, body.name, body.roots, body.answer_style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/{ws_id}")
def delete_workspace(ws_id: int):
    workspaces.delete(ws_id)
    return {"ok": True}

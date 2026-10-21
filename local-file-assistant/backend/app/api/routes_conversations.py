from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.assistant import chat_recall, conversation

router = APIRouter(prefix="/conversations", tags=["conversations"])


class Patch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


class Import(BaseModel):
    chats: list[dict]


@router.get("")
def list_conversations(workspace: int | None = None):
    """The chats of one workspace; no `workspace` = the "All files" chats."""
    return {"conversations": conversation.list_all(workspace)}


@router.post("")
def create_conversation():
    return conversation.create()


@router.post("/import")
def import_conversations(body: Import):
    return {"imported": conversation.import_chats(body.chats)}


@router.get("/search")
def search_conversations(q: str, limit: int = 8):
    """Past exchanges that match `q` (same retrieval the assistant uses to remember earlier chats)."""
    hits = chat_recall.recall(q, k=max(1, min(limit, 20)))
    return {"results": [{"conv_id": h["conv_id"], "title": h["title"], "msg_id": h["msg_id"], "snippet": h["text"][:300], "at": h["created_at"]} for h in hits]}


@router.get("/{conv_id}")
def get_conversation(conv_id: int):
    conv = conversation.get(conv_id)
    if conv is None:
        raise HTTPException(404, "No such conversation")
    return conv


@router.patch("/{conv_id}")
def patch_conversation(conv_id: int, body: Patch):
    if not conversation.update(conv_id, body.title, body.pinned):
        raise HTTPException(404, "No such conversation")
    return {"ok": True}


@router.delete("/{conv_id}")
def delete_conversation(conv_id: int):
    conversation.delete(conv_id)
    return {"ok": True}

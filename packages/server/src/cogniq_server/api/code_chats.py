"""Code Chats API — standalone conversations with Claude Code."""

import asyncio
import json
import logging
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cogniq_server.auth.dependencies import get_current_user
from cogniq_server.auth.jwt import decode_token
from cogniq_server.auth.models import User
from cogniq_server.dependencies import get_db, get_task_queue
from cogniq_shared.domain.code_chat import CodeChat
from cogniq_shared.domain.code_session import CodeMessage, QueuedMessage
from cogniq_shared.domain.code_chat_repository import CodeChatRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_chat_repo(db: AsyncIOMotorDatabase = Depends(get_db)) -> CodeChatRepository:
    return CodeChatRepository(db)


# ── DTOs ──

class CreateChatRequest(BaseModel):
    prompt: str


class ContinueChatRequest(BaseModel):
    prompt: str


def _chat_summary(c: CodeChat) -> dict:
    last_msg = c.messages[-1] if c.messages else None
    preview = ""
    if last_msg:
        content = last_msg.content
        preview = (str(content)[:80] + "...") if len(str(content)) > 80 else str(content)
    return {
        "id": c.id,
        "projectId": c.project_id,
        "title": c.title or preview or "New chat",
        "status": c.status,
        "model": c.model,
        "totalTurns": c.total_turns,
        "totalCostUsd": c.total_cost_usd,
        "queueLength": len(c.message_queue),
        "createdAt": c.created_at.isoformat(),
        "updatedAt": c.updated_at.isoformat(),
    }


def _chat_detail(c: CodeChat) -> dict:
    result = _chat_summary(c)
    result["cliSessionId"] = c.cli_session_id
    result["totalTokens"] = c.total_tokens
    result["messages"] = [
        {
            "id": m.message_id,
            "turn": m.turn,
            "role": m.role,
            "content": m.content,
            "toolName": m.tool_name,
            "toolInput": m.tool_input,
            "tokens": m.tokens,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        }
        for m in c.messages
    ]
    return result


# ── Endpoints ──

@router.get("/projects/{project_id}/code-chats")
async def list_code_chats(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: CodeChatRepository = Depends(_get_chat_repo),
):
    chats = await repo.list_by_project(project_id)
    return [_chat_summary(c) for c in chats]


@router.post("/projects/{project_id}/code-chats")
async def create_code_chat(
    project_id: str,
    body: CreateChatRequest,
    user: User = Depends(get_current_user),
    repo: CodeChatRepository = Depends(_get_chat_repo),
    task_queue: TaskQueueRepository = Depends(get_task_queue),
):
    """Create a new standalone code chat and send the first message."""
    # Create chat document
    title = body.prompt[:50] + ("..." if len(body.prompt) > 50 else "")
    chat = CodeChat(project_id=project_id, title=title, status="running")
    chat_id = await repo.create(chat)

    # Save user message
    user_msg = CodeMessage(turn=1, role="user", content=body.prompt)
    await repo.add_message(chat_id, user_msg)

    # Enqueue task for worker
    task_id = await task_queue.enqueue(
        issue_id=chat_id,  # reuse issue_id field for chat_id
        project_id=project_id,
        stage="code_chat",
        config={"chat_id": chat_id, "prompt": body.prompt},
    )

    return {"id": chat_id, "taskId": task_id}


@router.get("/code-chats/{chat_id}")
async def get_code_chat(
    chat_id: str,
    user: User = Depends(get_current_user),
    repo: CodeChatRepository = Depends(_get_chat_repo),
):
    chat = await repo.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return _chat_detail(chat)


@router.post("/code-chats/{chat_id}/continue")
async def continue_code_chat(
    chat_id: str,
    body: ContinueChatRequest,
    user: User = Depends(get_current_user),
    repo: CodeChatRepository = Depends(_get_chat_repo),
    task_queue: TaskQueueRepository = Depends(get_task_queue),
):
    chat = await repo.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Always enqueue to message_queue (FIFO)
    queued = QueuedMessage(prompt=body.prompt)
    await repo.enqueue_message(chat_id, queued)
    new_queue_len = len(chat.message_queue) + 1

    if chat.status == "running":
        active_task = await task_queue.find_active(chat_id, stage="code_chat")
        if active_task:
            return {"status": "queued", "queueLength": new_queue_len}
        # Stale running — reset
        await repo.update(chat_id, {"status": "completed"})

    # Set running and create task
    current_status = "completed" if chat.status == "running" else chat.status
    changed = await repo.update(chat_id, {"status": "running"}, only_if_status=current_status)
    if not changed:
        return {"status": "queued", "queueLength": new_queue_len}

    task_id = await task_queue.enqueue(
        issue_id=chat_id,
        project_id=chat.project_id,
        stage="code_chat",
        config={"chat_id": chat_id},
    )
    return {"status": "queued", "taskId": task_id, "queueLength": new_queue_len}


@router.post("/code-chats/{chat_id}/interrupt")
async def interrupt_code_chat(
    chat_id: str,
    user: User = Depends(get_current_user),
    repo: CodeChatRepository = Depends(_get_chat_repo),
):
    chat = await repo.get(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.status != "running":
        raise HTTPException(status_code=409, detail="Chat is not running")
    updated = await repo.request_interrupt(chat_id)
    if not updated:
        raise HTTPException(status_code=409, detail="Failed to set interrupt flag")
    return {"status": "interrupt_requested"}


async def _auth_sse_chat(token: str = Query(...), db: AsyncIOMotorDatabase = Depends(get_db)) -> User:
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_doc = await db["users"].find_one({"_id": user_id})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    return User.model_validate(user_doc)


@router.get("/code-chats/{chat_id}/stream")
async def stream_chat_events(
    chat_id: str,
    user: User = Depends(_auth_sse_chat),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    async def event_generator():
        yield f"data: {json.dumps({'type': 'connected', 'chatId': chat_id})}\n\n"
        pipeline = [
            {"$match": {
                "documentKey._id": chat_id,
                "operationType": {"$in": ["update", "replace"]},
            }}
        ]
        try:
            async with db["code_chats"].watch(pipeline) as stream:
                async for change in stream:
                    payload: dict[str, Any] = {"type": "chat_update", "chatId": chat_id}
                    yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

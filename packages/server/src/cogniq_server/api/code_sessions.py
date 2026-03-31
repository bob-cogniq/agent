"""Code Sessions API — view Claude Code execution history and continue sessions."""

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
from cogniq_server.dependencies import get_db, get_issue_repository, get_task_queue
from cogniq_shared.config import settings
from cogniq_shared.domain.code_session import CodeSession, CodeMessage, QueuedMessage
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request DTOs ──

class ContinueRequest(BaseModel):
    prompt: str


# ── Response DTOs ──

def _session_summary(s: CodeSession) -> dict:
    """Lightweight session info (no messages, no diff_content in changed_files)."""
    return {
        "id": s.session_id,
        "cliSessionId": s.cli_session_id,
        "runId": s.run_id,
        "status": s.status,
        "model": s.model,
        "totalTurns": s.total_turns,
        "totalTokens": s.total_tokens,
        "totalCostUsd": s.total_cost_usd,
        "changedFiles": [
            {"path": f.path, "status": f.status, "additions": f.additions, "deletions": f.deletions}
            for f in s.changed_files
        ],
        "queueLength": len(s.message_queue),
        "error": s.error,
        "startedAt": s.started_at.isoformat() if s.started_at else None,
        "completedAt": s.completed_at.isoformat() if s.completed_at else None,
    }


def _message_to_dict(m: CodeMessage) -> dict:
    return {
        "id": m.message_id,
        "turn": m.turn,
        "role": m.role,
        "content": m.content,
        "toolName": m.tool_name,
        "toolInput": m.tool_input,
        "tokens": m.tokens,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
    }


def _session_detail(s: CodeSession) -> dict:
    """Full session info with messages and diff_content."""
    result = _session_summary(s)
    # Include diff_content in detail view
    result["changedFiles"] = [f.model_dump() for f in s.changed_files]
    result["messages"] = [_message_to_dict(m) for m in s.messages]
    return result


# ── Endpoints ──

@router.get("/issues/{issue_id}/code-sessions")
async def list_code_sessions(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    """List all code sessions for an issue (without messages)."""
    sessions = await repo.list_code_sessions(issue_id)
    return [_session_summary(s) for s in sessions]


@router.get("/issues/{issue_id}/code-sessions/{session_id}")
async def get_code_session(
    issue_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    """Get a single code session with full message history."""
    session = await repo.get_code_session(issue_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Code session not found")
    return _session_detail(session)


@router.post("/issues/{issue_id}/code-sessions/{session_id}/continue")
async def continue_code_session(
    issue_id: str,
    session_id: str,
    body: ContinueRequest,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
    task_queue: TaskQueueRepository = Depends(get_task_queue),
):
    """Continue an existing Claude Code session with a follow-up prompt.

    If the session is already running, the message is queued and will be
    processed automatically when the current run completes.
    """
    session = await repo.get_code_session(issue_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Code session not found")
    if not session.cli_session_id:
        raise HTTPException(status_code=400, detail="Session cannot be continued (no CLI session ID)")

    # Always enqueue the message to the DB queue — guarantees FIFO ordering
    queued = QueuedMessage(prompt=body.prompt)
    await repo.enqueue_message(issue_id, session_id, queued)
    new_queue_len = len(session.message_queue) + 1

    if session.status == "running":
        # Session is active — worker will drain the queue automatically
        # Safety: if no active task (stale session), create a drain task
        active_task = await task_queue.find_active(issue_id, stage="continue")
        if not active_task:
            logger.warning("No active task for running session %s — creating drain task", session_id)
            await repo.update_code_session(issue_id, session_id, {"status": "completed"})
            # Fall through to create task below
        else:
            return {"status": "queued", "queueLength": new_queue_len}

    # Session is completed/failed — set running and create a task
    # Worker will pop the FIRST message from the DB queue (FIFO)
    changed = await repo.update_code_session(
        issue_id, session_id, {"status": "running"}, only_if_status=session.status,
    )
    if not changed:
        return {"status": "queued", "queueLength": new_queue_len}

    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Enqueue a "drain" task — worker pops from DB queue, not from task config
    task_id = await task_queue.enqueue(
        issue_id=issue_id,
        project_id=issue.project_id,
        stage="continue",
        config={
            "session_id": session_id,
            "cli_session_id": session.cli_session_id,
        },
    )

    return {"status": "queued", "taskId": task_id, "queueLength": new_queue_len}


@router.post("/issues/{issue_id}/code-sessions/{session_id}/interrupt")
async def interrupt_code_session(
    issue_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    """Request interruption of a running Claude Code session.

    Sets interrupt_requested=True on the session. The Worker detects this
    via MongoDB change stream and calls client.interrupt() + drain.
    """
    session = await repo.get_code_session(issue_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Code session not found")
    if session.status != "running":
        raise HTTPException(status_code=409, detail="Session is not running")

    updated = await repo.request_interrupt(issue_id, session_id)
    if not updated:
        raise HTTPException(status_code=409, detail="Failed to set interrupt flag")

    logger.info("Interrupt requested for session %s", session_id)
    return {"status": "interrupt_requested"}


async def _auth_sse(token: str = Query(...), db: AsyncIOMotorDatabase = Depends(get_db)) -> User:
    """Authenticate SSE connections via ?token= query param (EventSource can't set headers)."""
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


@router.get("/issues/{issue_id}/stream")
async def stream_issue_events(
    issue_id: str,
    user: User = Depends(_auth_sse),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """SSE stream — pushes real-time notifications when code sessions update.

    Clients connect once and receive events whenever the issue document changes.
    Each event signals the client to refetch session data.

    Uses ?token= query param because EventSource cannot send Authorization headers.
    """
    async def event_generator():
        # Initial connection event
        yield f"data: {json.dumps({'type': 'connected', 'issueId': issue_id})}\n\n"

        pipeline = [
            {"$match": {
                "documentKey._id": issue_id,
                "operationType": {"$in": ["update", "replace"]},
            }}
        ]
        try:
            async with db["issues"].watch(pipeline) as stream:
                async for change in stream:
                    updated = change.get("updateDescription", {}).get("updatedFields", {})
                    # Emit event when code_sessions or summary fields change
                    if any(k.startswith("code_sessions") or k.startswith("summary") for k in updated):
                        payload: dict[str, Any] = {"type": "session_update", "issueId": issue_id}
                        yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Tell nginx not to buffer SSE
            "Connection": "keep-alive",
        },
    )


@router.get("/issues/{issue_id}/code-sessions/{session_id}/files")
async def get_session_file_tree(
    issue_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    """Get the file tree stored during build (from git ls-tree)."""
    session = await repo.get_code_session(issue_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Code session not found")

    changed_paths = [f.path for f in session.changed_files]
    return {
        "files": session.file_tree,
        "changedPaths": changed_paths,
    }

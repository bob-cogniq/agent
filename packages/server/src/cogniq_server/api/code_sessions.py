"""Code Sessions API — view Claude Code execution history and continue sessions."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cogniq_server.auth.dependencies import get_current_user
from cogniq_server.auth.models import User
from cogniq_server.dependencies import get_db, get_issue_repository, get_task_queue
from cogniq_shared.domain.code_session import CodeSession, CodeMessage
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository

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
    """Continue an existing Claude Code session with a follow-up prompt."""
    # Validate session exists and has CLI session ID
    session = await repo.get_code_session(issue_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Code session not found")
    if not session.cli_session_id:
        raise HTTPException(status_code=400, detail="Session cannot be continued (no CLI session ID)")
    if session.status == "running":
        raise HTTPException(status_code=409, detail="Session is already running")

    # Get issue for project_id
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Save user prompt as a message immediately
    user_msg = CodeMessage(
        turn=session.total_turns + 1,
        role="user",
        content=body.prompt,
    )
    await repo.add_code_message(issue_id, session_id, user_msg)

    # Set session status to running
    await repo.update_code_session(issue_id, session_id, {"status": "running"})

    # Enqueue continue task
    task_id = await task_queue.enqueue(
        issue_id=issue_id,
        project_id=issue.project_id,
        stage="continue",
        config={
            "session_id": session_id,
            "cli_session_id": session.cli_session_id,
            "prompt": body.prompt,
        },
    )

    return {"status": "queued", "taskId": task_id}


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

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from cogniq_server.api.schemas import RunResponse
from cogniq_server.auth.dependencies import get_current_user
from cogniq_server.auth.models import User
from cogniq_server.dependencies import get_issue_repository
from cogniq_shared.domain.issue import Run
from cogniq_shared.registry.repository import IssueRepository

router = APIRouter()


def _run_to_response(r: Run) -> RunResponse:
    return RunResponse(
        id=r.run_id,
        stage=r.stage,
        phase=r.phase,
        status=r.status,
        trigger=r.trigger,
        attemptNumber=r.attempt_number,
        tokensUsed=r.tokens_used,
        costUsd=r.cost_usd,
        error=r.error,
        logs=r.logs,
        startedAt=r.started_at.isoformat() if r.started_at else None,
        completedAt=r.completed_at.isoformat() if r.completed_at else None,
    )


@router.get("/issues/{issue_id}/runs", response_model=list[RunResponse])
async def list_runs(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    runs = await repo.list_runs(issue_id)
    return [_run_to_response(r) for r in runs]


@router.post("/issues/{issue_id}/runs", response_model=RunResponse, status_code=201)
async def create_run(
    issue_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    run = Run(
        stage=body.get("stage", "plan"),
        phase=body.get("phase"),
        status="running",
        trigger=body.get("trigger", "manual"),
        config=body.get("config", {}),
        started_at=datetime.now(timezone.utc),
    )
    await repo.add_run(issue_id, run)
    return _run_to_response(run)


@router.patch("/issues/{issue_id}/runs/{run_id}", response_model=dict)
async def update_run(
    issue_id: str,
    run_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    updates: dict[str, Any] = {}
    if "status" in body:
        updates["status"] = body["status"]
    if "phase" in body:
        updates["phase"] = body["phase"]
    if "error" in body:
        updates["error"] = body["error"]
    if "logs" in body:
        updates["logs"] = body["logs"]
    if "tokens_used" in body:
        updates["tokens_used"] = body["tokens_used"]
    if "cost_usd" in body:
        updates["cost_usd"] = body["cost_usd"]
    if body.get("status") in ("completed", "failed", "timeout"):
        updates["completed_at"] = datetime.now(timezone.utc)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    await repo.update_run(issue_id, run_id, updates)
    return {"message": "Run updated", "run_id": run_id}

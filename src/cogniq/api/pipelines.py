from fastapi import APIRouter, Depends, HTTPException

from cogniq.api.schemas import AgentRunResponse, PipelineDetailResponse, PipelineResponse
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_issue_repository
from cogniq.domain.enums import Stage
from cogniq.registry.repository import IssueRepository

router = APIRouter()

# Mapping: backend stage → frontend AgentStep
_STAGE_TO_STEPS: dict[str, list[str]] = {
    "plan": ["PO_AGENT", "DESIGNER_AGENT", "DEV_DESIGN_AGENT"],
    "build": ["CODE_IMPLEMENTATION"],
}

_RUN_STATUS_MAP = {
    "pending": "PENDING",
    "running": "RUNNING",
    "completed": "COMPLETED",
    "failed": "FAILED",
    "timeout": "FAILED",
    "skipped": "SKIPPED",
}


@router.get("/issues/{issue_id}/pipeline", response_model=PipelineDetailResponse)
async def get_pipeline(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if not issue.runs:
        # No pipeline yet
        pipeline = PipelineResponse(
            id="", issueId=issue_id, status="PENDING", currentStep="PO_AGENT"
        )
        return PipelineDetailResponse(pipeline=pipeline)

    # Build pipeline status from runs
    last_run = issue.runs[-1]
    overall_status = _RUN_STATUS_MAP.get(last_run.status, "PENDING")
    step_map = {"plan": "PO_AGENT", "build": "CODE_IMPLEMENTATION", "reflect": "PO_AGENT"}
    current_step = step_map.get(last_run.stage, "PO_AGENT")

    pipeline = PipelineResponse(
        id=last_run.run_id,
        issueId=issue_id,
        status=overall_status,
        currentStep=current_step,
        startedAt=issue.runs[0].started_at.isoformat() if issue.runs[0].started_at else None,
        completedAt=last_run.completed_at.isoformat() if last_run.completed_at else None,
    )

    # Build agent runs for each step
    agent_runs: list[AgentRunResponse] = []
    total_cost = 0.0
    total_tokens = 0

    for run in issue.runs:
        steps = _STAGE_TO_STEPS.get(run.stage, [run.stage])
        run_status = _RUN_STATUS_MAP.get(run.status, "PENDING")

        for step in steps:
            agent_runs.append(
                AgentRunResponse(
                    id=run.run_id,
                    pipelineId=pipeline.id,
                    step=step,
                    status=run_status,
                    logs=run.logs,
                    tokensUsed=run.tokens_used,
                    costUsd=run.cost_usd,
                    error=run.error,
                    startedAt=run.started_at.isoformat() if run.started_at else None,
                    completedAt=run.completed_at.isoformat() if run.completed_at else None,
                )
            )
        total_cost += run.cost_usd or 0
        total_tokens += run.tokens_used or 0

    return PipelineDetailResponse(
        pipeline=pipeline,
        runs=agent_runs,
        totalCost=total_cost,
        totalTokens=total_tokens,
    )


@router.post("/issues/{issue_id}/pipeline/retry")
async def retry_pipeline(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if issue.status.value not in ("blocked", "in_review"):
        raise HTTPException(status_code=400, detail="Issue is not in a retryable state")

    # Transition to plan_approved to restart build
    from cogniq.domain.enums import IssueStatus

    try:
        issue = await repo.update_status(issue_id, IssueStatus.PLAN_APPROVED)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"message": "Pipeline retry initiated", "issue_id": issue_id, "new_status": issue.status.value}


@router.post("/issues/{issue_id}/pipeline/start")
async def start_pipeline(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    """Manually trigger the appropriate agent based on current issue status."""
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    try:
        from cogniq.dependencies import get_engine
        engine = get_engine()
        status = issue.status if isinstance(issue.status, str) else issue.status.value

        match status:
            case "backlog":
                # Start Plan
                await engine.handle_event("issue_created", issue_id, {"project_id": issue.project_id})
                return {"message": "Plan started", "issue_id": issue_id, "stage": "plan"}
            case "in_analysis":
                # Plan already running — re-enqueue
                from cogniq.dependencies import get_task_queue
                task_queue = get_task_queue()
                await task_queue.enqueue(issue_id, issue.project_id, "plan")
                return {"message": "Plan re-enqueued", "issue_id": issue_id, "stage": "plan"}
            case "plan_complete":
                # Approve Gate 1 and start Build
                await engine.handle_event("gate1_approved", issue_id, {"approved_by": user.id})
                return {"message": "Gate 1 approved, Build started", "issue_id": issue_id, "stage": "build"}
            case "plan_approved" | "in_progress":
                # Start/restart Build
                from cogniq.dependencies import get_task_queue
                task_queue = get_task_queue()
                await task_queue.enqueue(issue_id, issue.project_id, "build")
                return {"message": "Build enqueued", "issue_id": issue_id, "stage": "build"}
            case "blocked":
                # Retry Build
                from cogniq.domain.enums import IssueStatus
                await repo.update_status(issue_id, IssueStatus.PLAN_APPROVED)
                await engine.handle_event("gate1_approved", issue_id, {"approved_by": user.id})
                return {"message": "Build retry started", "issue_id": issue_id, "stage": "build"}
            case _:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot start pipeline from status '{status}'. Use retry or create a new issue.",
                )
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from fastapi import APIRouter, Depends, Query

from cogniq_server.api.schemas import ActiveIssueResponse
from cogniq_server.auth.dependencies import get_current_user
from cogniq_server.auth.models import User
from cogniq_server.dependencies import get_db, get_issue_repository
from cogniq_shared.registry.repository import IssueRepository

router = APIRouter()


@router.get("/active", response_model=list[ActiveIssueResponse])
async def get_active_issues(
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issues = await repo.find_active_issues()
    result = []
    for issue in issues:
        last_event = issue.events[-1] if issue.events else None
        result.append(
            ActiveIssueResponse(
                id=issue.id,
                title=issue.title,
                status=issue.status if isinstance(issue.status, str) else issue.status.value,
                currentStage=issue.summary.current_stage,
                currentPhase=issue.summary.current_phase,
                totalCostUsd=issue.summary.total_cost_usd,
                lastEventType=last_event.type if last_event else None,
                lastEventAt=last_event.occurred_at.isoformat() if last_event else None,
            )
        )
    return result


@router.get("/metrics")
async def get_metrics(
    period: str = Query("4w", description="Period: 1w, 2w, 4w, 8w"),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    # Fetch from metrics collection
    cursor = db["metrics"].find().sort("period_start", -1).limit(8)
    metrics = [doc async for doc in cursor]

    if not metrics:
        return {
            "period": period,
            "metrics": [],
            "summary": {
                "totalIssues": 0,
                "successRate": 0,
                "avgCostUsd": 0,
                "avgDurationSeconds": 0,
            },
        }

    # Serialize
    for m in metrics:
        m["_id"] = str(m["_id"])

    return {
        "period": period,
        "metrics": metrics,
        "summary": {
            "totalIssues": sum(m.get("issues_processed", 0) for m in metrics),
            "successRate": metrics[0].get("metrics", {}).get("success_rate", 0) if metrics else 0,
            "avgCostUsd": metrics[0].get("metrics", {}).get("avg_cost_usd", 0) if metrics else 0,
            "avgDurationSeconds": metrics[0].get("metrics", {}).get("avg_duration_seconds", 0) if metrics else 0,
        },
    }

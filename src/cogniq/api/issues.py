from fastapi import APIRouter, Depends, HTTPException, Query

from cogniq.api.schemas import (
    CreateIssueRequest,
    IssueResponse,
    UpdateIssueRequest,
    UpdateStatusRequest,
)
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_db, get_issue_repository, verify_team_member
from cogniq.domain.enums import IssueStatus, IssuePriority
from cogniq.domain.issue import Issue
from cogniq.registry.repository import IssueRepository

router = APIRouter()


@router.get("/teams/{team_id}/projects/{project_id}/issues", response_model=list[IssueResponse])
async def list_issues(
    team_id: str,
    project_id: str,
    status: str | None = Query(None, description="Backend status: backlog, in_analysis, plan_complete, ..."),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
    repo: IssueRepository = Depends(get_issue_repository),
):
    await verify_team_member(team_id, user.id, db)
    issues = await repo.list_by_project(project_id, status, skip, limit)
    return [IssueResponse.from_issue(i) for i in issues]


@router.post("/teams/{team_id}/projects/{project_id}/issues", response_model=IssueResponse, status_code=201)
async def create_issue(
    team_id: str,
    project_id: str,
    req: CreateIssueRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
    repo: IssueRepository = Depends(get_issue_repository),
):
    await verify_team_member(team_id, user.id, db)
    count = await repo.count_by_project(project_id)
    issue = Issue(
        project_id=project_id,
        number=count + 1,
        title=req.title,
        description=req.description,
        creator_id=user.id,
        status=IssueStatus.BACKLOG,
        priority=IssuePriority.MEDIUM,
    )
    await repo.create(issue)

    # Trigger orchestrator: issue_created → Plan agent
    try:
        from cogniq.dependencies import get_engine
        engine = get_engine()
        await engine.handle_event("issue_created", issue.id, {
            "project_id": project_id,
            "title": req.title,
        })
    except RuntimeError:
        pass  # Orchestrator not initialized (e.g. in tests)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Orchestrator dispatch failed: %s", e)

    return IssueResponse.from_issue(issue)


@router.get("/issues/{issue_id}", response_model=IssueResponse)
async def get_issue(
    issue_id: str,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return IssueResponse.from_issue(issue)


@router.put("/issues/{issue_id}", response_model=IssueResponse)
async def update_issue(
    issue_id: str,
    req: UpdateIssueRequest,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    updates = {}
    if req.title is not None:
        updates["title"] = req.title
    if req.description is not None:
        updates["description"] = req.description
    if req.priority is not None:
        updates["priority"] = req.priority.lower()
    if req.assigneeId is not None:
        updates["assignee_id"] = req.assigneeId
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    issue = await repo.update(issue_id, updates)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return IssueResponse.from_issue(issue)


@router.put("/issues/{issue_id}/status", response_model=IssueResponse)
async def update_issue_status(
    issue_id: str,
    req: UpdateStatusRequest,
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    # Backend status passed directly — no mapping
    try:
        new_status = IssueStatus(req.status)
    except ValueError:
        valid = ", ".join(s.value for s in IssueStatus)
        raise HTTPException(status_code=400, detail=f"Invalid status: {req.status}. Valid: {valid}")

    try:
        issue = await repo.update_status(issue_id, new_status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Trigger orchestrator for gate approvals
    try:
        from cogniq.dependencies import get_engine
        engine = get_engine()
        if new_status == IssueStatus.PLAN_APPROVED:
            await engine.handle_event("gate1_approved", issue_id, {"approved_by": user.id})
        elif new_status == IssueStatus.DONE:
            await engine.handle_event("gate2_merged", issue_id, {"merged_by": user.id})
    except RuntimeError:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Orchestrator event failed: %s", e)

    return IssueResponse.from_issue(issue)

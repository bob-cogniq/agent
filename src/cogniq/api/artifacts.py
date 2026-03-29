from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from cogniq.api.schemas import ArtifactResponse
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_issue_repository
from cogniq.domain.issue import Artifact
from cogniq.registry.repository import IssueRepository

router = APIRouter()


def _artifact_to_response(a: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=a.artifact_id,
        runId=a.run_id,
        type=a.type,
        version=a.version,
        content=a.content,
        contentMd=a.content_md,
        createdAt=a.created_at.isoformat(),
    )


@router.get("/issues/{issue_id}/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(
    issue_id: str,
    type: str | None = Query(None),
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    artifacts = await repo.list_artifacts(issue_id, type)
    return [_artifact_to_response(a) for a in artifacts]


@router.get("/issues/{issue_id}/artifacts/latest", response_model=ArtifactResponse)
async def get_latest_artifact(
    issue_id: str,
    type: str = Query(..., description="Artifact type"),
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    artifact = await repo.get_latest_artifact(issue_id, type)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_to_response(artifact)


@router.post("/issues/{issue_id}/artifacts", response_model=ArtifactResponse, status_code=201)
async def add_artifact(
    issue_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
    repo: IssueRepository = Depends(get_issue_repository),
):
    issue = await repo.get(issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    artifact_type = body.get("type", "")
    run_id = body.get("run_id")
    content = body.get("content")
    content_md = body.get("content_md")

    # Determine version
    existing = await repo.list_artifacts(issue_id, artifact_type)
    version = max((a.version for a in existing), default=0) + 1

    artifact = Artifact(
        run_id=run_id,
        type=artifact_type,
        version=version,
        content=content,
        content_md=content_md,
    )
    await repo.add_artifact(issue_id, artifact)
    return _artifact_to_response(artifact)

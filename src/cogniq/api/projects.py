import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from cogniq.api.schemas import (
    AddRepoRequest,
    CreateProjectRequest,
    ProjectResponse,
    UpdateProjectRequest,
    UpdateRepoRequest,
    _repo_config_to_response,
)
from cogniq.auth.dependencies import get_current_user
from cogniq.auth.models import User
from cogniq.dependencies import get_db, verify_team_member
from cogniq.domain.issue import Project, RepoConfig

router = APIRouter()


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:64] or "project"


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$")


@router.get("/teams/{team_id}/projects", response_model=list[ProjectResponse])
async def list_projects(
    team_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    cursor = db["projects"].find({"team_id": team_id}).sort("created_at", -1)
    return [ProjectResponse.from_db(doc) async for doc in cursor]


@router.post("/teams/{team_id}/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    team_id: str,
    req: CreateProjectRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)

    slug = req.slug or _slugify(req.name)
    if req.slug and not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid slug format")

    # team 내 unique 보장
    from cogniq.api.teams import _ensure_unique_slug
    slug = await _ensure_unique_slug(db, slug, "projects", scope_filter={"team_id": team_id})

    project = Project(
        team_id=team_id,
        name=req.name,
        slug=slug,
        description=req.description,
    )
    await db["projects"].insert_one(project.model_dump(by_alias=True))
    doc = await db["projects"].find_one({"_id": project.id})
    return ProjectResponse.from_db(doc)


@router.get("/teams/{team_id}/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    team_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    doc = await db["projects"].find_one({"_id": project_id, "team_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.from_db(doc)


@router.patch("/teams/{team_id}/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    team_id: str,
    project_id: str,
    req: UpdateProjectRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    updates = {k: v for k, v in req.model_dump(exclude_none=True).items()}

    # slug 변경 시 유효성 + 중복 체크
    if "slug" in updates:
        if not _SLUG_RE.match(updates["slug"]):
            raise HTTPException(status_code=400, detail="Invalid slug format")
        existing = await db["projects"].find_one({
            "team_id": team_id, "slug": updates["slug"], "_id": {"$ne": project_id},
        })
        if existing:
            raise HTTPException(status_code=409, detail="Project slug already taken in this team")

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc)

    result = await db["projects"].find_one_and_update(
        {"_id": project_id, "team_id": team_id},
        {"$set": updates},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.from_db(result)


# ── Project Repos (1:N) ──


@router.get("/teams/{team_id}/projects/{project_id}/repos")
async def list_repos(
    team_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    doc = await db["projects"].find_one({"_id": project_id, "team_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")
    return [_repo_config_to_response(r) for r in doc.get("repos", [])]


@router.post("/teams/{team_id}/projects/{project_id}/repos", status_code=201)
async def add_repo(
    team_id: str,
    project_id: str,
    req: AddRepoRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    doc = await db["projects"].find_one({"_id": project_id, "team_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")

    repo = RepoConfig(
        repo_url=req.repoUrl,
        repo_path=req.repoPath,
        base_branch=req.baseBranch,
        github_token=req.githubToken,
        lint_command=req.lintCommand,
        typecheck_command=req.typecheckCommand,
        test_command=req.testCommand,
        secret_scan_enabled=req.secretScanEnabled,
        is_primary=req.isPrimary,
    )

    # is_primary면 기존 primary 해제
    if req.isPrimary:
        await db["projects"].update_one(
            {"_id": project_id},
            {"$set": {"repos.$[].is_primary": False}},
        )

    await db["projects"].update_one(
        {"_id": project_id},
        {
            "$push": {"repos": repo.model_dump()},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    return _repo_config_to_response(repo.model_dump())


@router.patch("/teams/{team_id}/projects/{project_id}/repos/{repo_id}")
async def update_repo(
    team_id: str,
    project_id: str,
    repo_id: str,
    req: UpdateRepoRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    doc = await db["projects"].find_one({"_id": project_id, "team_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Project not found")

    repos = doc.get("repos", [])
    idx = next((i for i, r in enumerate(repos) if r.get("repo_id") == repo_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    field_map = {
        "repoUrl": "repo_url", "repoPath": "repo_path", "baseBranch": "base_branch",
        "githubToken": "github_token", "lintCommand": "lint_command",
        "typecheckCommand": "typecheck_command", "testCommand": "test_command",
        "secretScanEnabled": "secret_scan_enabled", "isPrimary": "is_primary",
    }
    updates = {}
    for api_key, db_key in field_map.items():
        val = getattr(req, api_key, None)
        if val is not None:
            updates[f"repos.{idx}.{db_key}"] = val

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if req.isPrimary:
        await db["projects"].update_one(
            {"_id": project_id},
            {"$set": {"repos.$[].is_primary": False}},
        )

    updates["updated_at"] = datetime.now(timezone.utc)
    result = await db["projects"].find_one_and_update(
        {"_id": project_id},
        {"$set": updates},
        return_document=True,
    )
    return _repo_config_to_response(result["repos"][idx])


@router.delete("/teams/{team_id}/projects/{project_id}/repos/{repo_id}", status_code=204)
async def delete_repo(
    team_id: str,
    project_id: str,
    repo_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await verify_team_member(team_id, user.id, db)
    result = await db["projects"].update_one(
        {"_id": project_id, "team_id": team_id},
        {
            "$pull": {"repos": {"repo_id": repo_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Project or repo not found")

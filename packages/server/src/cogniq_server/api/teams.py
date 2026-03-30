import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from cogniq_server.api.schemas import (
    AddTeamMemberRequest,
    CreateTeamRequest,
    GitHubRepoItem,
    GitHubTokenStatusResponse,
    SetGitHubTokenRequest,
    TeamResponse,
    UpdateTeamMemberRequest,
    UpdateTeamRequest,
)
from cogniq_server.auth.dependencies import get_current_user
from cogniq_server.auth.models import User
from cogniq_server.dependencies import get_db
from cogniq_shared.domain.enums import MemberRole
from cogniq_shared.domain.issue import Team, TeamMember
from cogniq_shared.integrations.github import GitHubClient

logger = logging.getLogger(__name__)

router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,38}[a-z0-9]$")


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)  # ASCII only
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug[:40] or "team"


async def _ensure_unique_slug(db, slug: str, collection: str = "teams", scope_filter: dict | None = None) -> str:
    """slug가 중복이면 뒤에 숫자를 붙여서 unique하게 만든다."""
    base = slug
    query = {"slug": slug}
    if scope_filter:
        query.update(scope_filter)

    if not await db[collection].find_one(query):
        return slug

    for i in range(1, 100):
        candidate = f"{base}-{i}"
        query_check = {"slug": candidate}
        if scope_filter:
            query_check.update(scope_filter)
        if not await db[collection].find_one(query_check):
            return candidate

    # Fallback: append random suffix
    from uuid import uuid4
    return f"{base}-{uuid4().hex[:6]}"


async def _build_user_map(db, team_doc: dict) -> dict:
    """팀 멤버의 user_id로 users 컬렉션에서 이름/이메일을 조회."""
    user_ids = [m.get("user_id") for m in team_doc.get("members", []) if m.get("user_id")]
    if not user_ids:
        return {}
    cursor = db["users"].find({"_id": {"$in": user_ids}}, {"_id": 1, "name": 1, "email": 1, "image": 1})
    return {doc["_id"]: doc async for doc in cursor}


@router.get("/teams", response_model=list[TeamResponse])
async def list_teams(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """현재 사용자가 소속된 팀 목록."""
    cursor = db["teams"].find({"members.user_id": user.id}).sort("created_at", -1)
    results = []
    async for doc in cursor:
        user_map = await _build_user_map(db, doc)
        results.append(TeamResponse.from_db(doc, user_map))
    return results


@router.post("/teams", response_model=TeamResponse, status_code=201)
async def create_team(
    req: CreateTeamRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    slug = req.slug or _slugify(req.name)

    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must be 3-40 chars, lowercase alphanumeric and hyphens, start/end with alphanumeric",
        )

    slug = await _ensure_unique_slug(db, slug, "teams")

    team = Team(
        slug=slug,
        name=req.name,
        members=[TeamMember(user_id=user.id, role=MemberRole.OWNER)],
    )
    await db["teams"].insert_one(team.model_dump(by_alias=True))
    doc = await db["teams"].find_one({"_id": team.id})
    user_map = await _build_user_map(db, doc)
    return TeamResponse.from_db(doc, user_map)


@router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id, "members.user_id": user.id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")
    user_map = await _build_user_map(db, doc)
    return TeamResponse.from_db(doc, user_map)


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    req: UpdateTeamRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can update team")

    updates: dict = {}
    if req.name is not None:
        updates["name"] = req.name
    if req.slug is not None:
        if not _SLUG_RE.match(req.slug):
            raise HTTPException(status_code=400, detail="Invalid slug format")
        existing = await db["teams"].find_one({"slug": req.slug, "_id": {"$ne": team_id}})
        if existing:
            raise HTTPException(status_code=409, detail="Team slug already taken")
        updates["slug"] = req.slug

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc)

    result = await db["teams"].find_one_and_update(
        {"_id": team_id}, {"$set": updates}, return_document=True,
    )
    user_map = await _build_user_map(db, result)
    return TeamResponse.from_db(result, user_map)


# ── GitHub Token ──


@router.get("/teams/{team_id}/github-token/status", response_model=GitHubTokenStatusResponse)
async def get_github_token_status(
    team_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id, "members.user_id": user.id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    token = doc.get("github_token", "")
    if not token:
        return GitHubTokenStatusResponse(connected=False)

    hint = token[:4] + "***" + token[-3:] if len(token) >= 8 else "***"
    # Try to get username from cached field, or return hint only
    return GitHubTokenStatusResponse(connected=True, tokenHint=hint, username=doc.get("github_username", ""))


@router.put("/teams/{team_id}/github-token", response_model=GitHubTokenStatusResponse)
async def set_github_token(
    team_id: str,
    req: SetGitHubTokenRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can set GitHub token")

    # Validate token by calling GitHub API
    try:
        client = GitHubClient(token=req.githubToken)
        gh_user = await client.get_authenticated_user()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid GitHub token")

    username = gh_user.get("login", "")
    hint = req.githubToken[:4] + "***" + req.githubToken[-3:] if len(req.githubToken) >= 8 else "***"

    await db["teams"].update_one(
        {"_id": team_id},
        {"$set": {
            "github_token": req.githubToken,
            "github_username": username,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return GitHubTokenStatusResponse(connected=True, tokenHint=hint, username=username)


@router.delete("/teams/{team_id}/github-token", status_code=204)
async def remove_github_token(
    team_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can remove GitHub token")

    await db["teams"].update_one(
        {"_id": team_id},
        {"$set": {"github_token": "", "github_username": "", "updated_at": datetime.now(timezone.utc)}},
    )


@router.get("/teams/{team_id}/github-repos", response_model=list[GitHubRepoItem])
async def list_github_repos(
    team_id: str,
    q: str | None = Query(None, description="Search query"),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id, "members.user_id": user.id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    token = doc.get("github_token", "")
    if not token:
        raise HTTPException(status_code=400, detail="No GitHub token configured for this team")

    try:
        client = GitHubClient(token=token)
        if q:
            repos = await client.search_repos(q, page=page, per_page=per_page)
        else:
            repos = await client.list_repos(page=page, per_page=per_page)
    except Exception as e:
        logger.error("GitHub API error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch repos from GitHub")

    return [
        GitHubRepoItem(
            fullName=r.get("full_name", ""),
            htmlUrl=r.get("html_url", ""),
            defaultBranch=r.get("default_branch", "main"),
            private=r.get("private", False),
            description=r.get("description"),
        )
        for r in repos
    ]


# ── Members ──


@router.post("/teams/{team_id}/members", status_code=201)
async def add_member(
    team_id: str,
    req: AddTeamMemberRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can add members")

    if team.has_member(req.userId):
        raise HTTPException(status_code=409, detail="User is already a member")

    member = TeamMember(user_id=req.userId, role=MemberRole(req.role))
    await db["teams"].update_one(
        {"_id": team_id},
        {
            "$push": {"members": member.model_dump()},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    return {"message": "Member added", "userId": req.userId, "role": req.role}


@router.patch("/teams/{team_id}/members/{user_id}")
async def update_member(
    team_id: str,
    user_id: str,
    req: UpdateTeamMemberRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can update members")

    members = doc.get("members", [])
    idx = next((i for i, m in enumerate(members) if m.get("user_id") == user_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Member not found")

    await db["teams"].update_one(
        {"_id": team_id},
        {
            "$set": {
                f"members.{idx}.role": req.role,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )
    return {"message": "Member updated", "userId": user_id, "role": req.role}


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_member(
    team_id: str,
    user_id: str,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    doc = await db["teams"].find_one({"_id": team_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found")

    team = Team(**doc)
    if not team.is_admin_or_owner(user.id):
        raise HTTPException(status_code=403, detail="Only owner/admin can remove members")

    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself")

    await db["teams"].update_one(
        {"_id": team_id},
        {
            "$pull": {"members": {"user_id": user_id}},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

"""API request/response schemas. Backend status/priority passed directly to frontend."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Auth ──

class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    inviteCode: str = Field(..., alias="inviteCode")

    model_config = {"populate_by_name": True}


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: str
    name: str | None = None
    email: str
    image: str | None = None
    locale: str = "ko"


class AuthResponse(BaseModel):
    user: UserResponse
    access_token: str
    refresh_token: str = ""


class TokenResponse(BaseModel):
    access_token: str


# ── Teams ──

class CreateTeamRequest(BaseModel):
    name: str
    slug: str | None = None  # 미입력 시 name에서 자동생성


class UpdateTeamRequest(BaseModel):
    name: str | None = None
    slug: str | None = None


class SetGitHubTokenRequest(BaseModel):
    githubToken: str

    model_config = {"populate_by_name": True}


class GitHubTokenStatusResponse(BaseModel):
    connected: bool
    tokenHint: str = ""
    username: str = ""


class GitHubRepoItem(BaseModel):
    fullName: str
    htmlUrl: str
    defaultBranch: str
    private: bool
    description: str | None = None


class AddTeamMemberRequest(BaseModel):
    userId: str
    role: str = "member"  # owner, admin, member, viewer

    model_config = {"populate_by_name": True}


class UpdateTeamMemberRequest(BaseModel):
    role: str


class TeamMemberResponse(BaseModel):
    userId: str
    role: str
    joinedAt: str
    name: str | None = None
    email: str | None = None
    image: str | None = None


class TeamResponse(BaseModel):
    id: str
    slug: str
    name: str
    hasGithubToken: bool = False
    members: list[TeamMemberResponse] = []
    createdAt: str = ""

    @classmethod
    def from_db(cls, doc: dict[str, Any], user_map: dict[str, dict[str, Any]] | None = None) -> "TeamResponse":
        user_map = user_map or {}
        members = []
        for m in doc.get("members", []):
            uid = m.get("user_id", "")
            u = user_map.get(uid, {})
            members.append(
                TeamMemberResponse(
                    userId=uid,
                    role=m.get("role", "member"),
                    joinedAt=m.get("joined_at", "").isoformat() if hasattr(m.get("joined_at", ""), "isoformat") else str(m.get("joined_at", "")),
                    name=u.get("name"),
                    email=u.get("email"),
                    image=u.get("image"),
                )
            )
        return cls(
            id=str(doc.get("_id", "")),
            slug=doc.get("slug", ""),
            name=doc.get("name", ""),
            hasGithubToken=bool(doc.get("github_token")),
            members=members,
            createdAt=doc.get("created_at", datetime.min).isoformat() if doc.get("created_at") else "",
        )


# ── Projects ──

class CreateProjectRequest(BaseModel):
    name: str
    slug: str | None = None  # 미입력 시 name에서 자동생성
    description: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    description: str | None = None


class ProjectResponse(BaseModel):
    id: str
    teamId: str = ""
    name: str
    slug: str
    description: str | None = None
    repos: list[dict[str, Any]] = []
    createdAt: str = ""

    @classmethod
    def from_db(cls, doc: dict[str, Any]) -> "ProjectResponse":
        repos_raw = doc.get("repos", [])
        repos = [_repo_config_to_response(r) for r in repos_raw]
        return cls(
            id=str(doc.get("_id", "")),
            teamId=doc.get("team_id", ""),
            name=doc.get("name", ""),
            slug=doc.get("slug", ""),
            description=doc.get("description"),
            repos=repos,
            createdAt=doc.get("created_at", datetime.min).isoformat() if doc.get("created_at") else "",
        )


def _mask_token(token: str) -> str:
    if not token or len(token) < 8:
        return "***" if token else ""
    return token[:4] + "***" + token[-3:]


def _repo_config_to_response(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "repoId": r.get("repo_id", ""),
        "repoUrl": r.get("repo_url", ""),
        "repoPath": r.get("repo_path", ""),
        "baseBranch": r.get("base_branch", "main"),
        "githubToken": _mask_token(r.get("github_token", "")),
        "lintCommand": r.get("lint_command", "ruff check ."),
        "typecheckCommand": r.get("typecheck_command", ""),
        "testCommand": r.get("test_command", "uv run pytest"),
        "secretScanEnabled": r.get("secret_scan_enabled", True),
        "isPrimary": r.get("is_primary", False),
        "addedAt": r.get("added_at", ""),
    }


# ── Project Repos ──

class AddRepoRequest(BaseModel):
    repoUrl: str
    repoPath: str = ""
    baseBranch: str = "main"
    githubToken: str = ""
    lintCommand: str = "ruff check ."
    typecheckCommand: str = "ruff check --select ANN ."
    testCommand: str = "uv run pytest"
    secretScanEnabled: bool = True
    isPrimary: bool = False

    model_config = {"populate_by_name": True}


class UpdateRepoRequest(BaseModel):
    repoUrl: str | None = None
    repoPath: str | None = None
    baseBranch: str | None = None
    githubToken: str | None = None
    lintCommand: str | None = None
    typecheckCommand: str | None = None
    testCommand: str | None = None
    secretScanEnabled: bool | None = None
    isPrimary: bool | None = None

    model_config = {"populate_by_name": True}


# ── Issues ──

class CreateIssueRequest(BaseModel):
    title: str
    description: str = ""


class UpdateIssueRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    assigneeId: str | None = Field(None, alias="assigneeId")

    model_config = {"populate_by_name": True}


class UpdateStatusRequest(BaseModel):
    status: str


class PipelineResponse(BaseModel):
    id: str
    issueId: str
    status: str
    currentStep: str
    startedAt: str | None = None
    completedAt: str | None = None


class IssueResponse(BaseModel):
    id: str
    number: int
    title: str
    description: str
    status: str  # Backend status directly: backlog, in_analysis, plan_complete, ...
    priority: str  # Backend priority directly: urgent, high, medium, low
    projectId: str
    creatorId: str
    assigneeId: str | None = None
    assignee: UserResponse | None = None
    pipeline: PipelineResponse | None = None
    createdAt: str
    updatedAt: str

    @classmethod
    def from_issue(cls, issue: Any) -> "IssueResponse":
        # Pass backend status/priority as-is (no mapping)
        status = issue.status if isinstance(issue.status, str) else issue.status.value
        priority = issue.priority if isinstance(issue.priority, str) else issue.priority.value

        pipeline = None
        if issue.runs:
            last_run = issue.runs[-1]
            pipeline = PipelineResponse(
                id=last_run.run_id,
                issueId=issue.id,
                status=last_run.status if isinstance(last_run.status, str) else last_run.status.value,
                currentStep=last_run.stage if isinstance(last_run.stage, str) else last_run.stage.value,
                startedAt=last_run.started_at.isoformat() if last_run.started_at else None,
                completedAt=last_run.completed_at.isoformat() if last_run.completed_at else None,
            )

        return cls(
            id=issue.id,
            number=issue.number,
            title=issue.title,
            description=issue.description,
            status=status,
            priority=priority,
            projectId=issue.project_id,
            creatorId=issue.creator_id,
            assigneeId=issue.assignee_id,
            pipeline=pipeline,
            createdAt=issue.created_at.isoformat(),
            updatedAt=issue.updated_at.isoformat(),
        )


# ── Documents ──

class CreateDocumentRequest(BaseModel):
    type: str = "custom"
    title: str
    content: str = ""
    issueId: str | None = Field(None, alias="issueId")

    model_config = {"populate_by_name": True}


class UpdateDocumentRequest(BaseModel):
    title: str | None = None
    content: str | None = None


class DocumentResponse(BaseModel):
    id: str
    type: str
    title: str
    content: str
    version: int
    issueId: str | None = None
    projectId: str
    createdAt: str
    updatedAt: str

    @classmethod
    def from_db(cls, doc: dict[str, Any]) -> "DocumentResponse":
        return cls(
            id=str(doc.get("_id", "")),
            type=doc.get("type", "custom"),
            title=doc.get("title", ""),
            content=doc.get("content", ""),
            version=doc.get("version", 1),
            issueId=doc.get("issue_id"),
            projectId=doc.get("project_id", ""),
            createdAt=doc.get("created_at", datetime.min).isoformat() if doc.get("created_at") else "",
            updatedAt=doc.get("updated_at", datetime.min).isoformat() if doc.get("updated_at") else "",
        )


# ── Runs / Artifacts / Events ──

class RunResponse(BaseModel):
    id: str
    stage: str
    phase: str | None = None
    status: str
    trigger: str = "auto"
    attemptNumber: int = 1
    tokensUsed: int | None = None
    costUsd: float | None = None
    error: str | None = None
    logs: str | None = None
    startedAt: str | None = None
    completedAt: str | None = None


class ArtifactResponse(BaseModel):
    id: str
    runId: str | None = None
    type: str
    version: int = 1
    content: dict[str, Any] | None = None
    contentMd: str | None = None
    createdAt: str


class EventResponse(BaseModel):
    id: str
    runId: str | None = None
    type: str
    payload: dict[str, Any] = {}
    occurredAt: str


# ── Pipeline detail (for pipeline viewer) ──

class AgentRunResponse(BaseModel):
    id: str
    pipelineId: str
    step: str  # PO_AGENT | DESIGNER_AGENT | DEV_DESIGN_AGENT | CODE_IMPLEMENTATION
    status: str  # PENDING | RUNNING | COMPLETED | FAILED | SKIPPED
    output: dict[str, Any] | None = None
    logs: str | None = None
    tokensUsed: int | None = None
    costUsd: float | None = None
    error: str | None = None
    startedAt: str | None = None
    completedAt: str | None = None


class PipelineDetailResponse(BaseModel):
    pipeline: PipelineResponse
    runs: list[AgentRunResponse] = []
    totalCost: float = 0.0
    totalTokens: int = 0


# ── Dashboard ──

class ActiveIssueResponse(BaseModel):
    id: str
    title: str
    status: str
    currentStage: str | None = None
    currentPhase: str | None = None
    totalCostUsd: float = 0.0
    lastEventType: str | None = None
    lastEventAt: str | None = None

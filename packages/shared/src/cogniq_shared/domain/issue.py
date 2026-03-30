from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from cogniq_shared.domain.enums import ArtifactType, EventType, IssueStatus, IssuePriority, MemberRole, RunStatus, Stage
from cogniq_shared.domain.code_session import CodeSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex[:12]


class Run(BaseModel):
    run_id: str = Field(default_factory=_new_id)
    stage: Stage
    phase: str | None = None
    status: RunStatus = RunStatus.PENDING
    trigger: str = "auto"
    attempt_number: int = 1
    config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: int | None = None
    cost_usd: float | None = None
    tokens_used: int | None = None
    error: str | None = None
    logs: str | None = None


class Artifact(BaseModel):
    artifact_id: str = Field(default_factory=_new_id)
    run_id: str | None = None
    type: ArtifactType
    version: int = 1
    content: dict[str, Any] | None = None
    content_md: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Event(BaseModel):
    event_id: str = Field(default_factory=_new_id)
    run_id: str | None = None
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=_utcnow)


class IssueSummary(BaseModel):
    total_cost_usd: float = 0.0
    total_duration_seconds: int = 0
    total_tokens: int = 0
    run_count: int = 0
    artifact_count: int = 0
    current_stage: str | None = None
    current_phase: str | None = None


class Issue(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    project_id: str
    team_key: str = ""
    number: int = 0
    title: str
    description: str = ""
    status: IssueStatus = IssueStatus.BACKLOG
    priority: IssuePriority = IssuePriority.MEDIUM
    labels: list[str] = Field(default_factory=list)
    creator_id: str = ""
    assignee_id: str | None = None
    runs: list[Run] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    code_sessions: list[CodeSession] = Field(default_factory=list)
    summary: IssueSummary = Field(default_factory=IssueSummary)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}


class TeamMember(BaseModel):
    user_id: str
    role: MemberRole = MemberRole.MEMBER
    joined_at: datetime = Field(default_factory=_utcnow)


class Team(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    slug: str                              # 유니크 식별자 (URL용)
    name: str
    github_token: str = ""                 # 팀 레벨 GitHub PAT
    members: list[TeamMember] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}

    def has_member(self, user_id: str) -> bool:
        return any(m.user_id == user_id for m in self.members)

    def get_member(self, user_id: str) -> TeamMember | None:
        return next((m for m in self.members if m.user_id == user_id), None)

    def is_admin_or_owner(self, user_id: str) -> bool:
        m = self.get_member(user_id)
        return m is not None and m.role in (MemberRole.OWNER, MemberRole.ADMIN)


class RepoConfig(BaseModel):
    """A GitHub repository linked to a project."""
    repo_id: str = Field(default_factory=_new_id)
    repo_url: str                          # https://github.com/owner/repo
    repo_path: str = ""                    # 서버 로컬 clone 경로
    base_branch: str = "main"
    github_token: str = ""                 # repo별 PAT (비어있으면 서버 기본값)
    github_webhook_secret: str = ""
    setup_command: str = ""                # dependency 설치 명령어 (예: "uv sync", "npm install")
    lint_command: str = "ruff check ."
    typecheck_command: str = "ruff check --select ANN ."
    test_command: str = "uv run pytest"
    secret_scan_enabled: bool = True
    is_primary: bool = False               # 기본 repo 지정
    added_at: datetime = Field(default_factory=_utcnow)


class Project(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    team_id: str
    name: str
    slug: str = ""
    description: str | None = None
    repos: list[RepoConfig] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}

    def get_primary_repo(self) -> RepoConfig | None:
        """primary repo 반환. 없으면 첫 번째 repo."""
        for r in self.repos:
            if r.is_primary:
                return r
        return self.repos[0] if self.repos else None

    def get_repo(self, repo_id: str) -> RepoConfig | None:
        for r in self.repos:
            if r.repo_id == repo_id:
                return r
        return None


class Document(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    type: str = "custom"
    title: str
    content: str = ""
    version: int = 1
    issue_id: str | None = None
    project_id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}

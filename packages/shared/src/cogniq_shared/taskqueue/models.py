from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex[:12]


class TaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskResult(BaseModel):
    status: str = "success"  # "success" | "failed"
    reason: str = ""
    pr_url: str = ""
    error: str = ""
    cost_usd: float = 0.0
    tokens_used: int = 0


class TaskDocument(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    issue_id: str
    project_id: str
    stage: str  # "plan" | "build"
    status: TaskStatus = TaskStatus.PENDING
    config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 10  # lower = higher priority
    worker_id: str | None = None
    result: TaskResult | None = None
    heartbeat_at: datetime | None = None
    server_acked: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    claimed_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"populate_by_name": True}

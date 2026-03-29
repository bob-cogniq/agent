from functools import lru_cache

from fastapi import Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from cogniq.config import Settings, settings
from cogniq.registry.database import get_database
from cogniq.registry.repository import IssueRepository


@lru_cache
def get_settings() -> Settings:
    return settings


def get_db() -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    return get_database()


def get_issue_repository() -> IssueRepository:
    return IssueRepository(get_database())


async def verify_team_member(team_id: str, user_id: str, db: AsyncIOMotorDatabase) -> dict:
    """Verify user is a member of the team. Returns team doc."""
    doc = await db["teams"].find_one({"_id": team_id, "members.user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Team not found or access denied")
    return doc


# ── Orchestrator singletons (initialized in lifespan) ──

_engine: "OrchestrationEngine | None" = None  # type: ignore[name-defined]
_task_queue: "TaskQueueRepository | None" = None  # type: ignore[name-defined]


def set_orchestrator(engine: "OrchestrationEngine", task_queue: "TaskQueueRepository") -> None:  # type: ignore[name-defined]
    global _engine, _task_queue
    _engine = engine
    _task_queue = task_queue


def get_engine():  # noqa: ANN201
    from cogniq.orchestrator.engine import OrchestrationEngine
    if _engine is None:
        raise RuntimeError("OrchestrationEngine not initialized")
    return _engine


def get_task_queue():  # noqa: ANN201
    from cogniq.taskqueue.repository import TaskQueueRepository
    if _task_queue is None:
        raise RuntimeError("TaskQueueRepository not initialized")
    return _task_queue

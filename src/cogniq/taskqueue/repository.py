import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from cogniq.taskqueue.models import TaskDocument, TaskResult, TaskStatus

logger = logging.getLogger(__name__)


class TaskQueueRepository:
    """MongoDB-backed task queue for agent dispatch."""

    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["task_queue"]

    # ── Server-side: enqueue / cancel / ack ──

    async def enqueue(
        self,
        issue_id: str,
        project_id: str,
        stage: str,
        config: dict[str, Any] | None = None,
        priority: int = 10,
    ) -> str:
        """Add a task to the queue. Returns task_id."""
        task = TaskDocument(
            issue_id=issue_id,
            project_id=project_id,
            stage=stage,
            config=config or {},
            priority=priority,
        )
        await self._col.insert_one(task.model_dump(by_alias=True))
        logger.info("Enqueued task %s: issue=%s stage=%s project=%s", task.id, issue_id, stage, project_id)
        return task.id

    async def cancel(self, issue_id: str, stage: str) -> bool:
        """Cancel a pending task. Returns True if cancelled."""
        result = await self._col.update_one(
            {"issue_id": issue_id, "stage": stage, "status": TaskStatus.PENDING},
            {"$set": {"status": TaskStatus.CANCELLED}},
        )
        if result.modified_count > 0:
            logger.info("Cancelled task: issue=%s stage=%s", issue_id, stage)
            return True
        return False

    async def ack(self, task_id: str) -> None:
        """Mark task as acknowledged by server (ResultWatcher processed it)."""
        await self._col.update_one(
            {"_id": task_id},
            {"$set": {"server_acked": True}},
        )

    async def find_unacked(self) -> list[dict[str, Any]]:
        """Find completed/failed tasks not yet acknowledged. For server startup recovery."""
        cursor = self._col.find({
            "status": {"$in": [TaskStatus.COMPLETED, TaskStatus.FAILED]},
            "server_acked": False,
        })
        return [doc async for doc in cursor]

    async def find_stale(self, timeout_seconds: int = 120) -> list[dict[str, Any]]:
        """Find tasks claimed/running but heartbeat expired."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        cursor = self._col.find({
            "status": {"$in": [TaskStatus.CLAIMED, TaskStatus.RUNNING]},
            "heartbeat_at": {"$lt": cutoff},
        })
        return [doc async for doc in cursor]

    # ── Worker-side: claim / start / complete / fail / heartbeat ──

    async def claim(self, worker_id: str) -> TaskDocument | None:
        """Atomically claim the next pending task, respecting per-project concurrency."""
        now = datetime.now(timezone.utc)

        # Find projects with active tasks (one-at-a-time per project)
        busy_projects = await self._col.distinct(
            "project_id",
            {"status": {"$in": [TaskStatus.CLAIMED, TaskStatus.RUNNING]}},
        )

        doc = await self._col.find_one_and_update(
            {
                "status": TaskStatus.PENDING,
                "project_id": {"$nin": busy_projects},
            },
            {
                "$set": {
                    "status": TaskStatus.CLAIMED,
                    "worker_id": worker_id,
                    "claimed_at": now,
                    "heartbeat_at": now,
                },
            },
            sort=[("priority", 1), ("created_at", 1)],
            return_document=True,
        )

        if doc:
            logger.info("Claimed task %s: issue=%s stage=%s", doc["_id"], doc["issue_id"], doc["stage"])
            return TaskDocument(**doc)
        return None

    async def start(self, task_id: str) -> None:
        """Transition task to running."""
        now = datetime.now(timezone.utc)
        await self._col.update_one(
            {"_id": task_id},
            {"$set": {"status": TaskStatus.RUNNING, "started_at": now, "heartbeat_at": now}},
        )

    async def complete(self, task_id: str, result: TaskResult) -> None:
        """Mark task as completed with result."""
        now = datetime.now(timezone.utc)
        await self._col.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "status": TaskStatus.COMPLETED,
                    "result": result.model_dump(),
                    "completed_at": now,
                    "heartbeat_at": now,
                },
            },
        )
        logger.info("Task completed: %s (status=%s)", task_id, result.status)

    async def fail(self, task_id: str, error: str) -> None:
        """Mark task as failed."""
        now = datetime.now(timezone.utc)
        await self._col.update_one(
            {"_id": task_id},
            {
                "$set": {
                    "status": TaskStatus.FAILED,
                    "result": TaskResult(status="failed", error=error).model_dump(),
                    "completed_at": now,
                    "heartbeat_at": now,
                },
            },
        )
        logger.info("Task failed: %s — %s", task_id, error[:200])

    async def heartbeat(self, task_id: str) -> None:
        """Update heartbeat timestamp."""
        await self._col.update_one(
            {"_id": task_id},
            {"$set": {"heartbeat_at": datetime.now(timezone.utc)}},
        )

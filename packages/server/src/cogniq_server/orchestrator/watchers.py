import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from cogniq_shared.config import Settings
from cogniq_shared.domain.enums import IssueStatus
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository

if TYPE_CHECKING:
    from cogniq_server.orchestrator.engine import OrchestrationEngine

logger = logging.getLogger(__name__)


class GateWatcher:
    """Periodically checks for issues stuck at approval gates and sends reminders."""

    def __init__(self, repo: IssueRepository, settings: Settings, notify_fn=None):
        self._repo = repo
        self._settings = settings
        self._notify_fn = notify_fn
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("GateWatcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("GateWatcher stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_pending_gates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("GateWatcher error: %s", e, exc_info=True)

            await asyncio.sleep(3600)  # Check every hour

    async def check_pending_gates(self) -> None:
        now = datetime.now(timezone.utc)

        # Gate 1: plan_complete waiting for approval
        gate1_issues = await self._find_issues_in_status(IssueStatus.PLAN_COMPLETE)
        for issue in gate1_issues:
            hours = (now - issue.updated_at).total_seconds() / 3600
            if hours >= 4:
                logger.info("Gate 1 reminder: %s (%.1f hours)", issue.id, hours)
                if self._notify_fn:
                    await self._notify_fn("gate1_reminder", issue.id, {"hours_waiting": round(hours, 1)})

        # Gate 2: in_review waiting for merge
        gate2_issues = await self._find_issues_in_status(IssueStatus.IN_REVIEW)
        for issue in gate2_issues:
            hours = (now - issue.updated_at).total_seconds() / 3600
            if hours >= 8:
                logger.info("Gate 2 reminder: %s (%.1f hours)", issue.id, hours)
                if self._notify_fn:
                    await self._notify_fn("gate2_reminder", issue.id, {"hours_waiting": round(hours, 1)})

    async def _find_issues_in_status(self, status: IssueStatus):
        col = self._repo._col
        cursor = col.find({"status": status.value})
        from cogniq_shared.domain.issue import Issue
        return [Issue.model_validate(doc) async for doc in cursor]


class ResultWatcher:
    """Watches task_queue for completed/failed tasks via Change Stream,
    then triggers the appropriate engine event."""

    def __init__(
        self,
        engine: "OrchestrationEngine",
        task_queue: TaskQueueRepository,
        db: AsyncIOMotorDatabase,  # type: ignore[type-arg]
    ):
        self._engine = engine
        self._task_queue = task_queue
        self._db = db
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        # Process any unacked results from before restart
        await self._recover_unacked()
        self._task = asyncio.create_task(self._loop())
        logger.info("ResultWatcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("ResultWatcher stopped")

    async def _recover_unacked(self) -> None:
        """On startup, process results that completed while server was down."""
        unacked = await self._task_queue.find_unacked()
        for doc in unacked:
            try:
                await self._process_result(doc)
            except Exception as e:
                logger.error("Failed to recover task %s: %s", doc.get("_id"), e)

    async def _loop(self) -> None:
        while self._running:
            try:
                pipeline = [
                    {"$match": {
                        "operationType": "update",
                        "updateDescription.updatedFields.status": {
                            "$in": ["completed", "failed"],
                        },
                    }},
                ]
                async with self._db["task_queue"].watch(
                    pipeline,
                    full_document="updateLookup",
                ) as stream:
                    async for change in stream:
                        if not self._running:
                            break
                        full_doc = change.get("fullDocument")
                        if full_doc and not full_doc.get("server_acked"):
                            await self._process_result(full_doc)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ResultWatcher stream error: %s", e, exc_info=True)
                await asyncio.sleep(5)  # Reconnect after error

    async def _process_result(self, task_doc: dict[str, Any]) -> None:
        """Map task result to engine event."""
        task_id = task_doc["_id"]
        issue_id = task_doc["issue_id"]
        stage = task_doc["stage"]
        result = task_doc.get("result") or {}
        result_status = result.get("status", "failed")

        if result_status == "success":
            event = "plan_completed" if stage == "plan" else "build_completed"
            payload: dict[str, Any] = {}
            if result.get("pr_url"):
                payload["pr_url"] = result["pr_url"]
            if result.get("cost_usd"):
                payload["cost_usd"] = result["cost_usd"]
        else:
            event = "plan_failed" if stage == "plan" else "build_failed"
            payload = {
                "error": result.get("error", ""),
                "reason": result.get("reason", ""),
            }

        logger.info("ResultWatcher: task=%s → event=%s issue=%s", task_id, event, issue_id)
        await self._engine.handle_event(event, issue_id, payload)
        await self._task_queue.ack(task_id)


class StaleTaskReaper:
    """Periodically marks stale tasks (heartbeat timeout) as failed."""

    def __init__(
        self,
        task_queue: TaskQueueRepository,
        timeout_seconds: int = 120,
        check_interval: float = 30.0,
    ):
        self._task_queue = task_queue
        self._timeout = timeout_seconds
        self._interval = check_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("StaleTaskReaper started (timeout=%ds, interval=%.0fs)", self._timeout, self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("StaleTaskReaper stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                stale = await self._task_queue.find_stale(self._timeout)
                for doc in stale:
                    task_id = doc["_id"]
                    worker_id = doc.get("worker_id", "unknown")
                    logger.warning("Stale task detected: %s (worker=%s)", task_id, worker_id)
                    await self._task_queue.fail(task_id, f"Worker heartbeat timeout (worker={worker_id})")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("StaleTaskReaper error: %s", e, exc_info=True)

            await asyncio.sleep(self._interval)

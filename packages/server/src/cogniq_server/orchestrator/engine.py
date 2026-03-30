import logging
from typing import Any

from cogniq_shared.domain.enums import EventType, IssueStatus
from cogniq_shared.domain.issue import Event
from cogniq_shared.domain.state_machine import StateMachine
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository

logger = logging.getLogger(__name__)


class OrchestrationEngine:
    """Central controller for issue lifecycle.

    Receives events (from webhooks, API, agents) and:
    1. Validates and executes state transitions
    2. Dispatches agent tasks via the scheduler
    3. Triggers notifications via integrations
    """

    def __init__(
        self,
        repo: IssueRepository,
        task_queue: TaskQueueRepository,
        integrations: Any = None,  # Will be typed properly when integrations are built
    ):
        self._repo = repo
        self._task_queue = task_queue
        self._integrations = integrations

    async def handle_event(self, event: str, issue_id: str, payload: dict[str, Any] | None = None) -> None:
        payload = payload or {}
        logger.info("Engine handling: event=%s issue=%s", event, issue_id)

        match event:
            case "issue_created":
                await self._on_issue_created(issue_id, payload)
            case "plan_completed":
                await self._on_plan_completed(issue_id, payload)
            case "plan_failed":
                await self._on_plan_failed(issue_id, payload)
            case "gate1_approved":
                await self._on_gate1_approved(issue_id, payload)
            case "gate1_rejected":
                await self._on_gate1_rejected(issue_id, payload)
            case "build_completed":
                await self._on_build_completed(issue_id, payload)
            case "build_failed":
                await self._on_build_failed(issue_id, payload)
            case "gate2_merged":
                await self._on_gate2_merged(issue_id, payload)
            case _:
                logger.warning("Unknown event: %s", event)

    # ── Event handlers ──

    async def _on_issue_created(self, issue_id: str, payload: dict[str, Any]) -> None:
        issue = await self._repo.get(issue_id)
        if not issue:
            logger.error("Issue not found: %s", issue_id)
            return

        await self._transition(issue_id, IssueStatus.IN_ANALYSIS)
        await self._emit(issue_id, EventType.PLAN_STARTED)
        await self._task_queue.enqueue(issue_id, issue.project_id, "plan")

    async def _on_plan_completed(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.PLAN_COMPLETE)
        await self._emit(issue_id, EventType.PLAN_COMPLETED, payload)

        # Check Fast Track
        if await self._is_fast_track(issue_id):
            logger.info("Fast Track: %s", issue_id)
            await self._transition(issue_id, IssueStatus.PLAN_APPROVED)
            await self._emit(issue_id, EventType.GATE1_APPROVED, {"auto": True})
            await self._transition(issue_id, IssueStatus.IN_PROGRESS)
            await self._emit(issue_id, EventType.BUILD_STARTED)
            issue = await self._repo.get(issue_id)
            if issue:
                await self._task_queue.enqueue(issue_id, issue.project_id, "build")
        else:
            await self._notify("plan_complete", issue_id)

    async def _on_plan_failed(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.BACKLOG)
        await self._emit(issue_id, EventType.PLAN_FAILED, payload)
        await self._notify("plan_failed", issue_id, payload)

    async def _on_gate1_approved(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.PLAN_APPROVED)
        await self._emit(issue_id, EventType.GATE1_APPROVED, payload)

        issue = await self._repo.get(issue_id)
        if issue:
            await self._transition(issue_id, IssueStatus.IN_PROGRESS)
            await self._emit(issue_id, EventType.BUILD_STARTED)
            await self._task_queue.enqueue(issue_id, issue.project_id, "build")

    async def _on_gate1_rejected(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.BACKLOG)
        await self._emit(issue_id, EventType.GATE1_REJECTED, payload)

    async def _on_build_completed(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.IN_REVIEW)
        await self._emit(issue_id, EventType.BUILD_COMPLETED, payload)
        await self._notify("build_complete", issue_id, payload)

    async def _on_build_failed(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.BLOCKED)
        await self._emit(issue_id, EventType.BUILD_FAILED, payload)
        await self._notify("build_failed", issue_id, payload)

    async def _on_gate2_merged(self, issue_id: str, payload: dict[str, Any]) -> None:
        await self._transition(issue_id, IssueStatus.DONE)
        await self._emit(issue_id, EventType.GATE2_MERGED, payload)
        await self._notify("gate2_merged", issue_id, payload)

    # ── Helpers ──

    async def _transition(self, issue_id: str, new_status: IssueStatus) -> None:
        try:
            await self._repo.update_status(issue_id, new_status)
            logger.info("Transition: %s → %s", issue_id, new_status.value)
        except ValueError as e:
            logger.error("Invalid transition for %s: %s", issue_id, e)

    async def _emit(self, issue_id: str, event_type: EventType, payload: dict[str, Any] | None = None) -> None:
        event = Event(type=event_type, payload=payload or {})
        try:
            await self._repo.add_event(issue_id, event)
        except Exception as e:
            logger.warning("Failed to emit event %s for %s: %s", event_type, issue_id, e)

    async def _is_fast_track(self, issue_id: str) -> bool:
        issue = await self._repo.get(issue_id)
        if not issue:
            return False

        labels = [l.lower() for l in issue.labels]

        # Hotfix + Urgent
        if "hotfix" in labels and issue.priority == "urgent":
            return True

        # chore/docs/refactor + short description + no safety + no new deps
        fast_labels = {"chore", "docs", "refactor"}
        if fast_labels & set(labels):
            if len(issue.description) <= 200:
                # Check safety flags from analysis artifact
                analysis = await self._repo.get_latest_artifact(issue_id, "analysis")
                if analysis and analysis.content:
                    safety_flags = analysis.content.get("safety", {}).get("flags", [])
                    if not safety_flags:
                        return True
        return False

    async def _notify(self, event: str, issue_id: str, payload: dict[str, Any] | None = None) -> None:
        if self._integrations:
            try:
                slack = getattr(self._integrations, "slack", None)
                if slack:
                    await slack.notify(event, issue_id, payload)
            except Exception as e:
                logger.warning("Notification failed: %s", e)

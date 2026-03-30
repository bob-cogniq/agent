from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from cogniq_shared.domain.enums import IssueStatus
from cogniq_shared.domain.issue import Artifact, Event, Issue, IssueSummary, Run
from cogniq_shared.domain.state_machine import StateMachine


class IssueRepository:
    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["issues"]

    # ── Issue CRUD ──

    async def create(self, issue: Issue) -> Issue:
        doc = issue.model_dump(by_alias=True)
        await self._col.insert_one(doc)
        return issue

    async def get(self, issue_id: str) -> Issue | None:
        doc = await self._col.find_one({"_id": issue_id})
        if doc is None:
            return None
        return Issue.model_validate(doc)

    async def list_by_project(
        self,
        project_id: str,
        status: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[Issue]:
        query: dict[str, Any] = {"project_id": project_id}
        if status:
            query["status"] = status
        cursor = self._col.find(query).sort("updated_at", -1).skip(skip).limit(limit)
        return [Issue.model_validate(doc) async for doc in cursor]

    async def count_by_project(self, project_id: str) -> int:
        return await self._col.count_documents({"project_id": project_id})

    async def update(self, issue_id: str, updates: dict[str, Any]) -> Issue | None:
        updates["updated_at"] = datetime.now(timezone.utc)
        result = await self._col.find_one_and_update(
            {"_id": issue_id},
            {"$set": updates},
            return_document=True,
        )
        return Issue.model_validate(result) if result else None

    async def update_status(self, issue_id: str, new_status: IssueStatus) -> Issue:
        issue = await self.get(issue_id)
        if issue is None:
            raise ValueError(f"Issue not found: {issue_id}")
        StateMachine.validate_transition(issue.status, new_status)
        result = await self.update(issue_id, {"status": new_status.value})
        if result is None:
            raise ValueError(f"Failed to update issue: {issue_id}")
        return result

    # ── Runs ──

    async def add_run(self, issue_id: str, run: Run) -> str:
        await self._col.update_one(
            {"_id": issue_id},
            {
                "$push": {"runs": run.model_dump()},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return run.run_id

    async def update_run(self, issue_id: str, run_id: str, updates: dict[str, Any]) -> None:
        set_fields = {f"runs.$.{k}": v for k, v in updates.items()}
        set_fields["updated_at"] = datetime.now(timezone.utc)
        await self._col.update_one(
            {"_id": issue_id, "runs.run_id": run_id},
            {"$set": set_fields},
        )

    async def list_runs(self, issue_id: str) -> list[Run]:
        doc = await self._col.find_one({"_id": issue_id}, {"runs": 1})
        if not doc or "runs" not in doc:
            return []
        return [Run.model_validate(r) for r in doc["runs"]]

    # ── Artifacts ──

    async def add_artifact(self, issue_id: str, artifact: Artifact) -> str:
        await self._col.update_one(
            {"_id": issue_id},
            {
                "$push": {"artifacts": artifact.model_dump()},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return artifact.artifact_id

    async def list_artifacts(self, issue_id: str, artifact_type: str | None = None) -> list[Artifact]:
        doc = await self._col.find_one({"_id": issue_id}, {"artifacts": 1})
        if not doc or "artifacts" not in doc:
            return []
        artifacts = [Artifact.model_validate(a) for a in doc["artifacts"]]
        if artifact_type:
            artifacts = [a for a in artifacts if a.type == artifact_type]
        return artifacts

    async def get_latest_artifact(self, issue_id: str, artifact_type: str) -> Artifact | None:
        artifacts = await self.list_artifacts(issue_id, artifact_type)
        if not artifacts:
            return None
        return max(artifacts, key=lambda a: a.version)

    # ── Events ──

    async def add_event(self, issue_id: str, event: Event) -> str:
        await self._col.update_one(
            {"_id": issue_id},
            {
                "$push": {"events": event.model_dump()},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return event.event_id

    async def list_events(self, issue_id: str) -> list[Event]:
        doc = await self._col.find_one({"_id": issue_id}, {"events": 1})
        if not doc or "events" not in doc:
            return []
        return [Event.model_validate(e) for e in doc["events"]]

    # ── Summary ──

    async def update_summary(self, issue_id: str) -> None:
        doc = await self._col.find_one({"_id": issue_id}, {"runs": 1, "artifacts": 1})
        if not doc:
            return
        runs = doc.get("runs", [])
        artifacts = doc.get("artifacts", [])
        total_cost = sum(r.get("cost_usd", 0) or 0 for r in runs)
        total_duration = sum(r.get("duration_seconds", 0) or 0 for r in runs)
        total_tokens = sum(r.get("tokens_used", 0) or 0 for r in runs)
        current_run = runs[-1] if runs else None

        summary = IssueSummary(
            total_cost_usd=total_cost,
            total_duration_seconds=total_duration,
            total_tokens=total_tokens,
            run_count=len(runs),
            artifact_count=len(artifacts),
            current_stage=current_run.get("stage") if current_run else None,
            current_phase=current_run.get("phase") if current_run else None,
        )
        await self._col.update_one(
            {"_id": issue_id},
            {"$set": {"summary": summary.model_dump(), "updated_at": datetime.now(timezone.utc)}},
        )

    # ── Queries ──

    async def find_active_issues(self) -> list[Issue]:
        active_statuses = [
            IssueStatus.IN_ANALYSIS.value,
            IssueStatus.PLAN_COMPLETE.value,
            IssueStatus.PLAN_APPROVED.value,
            IssueStatus.IN_PROGRESS.value,
            IssueStatus.IN_REVIEW.value,
            IssueStatus.BLOCKED.value,
        ]
        cursor = self._col.find({"status": {"$in": active_statuses}}).sort("updated_at", -1)
        return [Issue.model_validate(doc) async for doc in cursor]

    async def find_running_runs(self) -> list[dict[str, Any]]:
        """Find issues with running runs (for crash recovery)."""
        cursor = self._col.find(
            {"runs.status": "running"},
            {"_id": 1, "project_id": 1, "runs.$": 1},
        )
        return [doc async for doc in cursor]

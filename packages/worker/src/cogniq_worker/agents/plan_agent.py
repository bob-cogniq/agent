import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cogniq_worker.agents.base import BaseAgent, CostTracker, RunResult
from cogniq_worker.agents.claude_client import ClaudeClient
from cogniq_shared.config import settings
from cogniq_shared.domain.enums import ArtifactType, EventType, Stage
from cogniq_shared.integrations.linear import LinearClient
from cogniq_shared.registry.repository import IssueRepository

logger = logging.getLogger(__name__)


class PlanAgent(BaseAgent):
    """ProductOwnerAgent: analyzes issues, creates verification contracts, generates plan.md."""

    stage = Stage.PLAN

    def __init__(self, repo: IssueRepository, workdir: Path | None = None):
        super().__init__(
            repo=repo,
            timeout_seconds=settings.plan_timeout_minutes * 60,
            max_cost_usd=settings.build_max_cost_usd,
        )
        self._claude = ClaudeClient(
            cost_tracker=self._cost_tracker,
        )
        self._linear = LinearClient(api_key=settings.linear_api_key) if settings.linear_api_key else None
        self._workdir = workdir or Path(".cogniq")

    async def run(self, issue_id: str, run_id: str) -> RunResult:
        cogniq_dir = self._workdir
        cogniq_dir.mkdir(parents=True, exist_ok=True)

        # 1. Fetch issue data
        issue = await self._repo.get(issue_id)
        if not issue:
            return RunResult(status="failed", reason="issue_not_found")

        issue_data: dict[str, Any] = {
            "id": issue.id,
            "title": issue.title,
            "description": issue.description,
            "priority": issue.priority,
            "labels": issue.labels,
        }

        # Enrich from Linear if available
        if self._linear and settings.linear_api_key:
            try:
                linear_data = await self._linear.get_issue(issue_id)
                if linear_data:
                    issue_data.update({
                        "linear_description": linear_data.get("description", ""),
                        "team_key": linear_data.get("team", {}).get("key", ""),
                        "comments": [c.get("body", "") for c in linear_data.get("comments", {}).get("nodes", [])],
                    })
            except Exception as e:
                logger.warning("Failed to fetch from Linear: %s", e)

        # Save issue snapshot
        issue_toml_data = {
            "meta": {"snapshot_at": datetime.now(timezone.utc).isoformat()},
            "issue": issue_data,
        }
        issue_path = cogniq_dir / "issue.toml"
        self.write_toml(issue_path, issue_toml_data)
        await self._push_artifact(issue_id, run_id, ArtifactType.ISSUE_SNAPSHOT, issue_path)

        # 2. Validate issue (abort conditions)
        if not self._validate_issue(issue_data):
            reason = "Issue description is empty or lacks verifiable requirements"
            logger.warning("Plan abort for %s: %s", issue_id, reason)
            if self._linear:
                try:
                    await self._linear.add_comment(issue_id, f"Plan 중단: {reason}")
                except Exception:
                    pass
            return RunResult(status="failed", reason="invalid_issue")

        # 3. Analyze codebase
        codebase_context = self._gather_codebase_context()
        analysis = await self._claude.analyze_codebase(issue_data, codebase_context)
        analysis_toml_data = {
            "meta": {
                "issue_id": issue_id,
                "created_by": "plan",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            **analysis,
        }
        analysis_path = cogniq_dir / "analysis.toml"
        self.write_toml(analysis_path, analysis_toml_data)
        await self._push_artifact(issue_id, run_id, ArtifactType.ANALYSIS, analysis_path)

        # 4. Define verification criteria
        verification = await self._claude.define_verification(issue_data, analysis)
        verification_toml_data = {
            "meta": {
                "issue_id": issue_id,
                "created_by": "plan",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            **verification,
        }
        verification_path = cogniq_dir / "verification.toml"
        self.write_toml(verification_path, verification_toml_data)
        await self._push_artifact(issue_id, run_id, ArtifactType.VERIFICATION, verification_path)

        # 5. Generate plan.md
        plan_md = await self._claude.generate_plan(issue_data, analysis)
        plan_path = cogniq_dir / "plan.md"
        self.write_text(plan_path, plan_md)
        await self._push_artifact(issue_id, run_id, ArtifactType.PLAN_MD, plan_path)

        # 6. Generate design.md (conditional)
        design_required = analysis.get("design", {}).get("required", False)
        if design_required:
            design_md = await self._claude.generate_design(issue_data, analysis)
            design_path = cogniq_dir / "design.md"
            self.write_text(design_path, design_md)
            await self._push_artifact(issue_id, run_id, ArtifactType.DESIGN_MD, design_path)

        # 7. Decomposition check
        should_split = analysis.get("decompose", {}).get("should_split", False)
        if should_split and self._linear:
            sub_issues = analysis.get("decompose", {}).get("sub_issues", [])
            if sub_issues:
                try:
                    await self._linear.create_sub_issues(issue_id, sub_issues)
                    logger.info("Created %d sub-issues for %s", len(sub_issues), issue_id)
                except Exception as e:
                    logger.warning("Failed to create sub-issues: %s", e)

        # 8. Safety flags
        safety_flags = analysis.get("safety", {}).get("flags", [])
        if safety_flags:
            labels = issue.labels + [f"safety:{f}" for f in safety_flags]
            await self._repo.update(issue_id, {"labels": labels})
            if self._linear:
                try:
                    flags_str = ", ".join(safety_flags)
                    await self._linear.add_comment(issue_id, f"⚠️ 안전 플래그: {flags_str}")
                except Exception:
                    pass

        return RunResult(status="success")

    def _validate_issue(self, issue_data: dict[str, Any]) -> bool:
        """Check abort conditions."""
        description = issue_data.get("description", "") or issue_data.get("linear_description", "")
        if not description or len(description.strip()) < 10:
            return False
        return True

    def _gather_codebase_context(self) -> str:
        """Gather relevant codebase context for analysis.

        In a real implementation, this would:
        1. Read CLAUDE.md for project conventions
        2. List directory structure
        3. Read relevant source files
        For now, returns a placeholder.
        """
        context_parts = []

        # Try to read CLAUDE.md
        claude_md = Path("CLAUDE.md")
        if claude_md.exists():
            context_parts.append(f"# CLAUDE.md\n{claude_md.read_text()[:2000]}")

        # Try to read project structure
        src_dir = Path("src")
        if src_dir.exists():
            files = list(src_dir.rglob("*.py"))[:50]
            context_parts.append("# Project files\n" + "\n".join(str(f) for f in files))

        return "\n\n".join(context_parts) if context_parts else "No codebase context available."

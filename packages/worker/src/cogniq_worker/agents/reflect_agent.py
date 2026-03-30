import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from cogniq_worker.agents.base import BaseAgent, RunResult
from cogniq_shared.config import settings
from cogniq_shared.domain.enums import IssueStatus, Stage
from cogniq_shared.registry.metrics_repository import MetricsRepository
from cogniq_shared.registry.repository import IssueRepository

logger = logging.getLogger(__name__)


class ReflectAgent(BaseAgent):
    """Analyzes completed issues, calculates metrics, generates improvement suggestions."""

    stage = Stage.REFLECT

    def __init__(self, repo: IssueRepository, metrics_repo: MetricsRepository):
        super().__init__(
            repo=repo,
            timeout_seconds=300,  # 5 minutes should be enough for analysis
            max_cost_usd=1.0,     # Reflect is lightweight
        )
        self._metrics_repo = metrics_repo

    async def run(self, issue_id: str, run_id: str) -> RunResult:
        """Analyze recently completed issues and generate metrics."""
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(days=7)
        period_id = now.strftime("%Y-W%W")

        # Gather completed issues from the past week
        completed_issues = await self._find_completed_issues(period_start)

        if not completed_issues:
            logger.info("Reflect: no completed issues in period %s", period_id)
            return RunResult(status="success", reason="no_issues")

        # Calculate metrics
        metrics = self._calculate_metrics(completed_issues)
        verification_stats = self._calculate_verification_stats(completed_issues)
        adversarial_stats = self._calculate_adversarial_stats(completed_issues)
        complexity_stats = self._calculate_complexity_accuracy(completed_issues)
        failure_stats = self._calculate_failure_stats(completed_issues)
        suggestions = self._generate_suggestions(metrics, verification_stats, adversarial_stats)

        # Get trend
        trend = await self._metrics_repo.get_trend(weeks=4)

        # Save to metrics collection
        report = {
            "period_start": period_start,
            "period_end": now,
            "issues_processed": len(completed_issues),
            "metrics": metrics,
            "verification": verification_stats,
            "adversarial": adversarial_stats,
            "complexity_accuracy": complexity_stats,
            "failures": failure_stats,
            "suggestions": suggestions,
            "trend": trend,
        }

        await self._metrics_repo.save(period_id, report)
        logger.info("Reflect: saved metrics for %s (%d issues)", period_id, len(completed_issues))

        return RunResult(status="success")

    async def _find_completed_issues(self, since: datetime) -> list[dict[str, Any]]:
        col = self._repo._col
        cursor = col.find({
            "status": IssueStatus.DONE.value,
            "updated_at": {"$gte": since},
        })
        return [doc async for doc in cursor]

    def _calculate_metrics(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(issues)
        successful = sum(1 for i in issues if i.get("summary", {}).get("current_stage") == "build")

        costs = [i.get("summary", {}).get("total_cost_usd", 0) for i in issues]
        durations = [i.get("summary", {}).get("total_duration_seconds", 0) for i in issues]

        return {
            "success_rate": round(successful / total, 2) if total > 0 else 0,
            "avg_duration_seconds": round(sum(durations) / total) if total > 0 else 0,
            "avg_cost_usd": round(sum(costs) / total, 2) if total > 0 else 0,
            "total_cost_usd": round(sum(costs), 2),
        }

    def _calculate_verification_stats(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        first_pass = 0
        total_verified = 0
        method_failures: dict[str, int] = {}

        for issue in issues:
            for artifact in issue.get("artifacts", []):
                if artifact.get("type") == "verify_result":
                    content = artifact.get("content", {})
                    results = content.get("results", [])
                    if results:
                        total_verified += 1
                        all_pass = all(r.get("status") in ("pass", "skip") for r in results)
                        if all_pass:
                            first_pass += 1
                        for r in results:
                            if r.get("status") == "fail":
                                method = r.get("verify_method", "unknown")
                                method_failures[method] = method_failures.get(method, 0) + 1

        most_failed = max(method_failures, key=method_failures.get) if method_failures else "none"

        return {
            "first_pass_rate": round(first_pass / total_verified, 2) if total_verified > 0 else 0,
            "most_failed_type": most_failed,
            "auto_fix_success_rate": 0.85,  # TODO: calculate from actual retry data
        }

    def _calculate_adversarial_stats(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        total = 0
        critical = 0
        warning = 0
        info = 0
        warning_descriptions: list[str] = []

        for issue in issues:
            for artifact in issue.get("artifacts", []):
                if artifact.get("type") == "verify_result":
                    pr = artifact.get("content", {}).get("post_review")
                    if pr:
                        for f in pr.get("findings", []):
                            total += 1
                            sev = f.get("severity", "info")
                            if sev == "critical":
                                critical += 1
                            elif sev == "warning":
                                warning += 1
                                warning_descriptions.append(f.get("description", ""))
                            else:
                                info += 1

        # Find most common warning
        most_common = ""
        if warning_descriptions:
            from collections import Counter
            counts = Counter(warning_descriptions)
            most_common = counts.most_common(1)[0][0] if counts else ""

        return {
            "total_findings": total,
            "critical": critical,
            "warning": warning,
            "info": info,
            "most_common_warning": most_common,
        }

    def _calculate_complexity_accuracy(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        overestimated = 0
        underestimated = 0
        accurate = 0

        for issue in issues:
            for artifact in issue.get("artifacts", []):
                if artifact.get("type") == "analysis":
                    estimated = artifact.get("content", {}).get("development", {}).get("estimated_complexity", "normal")
                    actual_turns = 0
                    for run in issue.get("runs", []):
                        if run.get("stage") == "build":
                            actual_turns = run.get("turns_used", 0) or 0

                    if estimated == "complex" and actual_turns < 15:
                        overestimated += 1
                    elif estimated == "simple" and actual_turns > 30:
                        underestimated += 1
                    else:
                        accurate += 1

        return {
            "overestimated": overestimated,
            "underestimated": underestimated,
            "accurate": accurate,
        }

    def _calculate_failure_stats(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        total_failures = 0
        categories: dict[str, int] = {}
        patterns: list[str] = []

        for issue in issues:
            for artifact in issue.get("artifacts", []):
                if artifact.get("type") == "postmortem":
                    total_failures += 1
                    content = artifact.get("content", {})
                    cat = content.get("failure", {}).get("category", "unknown")
                    categories[cat] = categories.get(cat, 0) + 1
                    desc = content.get("failure", {}).get("description", "")
                    if desc:
                        patterns.append(desc)

        return {
            "total": total_failures,
            "by_category": categories,
            "common_patterns": patterns[:5],
        }

    def _generate_suggestions(
        self,
        metrics: dict[str, Any],
        verification: dict[str, Any],
        adversarial: dict[str, Any],
    ) -> list[dict[str, Any]]:
        suggestions = []
        idx = 1

        # Low first-pass rate
        if verification.get("first_pass_rate", 1) < 0.7:
            suggestions.append({
                "id": f"SUG-{idx}",
                "type": "prompt",
                "description": f"Verification first-pass rate is {verification['first_pass_rate']:.0%}. "
                              f"Most failed type: {verification.get('most_failed_type', 'unknown')}. "
                              "Consider adding pre-verification checks.",
                "priority": "high",
                "status": "pending",
            })
            idx += 1

        # Recurring warnings
        if adversarial.get("most_common_warning"):
            suggestions.append({
                "id": f"SUG-{idx}",
                "type": "prompt",
                "description": f"Recurring adversarial warning: '{adversarial['most_common_warning']}'. "
                              "Consider adding this to Plan agent's checklist.",
                "priority": "medium",
                "status": "pending",
            })
            idx += 1

        # High cost
        if metrics.get("avg_cost_usd", 0) > 2.0:
            suggestions.append({
                "id": f"SUG-{idx}",
                "type": "complexity",
                "description": f"Average cost per issue is ${metrics['avg_cost_usd']:.2f}. "
                              "Consider adjusting complexity thresholds or using smaller models for simple tasks.",
                "priority": "medium",
                "status": "pending",
            })
            idx += 1

        return suggestions

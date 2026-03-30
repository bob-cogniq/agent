import asyncio
import logging
import tomllib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomli_w

from cogniq_shared.domain.enums import ArtifactType, EventType, RunStatus, Stage
from cogniq_shared.domain.issue import Artifact, Event, Run
from cogniq_shared.registry.repository import IssueRepository

logger = logging.getLogger(__name__)


class CostLimitExceeded(Exception):
    def __init__(self, current: float, limit: float):
        self.current = current
        self.limit = limit
        super().__init__(f"Cost limit exceeded: ${current:.2f} > ${limit:.2f}")


class CostTracker:
    """Tracks token usage and cost, raises CostLimitExceeded when over budget."""

    # Approximate pricing per 1M tokens (input/output)
    PRICING: dict[str, tuple[float, float]] = {
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-opus-4-20250514": (15.0, 75.0),
        "claude-haiku-3-20250307": (0.25, 1.25),
    }
    DEFAULT_PRICING = (3.0, 15.0)

    def __init__(self, max_usd: float):
        self.total_usd: float = 0.0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self._max_usd = max_usd

    def add(self, input_tokens: int, output_tokens: int, model: str = "") -> float:
        input_rate, output_rate = self.PRICING.get(model, self.DEFAULT_PRICING)
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        self.total_usd += cost
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

        if self.total_usd > self._max_usd:
            raise CostLimitExceeded(self.total_usd, self._max_usd)
        return cost

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


class RunResult:
    def __init__(self, status: str = "success", reason: str = "", pr_url: str = "", **extra: Any):
        self.status = status
        self.reason = reason
        self.pr_url = pr_url
        self.extra = extra


class BaseAgent(ABC):
    """Base class for all agents. Provides timeout, cost tracking, and Registry push."""

    stage: Stage  # Subclass must define

    def __init__(
        self,
        repo: IssueRepository,
        timeout_seconds: int = 600,
        max_cost_usd: float = 5.0,
    ):
        self._repo = repo
        self._timeout = timeout_seconds
        self._cost_tracker = CostTracker(max_usd=max_cost_usd)

    async def execute(self, issue_id: str) -> RunResult:
        """Execute with timeout, cost tracking, and error handling wrapper."""
        run = Run(
            stage=self.stage,
            status=RunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        run_id = await self._repo.add_run(issue_id, run)
        await self._emit_event(issue_id, EventType(f"{self.stage}_started"), run_id)

        try:
            async with asyncio.timeout(self._timeout):
                result = await self.run(issue_id, run_id)

            # Success
            completed_at = datetime.now(timezone.utc)
            duration = int((completed_at - run.started_at).total_seconds()) if run.started_at else 0
            await self._repo.update_run(issue_id, run_id, {
                "status": RunStatus.COMPLETED.value,
                "completed_at": completed_at,
                "duration_seconds": duration,
                "cost_usd": self._cost_tracker.total_usd,
                "tokens_used": self._cost_tracker.total_tokens,
            })
            await self._repo.update_summary(issue_id)
            await self._emit_event(issue_id, EventType(f"{self.stage}_completed"), run_id, {
                "cost_usd": round(self._cost_tracker.total_usd, 4),
                "tokens_used": self._cost_tracker.total_tokens,
            })
            return result

        except TimeoutError:
            await self._handle_failure(issue_id, run_id, "timeout", "Agent timed out")
            raise
        except CostLimitExceeded as e:
            await self._handle_failure(issue_id, run_id, "cost_limit", str(e))
            raise
        except Exception as e:
            await self._handle_failure(issue_id, run_id, "permanent", str(e))
            raise

    async def _handle_failure(self, issue_id: str, run_id: str, category: str, error: str) -> None:
        logger.error("Agent %s failed for %s: [%s] %s", self.stage, issue_id, category, error)
        await self._repo.update_run(issue_id, run_id, {
            "status": RunStatus.FAILED.value,
            "completed_at": datetime.now(timezone.utc),
            "error": error,
            "cost_usd": self._cost_tracker.total_usd,
            "tokens_used": self._cost_tracker.total_tokens,
        })
        await self._repo.update_summary(issue_id)

        # Create postmortem artifact
        postmortem = {
            "meta": {"issue_id": issue_id, "created_by": self.stage.value},
            "failure": {
                "description": error,
                "category": category,
            },
            "context": {
                "tokens_used": self._cost_tracker.total_tokens,
                "cost_usd": round(self._cost_tracker.total_usd, 4),
            },
        }
        artifact = Artifact(
            run_id=run_id,
            type=ArtifactType.POSTMORTEM,
            content=postmortem,
        )
        await self._repo.add_artifact(issue_id, artifact)
        await self._emit_event(issue_id, EventType(f"{self.stage}_failed"), run_id, {"error": error})

    async def _push_artifact(
        self,
        issue_id: str,
        run_id: str,
        artifact_type: ArtifactType,
        file_path: Path,
    ) -> None:
        """Push a local file to Registry. Fire-and-forget — failure doesn't stop the agent."""
        try:
            content, content_md = self._parse_file(file_path)

            # Determine version
            existing = await self._repo.list_artifacts(issue_id, artifact_type.value)
            version = max((a.version for a in existing), default=0) + 1

            artifact = Artifact(
                run_id=run_id,
                type=artifact_type,
                version=version,
                content=content,
                content_md=content_md,
            )
            await self._repo.add_artifact(issue_id, artifact)
            logger.debug("Pushed artifact: %s v%d for %s", artifact_type, version, issue_id)
        except Exception as e:
            logger.warning("Registry push failed for %s/%s: %s", issue_id, artifact_type, e)

    async def _emit_event(
        self,
        issue_id: str,
        event_type: EventType,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit event to Registry. Fire-and-forget."""
        try:
            event = Event(run_id=run_id, type=event_type, payload=payload or {})
            await self._repo.add_event(issue_id, event)
        except Exception as e:
            logger.warning("Event emit failed for %s/%s: %s", issue_id, event_type, e)

    @staticmethod
    def _parse_file(file_path: Path) -> tuple[dict[str, Any] | None, str | None]:
        """Parse TOML → dict or MD → string."""
        suffix = file_path.suffix.lower()
        text = file_path.read_text(encoding="utf-8")

        if suffix == ".toml":
            return tomllib.loads(text), None
        elif suffix in (".md", ".markdown"):
            return None, text
        else:
            return {"raw": text}, None

    @staticmethod
    def write_toml(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cleaned = _strip_none(data)
        with open(path, "wb") as f:
            tomli_w.dump(cleaned, f)


    @staticmethod
    def write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @abstractmethod
    async def run(self, issue_id: str, run_id: str) -> RunResult:
        """Subclass implements the actual agent logic."""
        ...


def _strip_none(obj: Any) -> Any:
    """Recursively remove None values from dicts (TOML doesn't support None)."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(i) for i in obj]
    return obj

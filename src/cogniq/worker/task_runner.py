import logging

from cogniq.agents.build_agent import BuildAgent
from cogniq.agents.plan_agent import PlanAgent
from cogniq.registry.repository import IssueRepository
from cogniq.taskqueue.models import TaskDocument, TaskResult

logger = logging.getLogger(__name__)


async def run_agent(task: TaskDocument, repo: IssueRepository) -> TaskResult:
    """Map task stage to agent, execute, return result."""
    logger.info("Running agent: issue=%s stage=%s", task.issue_id, task.stage)

    if task.stage == "plan":
        agent = PlanAgent(repo=repo)
    elif task.stage == "build":
        agent = BuildAgent(repo=repo)
    else:
        return TaskResult(status="failed", error=f"Unknown stage: {task.stage}")

    result = await agent.execute(task.issue_id)

    return TaskResult(
        status=result.status,
        reason=result.reason,
        pr_url=result.pr_url,
        cost_usd=agent._cost_tracker.total_usd,
        tokens_used=agent._cost_tracker.total_tokens,
    )

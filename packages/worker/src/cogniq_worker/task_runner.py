import logging

from cogniq_worker.agents.build_agent import BuildAgent
from cogniq_worker.agents.plan_agent import PlanAgent
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.models import TaskDocument, TaskResult
from cogniq_worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


async def run_agent(task: TaskDocument, repo: IssueRepository, workspace_mgr: WorkspaceManager) -> TaskResult:
    """Map task stage to agent, execute, return result."""
    logger.info("Running agent: issue=%s stage=%s", task.issue_id, task.stage)

    if task.stage == "plan":
        agent = PlanAgent(repo=repo)
        result = await agent.execute(task.issue_id)
    elif task.stage == "build":
        agent = BuildAgent(repo=repo, workspace_mgr=workspace_mgr)
        result = await agent.execute(task.issue_id)
    else:
        return TaskResult(status="failed", error=f"Unknown stage: {task.stage}")

    return TaskResult(
        status=result.status,
        reason=result.reason,
        pr_url=result.pr_url,
        cost_usd=agent._cost_tracker.total_usd,
        tokens_used=agent._cost_tracker.total_tokens,
    )

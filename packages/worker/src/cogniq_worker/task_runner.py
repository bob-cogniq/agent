import logging
from datetime import datetime, timezone

from cogniq_worker.agents.build_agent import BuildAgent
from cogniq_worker.agents.plan_agent import PlanAgent
from cogniq_worker.agents.claude_code import ClaudeCodeRunner
from cogniq_worker.agents.base import CostTracker
from cogniq_shared.config import settings
from cogniq_shared.domain.code_session import CodeMessage, ChangedFile
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
    elif task.stage == "continue":
        return await _handle_continue(task, repo, workspace_mgr)
    else:
        return TaskResult(status="failed", error=f"Unknown stage: {task.stage}")

    return TaskResult(
        status=result.status,
        reason=result.reason,
        pr_url=result.pr_url,
        cost_usd=agent._cost_tracker.total_usd,
        tokens_used=agent._cost_tracker.total_tokens,
    )


async def _handle_continue(
    task: TaskDocument, repo: IssueRepository, workspace_mgr: WorkspaceManager,
) -> TaskResult:
    """Continue an existing Claude Code session with a follow-up prompt."""
    config = task.config or {}
    session_id = config.get("session_id", "")
    cli_session_id = config.get("cli_session_id", "")
    prompt = config.get("prompt", "")

    if not cli_session_id or not prompt:
        return TaskResult(status="failed", error="Missing cli_session_id or prompt")

    # Load issue to find workspace
    issue = await repo.get(task.issue_id)
    if not issue:
        return TaskResult(status="failed", error="Issue not found")

    # Reuse existing workspace
    workspace = await workspace_mgr.get_workspace(issue.project_id)
    if not workspace:
        return TaskResult(status="failed", error="Workspace not found (worktree may have been cleaned)")

    # Run Claude Code with --resume
    cost_tracker = CostTracker(max_usd=settings.build_max_cost_usd)
    runner = ClaudeCodeRunner(cost_tracker=cost_tracker, max_turns=settings.build_max_turns)

    logger.info("Continuing session %s for issue %s", cli_session_id, task.issue_id)
    code_result = await runner.resume(
        workspace_root=workspace.root_path,
        cli_session_id=cli_session_id,
        prompt=prompt,
    )

    # Append new messages to existing session
    for msg_data in code_result.messages:
        msg = CodeMessage(**msg_data)
        await repo.add_code_message(task.issue_id, session_id, msg)

    # Update session aggregates
    session = await repo.get_code_session(task.issue_id, session_id)
    if session:
        new_turns = session.total_turns + code_result.turns_used
        new_tokens = {
            "input": session.total_tokens.get("input", 0) + code_result.total_input_tokens,
            "output": session.total_tokens.get("output", 0) + code_result.total_output_tokens,
        }
        new_cost = session.total_cost_usd + code_result.total_cost_usd

        updates = {
            "status": "completed" if code_result.success else "failed",
            "total_turns": new_turns,
            "total_tokens": new_tokens,
            "total_cost_usd": new_cost,
            "cli_session_id": code_result.cli_session_id or cli_session_id,
            "completed_at": datetime.now(timezone.utc),
            "error": code_result.error[:500] if code_result.error else None,
        }
        await repo.update_code_session(task.issue_id, session_id, updates)

    status = "success" if code_result.success else "failed"
    return TaskResult(
        status=status,
        cost_usd=cost_tracker.total_usd,
        tokens_used=cost_tracker.total_tokens,
    )

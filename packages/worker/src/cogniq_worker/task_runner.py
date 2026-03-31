import logging
from datetime import datetime, timezone

from cogniq_worker.agents.build_agent import BuildAgent
from cogniq_worker.agents.plan_agent import PlanAgent
from cogniq_shared.config import settings
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.models import TaskDocument, TaskResult
from cogniq_shared.taskqueue.repository import TaskQueueRepository
from cogniq_worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


async def run_agent(
    task: TaskDocument,
    repo: IssueRepository,
    workspace_mgr: WorkspaceManager,
    task_queue: TaskQueueRepository | None = None,
    session_registry: "SessionRegistry | None" = None,  # type: ignore[name-defined]
) -> TaskResult:
    """Map task stage to agent, execute, return result."""
    logger.info("Running agent: issue=%s stage=%s", task.issue_id, task.stage)

    if task.stage == "plan":
        agent = PlanAgent(repo=repo)
        result = await agent.execute(task.issue_id)
    elif task.stage == "build":
        agent = BuildAgent(repo=repo, workspace_mgr=workspace_mgr)
        result = await agent.execute(task.issue_id)
    elif task.stage == "continue":
        return await _handle_continue(task, repo, workspace_mgr, task_queue, session_registry)
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
    task: TaskDocument,
    repo: IssueRepository,
    workspace_mgr: WorkspaceManager,
    task_queue: TaskQueueRepository | None = None,
    session_registry: "SessionRegistry | None" = None,  # type: ignore[name-defined]
) -> TaskResult:
    """Continue a Claude Code session.

    Strategy (Claude Desktop style):
    1. If a SessionRegistry is available and the issue has an active session,
       route the message directly into the running ClaudeSDKClient (same
       conversation context, no subprocess re-spawn).
    2. Otherwise, fall back to SDK-based resume (new SDK client with --resume).
    """
    from cogniq_worker.session_manager import SessionRegistry

    config = task.config or {}
    session_id = config.get("session_id", "")
    cli_session_id = config.get("cli_session_id", "")
    prompt = config.get("prompt", "")

    if not prompt:
        return TaskResult(status="failed", error="Missing prompt in continue task config")

    # ── Path A: Active persistent session ──
    if session_registry and session_registry.is_active(task.issue_id):
        mgr = session_registry.get(task.issue_id)
        if mgr:
            logger.info("Routing to active session for issue %s", task.issue_id)
            await mgr.send_message(session_id, prompt)
            return TaskResult(status="success")

    # ── Path B: Start / resume with ClaudeSDKClient ──
    issue = await repo.get(task.issue_id)
    if not issue:
        return TaskResult(status="failed", error="Issue not found")

    workspace = await workspace_mgr.get_workspace(issue.project_id)
    if not workspace:
        return TaskResult(status="failed", error="Workspace not found")

    if session_registry:
        # Create a persistent manager that will stay alive for follow-ups
        mgr = session_registry.create_and_register(
            issue_id=task.issue_id,
            workspace_root=workspace.root_path,
            repo=repo,
            task_queue=task_queue or _noop_task_queue(),
            max_turns=settings.build_max_turns,
        )
        mgr.start(
            session_id=session_id,
            initial_prompt=prompt,
            cli_session_id=cli_session_id or None,
            project_id=issue.project_id,
        )
        # Wait for the first execution to complete
        if mgr._task:
            try:
                await mgr._task
            except Exception as e:
                logger.error("Session task failed: %s", e)
                return TaskResult(status="failed", error=str(e)[:500])
    else:
        # No registry — use SDK runner directly (stateless resume)
        from cogniq_worker.agents.claude_sdk import ClaudeSDKRunner
        from cogniq_worker.agents.base import CostTracker
        from cogniq_shared.domain.code_session import CodeMessage

        cost_tracker = CostTracker(max_usd=settings.build_max_cost_usd)
        runner = ClaudeSDKRunner(cost_tracker=cost_tracker, max_turns=settings.build_max_turns)

        code_result = await runner.resume(
            workspace_root=workspace.root_path,
            cli_session_id=cli_session_id,
            prompt=prompt,
        )

        for msg_data in code_result.messages:
            msg = CodeMessage(**msg_data)
            await repo.add_code_message(task.issue_id, session_id, msg)

        session = await repo.get_code_session(task.issue_id, session_id)
        if session:
            new_turns = session.total_turns + code_result.turns_used
            new_status = "completed" if code_result.success else "failed"
            await repo.update_code_session(
                task.issue_id, session_id,
                {
                    "status": new_status,
                    "total_turns": new_turns,
                    "total_tokens": {
                        "input": session.total_tokens.get("input", 0) + code_result.total_input_tokens,
                        "output": session.total_tokens.get("output", 0) + code_result.total_output_tokens,
                    },
                    "total_cost_usd": session.total_cost_usd + code_result.total_cost_usd,
                    "cli_session_id": code_result.cli_session_id or cli_session_id,
                    "completed_at": datetime.now(timezone.utc),
                    "error": code_result.error[:500] if code_result.error else None,
                },
                only_if_status="running",
            )

        # Process any queued messages
        if task_queue:
            next_msg = await repo.pop_queued_message(task.issue_id, session_id)
            if next_msg:
                await task_queue.enqueue(
                    issue_id=task.issue_id,
                    project_id=issue.project_id,
                    stage="continue",
                    config={
                        "session_id": session_id,
                        "cli_session_id": code_result.cli_session_id or cli_session_id,
                        "prompt": next_msg.prompt,
                    },
                )

    return TaskResult(status="success")


def _noop_task_queue():
    """Placeholder when no task_queue is available."""
    class _Noop:
        async def enqueue(self, **_):
            pass
    return _Noop()

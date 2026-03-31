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
    elif task.stage == "code_chat":
        return await _handle_code_chat(task, workspace_mgr, task_queue, session_registry)
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

    # Pop the FIRST message from DB queue (FIFO ordering)
    first_msg = await repo.pop_queued_message(task.issue_id, session_id)
    if not first_msg:
        # Queue empty — nothing to process
        logger.info("No queued messages for session %s — skipping", session_id)
        return TaskResult(status="success")

    prompt = first_msg.prompt

    # Save user message to chat history
    from cogniq_shared.domain.code_session import CodeMessage
    session = await repo.get_code_session(task.issue_id, session_id)
    turn = (session.total_turns if session else 0) + 1
    user_msg = CodeMessage(turn=turn, role="user", content=prompt)
    await repo.add_code_message(task.issue_id, session_id, user_msg)

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
        # Wait only for the first execution — session loop stays alive
        # in the background for follow-up messages
        try:
            await mgr.wait_first_done(timeout=600)
        except Exception as e:
            logger.error("Session first execution failed: %s", e)
            return TaskResult(status="failed", error=str(e)[:500])
    else:
        # No registry — use SDK runner directly (stateless resume)
        from cogniq_worker.agents.claude_sdk import ClaudeSDKRunner
        from cogniq_worker.agents.base import CostTracker

        cost_tracker = CostTracker(max_usd=settings.build_max_cost_usd)
        runner = ClaudeSDKRunner(cost_tracker=cost_tracker, max_turns=settings.build_max_turns)

        code_result = await runner.resume(
            workspace_root=workspace.root_path,
            cli_session_id=cli_session_id,
            prompt=prompt,
        )

        for msg_data in code_result.messages:
            await repo.add_code_message(task.issue_id, session_id, CodeMessage(**msg_data))

        session = await repo.get_code_session(task.issue_id, session_id)
        if session:
            new_status = "completed" if code_result.success else "failed"
            await repo.update_code_session(
                task.issue_id, session_id,
                {
                    "status": new_status,
                    "total_turns": session.total_turns + code_result.turns_used,
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

        # If more messages in queue, create next drain task
        if task_queue:
            next_msg = await repo.pop_queued_message(task.issue_id, session_id)
            if next_msg:
                # Put it back — the next task will pop it
                await repo.enqueue_message(task.issue_id, session_id, next_msg)
                await repo.update_code_session(task.issue_id, session_id, {"status": "running"})
                await task_queue.enqueue(
                    issue_id=task.issue_id,
                    project_id=issue.project_id,
                    stage="continue",
                    config={"session_id": session_id, "cli_session_id": code_result.cli_session_id or cli_session_id},
                )

    return TaskResult(status="success")


async def _handle_code_chat(
    task: TaskDocument,
    workspace_mgr: WorkspaceManager,
    task_queue: TaskQueueRepository | None = None,
    session_registry: "SessionRegistry | None" = None,  # type: ignore[name-defined]
) -> TaskResult:
    """Handle a standalone code chat task (not tied to an issue)."""
    from cogniq_shared.domain.code_chat_repository import CodeChatRepository
    from cogniq_shared.domain.code_session import CodeMessage
    from cogniq_shared.registry.database import get_database

    config = task.config or {}
    chat_id = config.get("chat_id", "")
    prompt = config.get("prompt", "")  # Only set for first message

    db = get_database()
    chat_repo = CodeChatRepository(db)

    chat = await chat_repo.get(chat_id)
    if not chat:
        return TaskResult(status="failed", error="Chat not found")

    # If no prompt in config, pop from queue (FIFO)
    if not prompt:
        first_msg = await chat_repo.pop_queued_message(chat_id)
        if not first_msg:
            return TaskResult(status="success")
        prompt = first_msg.prompt
        user_msg = CodeMessage(turn=chat.total_turns + 1, role="user", content=prompt)
        await chat_repo.add_message(chat_id, user_msg)

    # Get workspace
    workspace = await workspace_mgr.get_workspace(task.project_id)
    if not workspace:
        return TaskResult(status="failed", error="Workspace not found")

    # Use SDK runner
    from cogniq_worker.agents.claude_sdk import ClaudeSDKRunner
    from cogniq_worker.agents.base import CostTracker

    cost_tracker = CostTracker(max_usd=settings.build_max_cost_usd)
    runner = ClaudeSDKRunner(cost_tracker=cost_tracker, max_turns=settings.build_max_turns)

    if chat.cli_session_id:
        code_result = await runner.resume(
            workspace_root=workspace.root_path,
            cli_session_id=chat.cli_session_id,
            prompt=prompt,
        )
    else:
        code_result = await runner.run(
            workspace_root=workspace.root_path,
            prompt=prompt,
        )

    # Save messages — skip plain user text (already saved above or by API)
    # Keep tool_use, tool_result, assistant messages
    for msg_data in code_result.messages:
        role = msg_data.get("role", "")
        if role == "user" and not msg_data.get("tool_name"):
            continue  # Skip echoed user prompts, but keep tool_result
        await chat_repo.add_message(chat_id, CodeMessage(**msg_data))

    # Update chat
    new_status = "completed" if code_result.success else "failed"
    await chat_repo.update(chat_id, {
        "status": new_status,
        "cli_session_id": code_result.cli_session_id or chat.cli_session_id,
        "total_turns": chat.total_turns + code_result.turns_used,
        "total_tokens": {
            "input": chat.total_tokens.get("input", 0) + code_result.total_input_tokens,
            "output": chat.total_tokens.get("output", 0) + code_result.total_output_tokens,
        },
        "total_cost_usd": chat.total_cost_usd + code_result.total_cost_usd,
    }, only_if_status="running")

    # Don't create drain tasks here — continue API handles that.
    # This prevents double execution of queued messages.

    return TaskResult(status="success")


def _noop_task_queue():
    """Placeholder when no task_queue is available."""
    class _Noop:
        async def enqueue(self, **_):
            pass
    return _Noop()

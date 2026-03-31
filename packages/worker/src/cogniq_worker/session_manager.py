"""IssueSessionManager — Claude Desktop-style persistent session per issue.

Each issue gets one ClaudeSDKClient that stays alive across multiple user
messages. Follow-up prompts are fed into the same client (same conversation
context), processed sequentially via an asyncio.Queue.

SessionRegistry is a process-level singleton that maps issue_id → manager.
"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
from claude_code_sdk._errors import MessageParseError
from claude_code_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
)

from cogniq_shared.config import settings
from cogniq_shared.domain.code_session import CodeMessage, ChangedFile
from cogniq_shared.registry.repository import IssueRepository
from cogniq_shared.taskqueue.repository import TaskQueueRepository

logger = logging.getLogger(__name__)


class IssueSessionManager:
    """Manages a single ClaudeSDKClient for one issue.

    Lifecycle:
      1. start() — opens the client, runs the initial prompt
      2. send_message() — enqueues follow-up prompts
      3. Worker loop calls receive_message() from the asyncio.Queue in sequence
      4. Session ends when queue is idle (timeout) or stop() is called
    """

    def __init__(
        self,
        issue_id: str,
        workspace_root: Path,
        repo: IssueRepository,
        task_queue: TaskQueueRepository,
        max_turns: int = 50,
    ):
        self.issue_id = issue_id
        self.workspace_root = workspace_root
        self._repo = repo
        self._task_queue = task_queue
        self._max_turns = max_turns

        self._client: ClaudeSDKClient | None = None
        self._message_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        # Queue contains (session_id, prompt) tuples

        self._active = False
        self._task: asyncio.Task | None = None
        self._cli_session_id: str = ""  # updated after first ResultMessage
        self._first_done = asyncio.Event()  # set after first _execute completes
        self._first_error: Exception | None = None

    # ── Public API ──

    def start(
        self,
        session_id: str,
        initial_prompt: str,
        cli_session_id: str | None = None,
        project_id: str = "",
    ) -> None:
        """Launch the session loop as a background task."""
        self._active = True
        self._cli_session_id = cli_session_id or ""
        self._project_id = project_id
        self._task = asyncio.create_task(
            self._run_loop(session_id, initial_prompt),
            name=f"session-{self.issue_id}",
        )
        self._task.add_done_callback(self._on_done)

    async def send_message(self, session_id: str, prompt: str) -> None:
        """Enqueue a follow-up prompt for sequential processing."""
        await self._message_queue.put((session_id, prompt))

    async def interrupt(self) -> None:
        """Send interrupt signal to the running client."""
        if self._client:
            await self._client.interrupt()

    def is_active(self) -> bool:
        return self._active and (self._task is not None and not self._task.done())

    def stop(self) -> None:
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait_first_done(self, timeout: float = 600) -> None:
        """Wait until the first execution completes (not the full session loop).

        After this returns the session loop continues in the background,
        ready to process queued follow-up messages without blocking the
        Worker main loop.

        Raises the first execution's error if it failed.
        """
        await asyncio.wait_for(self._first_done.wait(), timeout=timeout)
        if self._first_error:
            raise self._first_error

    # ── Internal loop ──

    def _on_done(self, task: asyncio.Task) -> None:
        self._active = False
        if not task.cancelled() and task.exception():
            logger.error("Session %s failed: %s", self.issue_id, task.exception())

    async def _run_loop(self, first_session_id: str, first_prompt: str) -> None:
        """Open client, execute first prompt, then drain queue until idle."""
        opts = ClaudeCodeOptions(
            permission_mode="bypassPermissions",
            max_turns=self._max_turns,
            cwd=str(self.workspace_root),
            include_partial_messages=True,
        )
        if self._cli_session_id:
            opts.resume = self._cli_session_id

        try:
            async with ClaudeSDKClient(options=opts) as client:
                self._client = client

                # First message
                try:
                    await self._execute(client, first_session_id, first_prompt)
                except Exception as e:
                    self._first_error = e
                    raise
                finally:
                    self._first_done.set()

                # Process queued messages (background — Worker loop unblocks)
                while self._active:
                    try:
                        session_id, prompt = await asyncio.wait_for(
                            self._message_queue.get(),
                            timeout=settings.session_idle_timeout_seconds if hasattr(settings, "session_idle_timeout_seconds") else 300,
                        )
                        await self._execute(client, session_id, prompt)
                    except asyncio.TimeoutError:
                        logger.info("Session %s idle timeout — closing", self.issue_id)
                        break
        except asyncio.CancelledError:
            logger.info("Session %s cancelled", self.issue_id)
        except Exception as e:
            logger.error("Session %s error: %s", self.issue_id, e, exc_info=True)
            self._first_error = self._first_error or e
        finally:
            self._first_done.set()  # Ensure callers never hang
            self._client = None
            self._active = False

    async def _execute(self, client: ClaudeSDKClient, session_id: str, prompt: str) -> None:
        """Execute one prompt against the persistent client."""
        logger.info("Session %s executing prompt (session_id=%s)", self.issue_id, session_id)
        turn_offset = await self._get_turn_offset(session_id)

        messages: list[dict] = []
        files_changed: list[str] = []
        turn = 0

        await client.query(prompt)

        async for message in self._safe_receive(client):
            if isinstance(message, AssistantMessage):
                turn += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        messages.append({"turn": turn_offset + turn, "role": "assistant", "content": block.text})
                    elif isinstance(block, ToolUseBlock):
                        messages.append({
                            "turn": turn_offset + turn, "role": "tool_use",
                            "content": block.input, "tool_name": block.name, "tool_input": block.input,
                        })
                        if block.name in ("Write", "Edit"):
                            path = block.input.get("file_path", "")
                            if path and path not in files_changed:
                                files_changed.append(path)

            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            messages.append({
                                "turn": turn_offset + turn, "role": "tool_result",
                                "content": block.content, "tool_name": block.tool_use_id,
                            })
                else:
                    messages.append({"turn": turn_offset + turn, "role": "user", "content": str(content)})

            elif isinstance(message, ResultMessage):
                # Save cli_session_id for future resumes
                if message.session_id:
                    self._cli_session_id = message.session_id

                usage = message.usage or {}
                session = await self._repo.get_code_session(self.issue_id, session_id)
                if session:
                    new_turns = session.total_turns + message.num_turns
                    new_tokens = {
                        "input": session.total_tokens.get("input", 0) + usage.get("input_tokens", 0),
                        "output": session.total_tokens.get("output", 0) + usage.get("output_tokens", 0),
                    }
                    new_cost = session.total_cost_usd + (message.total_cost_usd or 0.0)
                    new_status = "failed" if message.is_error else "completed"

                    # Persist all new messages
                    for m in messages:
                        await self._repo.add_code_message(self.issue_id, session_id, CodeMessage(**m))

                    await self._repo.update_code_session(
                        self.issue_id, session_id,
                        {
                            "status": new_status,
                            "cli_session_id": self._cli_session_id,
                            "total_turns": new_turns,
                            "total_tokens": new_tokens,
                            "total_cost_usd": new_cost,
                            "completed_at": datetime.now(timezone.utc),
                            "error": (message.result or "")[:500] if message.is_error else None,
                        },
                        only_if_status="running",
                    )

                # Process next queued message if any
                next_msg = await self._repo.pop_queued_message(self.issue_id, session_id)
                if next_msg:
                    logger.info("Auto-processing queued message for session %s", session_id)
                    await self._message_queue.put((session_id, next_msg.prompt))
                    # Mark session running again for the queued message
                    user_msg = CodeMessage(
                        turn=turn_offset + turn + 1,
                        role="user",
                        content=next_msg.prompt,
                    )
                    await self._repo.add_code_message(self.issue_id, session_id, user_msg)
                    await self._repo.update_code_session(self.issue_id, session_id, {"status": "running"})

            elif isinstance(message, StreamEvent):
                # Future: relay to SSE broadcaster for text-chunk streaming
                pass

    @staticmethod
    async def _safe_receive(client: ClaudeSDKClient):
        """Wrap receive_response to skip unknown message types (e.g. rate_limit_event)."""
        it = client.receive_response()
        while True:
            try:
                message = await it.__anext__()
                yield message
            except StopAsyncIteration:
                break
            except MessageParseError as e:
                logger.debug("Skipping unknown message type: %s", e)
                continue

    async def _get_turn_offset(self, session_id: str) -> int:
        """Return current total_turns so new messages have correct turn numbers."""
        session = await self._repo.get_code_session(self.issue_id, session_id)
        return session.total_turns if session else 0


class SessionRegistry:
    """Process-level registry of active IssueSessionManagers.

    One instance per Worker process. Maps issue_id → IssueSessionManager.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, IssueSessionManager] = {}

    def get(self, issue_id: str) -> IssueSessionManager | None:
        mgr = self._sessions.get(issue_id)
        if mgr and not mgr.is_active():
            del self._sessions[issue_id]
            return None
        return mgr

    def register(self, issue_id: str, manager: IssueSessionManager) -> None:
        self._sessions[issue_id] = manager

    def remove(self, issue_id: str) -> None:
        if issue_id in self._sessions:
            self._sessions[issue_id].stop()
            del self._sessions[issue_id]

    def is_active(self, issue_id: str) -> bool:
        return self.get(issue_id) is not None

    def create_and_register(
        self,
        issue_id: str,
        workspace_root: Path,
        repo: IssueRepository,
        task_queue: TaskQueueRepository,
        max_turns: int = 50,
    ) -> IssueSessionManager:
        """Create a new IssueSessionManager and register it."""
        mgr = IssueSessionManager(
            issue_id=issue_id,
            workspace_root=workspace_root,
            repo=repo,
            task_queue=task_queue,
            max_turns=max_turns,
        )
        self._sessions[issue_id] = mgr
        return mgr

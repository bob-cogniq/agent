"""IssueSessionManager — Claude Desktop-style persistent session per issue.

Each issue gets one ClaudeSDKClient that stays alive across multiple user
messages. Follow-up prompts are fed into the same client (same conversation
context), processed sequentially via an asyncio.Queue.

Phase 3: Messages are saved to DB incrementally (each AssistantMessage/
ToolUseBlock immediately) so SSE change-stream picks them up in near
real-time.

Phase 4: An interrupt watcher task monitors the session document for
interrupt_requested=True and calls client.interrupt() when detected.

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
    """Manages a single ClaudeSDKClient for one issue."""

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

        self._active = False
        self._task: asyncio.Task | None = None
        self._interrupt_watcher: asyncio.Task | None = None
        self._cli_session_id: str = ""
        self._first_done = asyncio.Event()
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
        await self._message_queue.put((session_id, prompt))

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    def is_active(self) -> bool:
        return self._active and (self._task is not None and not self._task.done())

    def stop(self) -> None:
        self._active = False
        if self._interrupt_watcher and not self._interrupt_watcher.done():
            self._interrupt_watcher.cancel()
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait_first_done(self, timeout: float = 600) -> None:
        """Wait until the first execution completes (not the full session loop)."""
        await asyncio.wait_for(self._first_done.wait(), timeout=timeout)
        if self._first_error:
            raise self._first_error

    # ── Internal loop ──

    def _on_done(self, task: asyncio.Task) -> None:
        self._active = False
        if self._interrupt_watcher and not self._interrupt_watcher.done():
            self._interrupt_watcher.cancel()
        if not task.cancelled() and task.exception():
            logger.error("Session %s failed: %s", self.issue_id, task.exception())

    async def _run_loop(self, first_session_id: str, first_prompt: str) -> None:
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

                # Start interrupt watcher
                self._interrupt_watcher = asyncio.create_task(
                    self._watch_interrupt(first_session_id),
                    name=f"interrupt-{self.issue_id}",
                )

                # First message
                try:
                    await self._execute(client, first_session_id, first_prompt)
                except Exception as e:
                    self._first_error = e
                    raise
                finally:
                    self._first_done.set()

                # Process queued messages (background)
                idle_timeout = getattr(settings, "session_idle_timeout_seconds", 300)
                while self._active:
                    try:
                        session_id, prompt = await asyncio.wait_for(
                            self._message_queue.get(), timeout=idle_timeout,
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
            self._first_done.set()
            if self._interrupt_watcher and not self._interrupt_watcher.done():
                self._interrupt_watcher.cancel()
            self._client = None
            self._active = False

    async def _execute(self, client: ClaudeSDKClient, session_id: str, prompt: str) -> None:
        """Execute one prompt. Saves messages incrementally for real-time SSE."""
        logger.info("Session %s executing (session_id=%s)", self.issue_id, session_id)
        turn_offset = await self._get_turn_offset(session_id)
        turn = 0

        await client.query(prompt)

        async for message in self._safe_receive(client):
            if isinstance(message, AssistantMessage):
                turn += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        # Phase 3: save immediately → SSE detects change
                        msg = CodeMessage(
                            turn=turn_offset + turn, role="assistant", content=block.text,
                        )
                        await self._repo.add_code_message(self.issue_id, session_id, msg)
                    elif isinstance(block, ToolUseBlock):
                        msg = CodeMessage(
                            turn=turn_offset + turn, role="tool_use",
                            content=block.input, tool_name=block.name, tool_input=block.input,
                        )
                        await self._repo.add_code_message(self.issue_id, session_id, msg)

            elif isinstance(message, UserMessage):
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            msg = CodeMessage(
                                turn=turn_offset + turn, role="tool_result",
                                content=block.content, tool_name=block.tool_use_id,
                            )
                            await self._repo.add_code_message(self.issue_id, session_id, msg)
                # Don't save user-typed prompts here (already saved by API)

            elif isinstance(message, ResultMessage):
                if message.session_id:
                    self._cli_session_id = message.session_id

                usage = message.usage or {}
                session = await self._repo.get_code_session(self.issue_id, session_id)
                if session:
                    new_turns = session.total_turns + message.num_turns
                    new_status = "failed" if message.is_error else "completed"
                    await self._repo.update_code_session(
                        self.issue_id, session_id,
                        {
                            "status": new_status,
                            "cli_session_id": self._cli_session_id,
                            "total_turns": new_turns,
                            "total_tokens": {
                                "input": session.total_tokens.get("input", 0) + usage.get("input_tokens", 0),
                                "output": session.total_tokens.get("output", 0) + usage.get("output_tokens", 0),
                            },
                            "total_cost_usd": session.total_cost_usd + (message.total_cost_usd or 0.0),
                            "completed_at": datetime.now(timezone.utc),
                            "interrupt_requested": False,
                            "error": (message.result or "")[:500] if message.is_error else None,
                        },
                        only_if_status="running",
                    )

                # Process next queued message (from DB queue)
                next_msg = await self._repo.pop_queued_message(self.issue_id, session_id)
                if next_msg:
                    logger.info("Auto-processing queued message for session %s", session_id)
                    await self._message_queue.put((session_id, next_msg.prompt))
                    user_msg = CodeMessage(
                        turn=turn_offset + turn + 1, role="user", content=next_msg.prompt,
                    )
                    await self._repo.add_code_message(self.issue_id, session_id, user_msg)
                    await self._repo.update_code_session(self.issue_id, session_id, {"status": "running"})

            elif isinstance(message, StreamEvent):
                pass  # Fine-grained events — handled by incremental saves above

    # ── Phase 4: Interrupt watcher ──

    async def _watch_interrupt(self, default_session_id: str) -> None:
        """Watch MongoDB for interrupt_requested flag via polling.

        Polls every 2 seconds — much simpler than a dedicated change stream
        and sufficient for interrupt responsiveness.
        """
        try:
            while self._active:
                await asyncio.sleep(2)
                session = await self._repo.get_code_session(self.issue_id, default_session_id)
                if session and session.interrupt_requested:
                    logger.info("Interrupt detected for session %s — interrupting client", default_session_id)
                    await self._repo.clear_interrupt(self.issue_id, default_session_id)
                    if self._client:
                        await self._client.interrupt()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Interrupt watcher error: %s", e)

    # ── Helpers ──

    @staticmethod
    async def _safe_receive(client: ClaudeSDKClient):
        """Wrap receive_response to skip unknown message types."""
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
        session = await self._repo.get_code_session(self.issue_id, session_id)
        return session.total_turns if session else 0


class SessionRegistry:
    """Process-level registry of active IssueSessionManagers."""

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
        mgr = IssueSessionManager(
            issue_id=issue_id,
            workspace_root=workspace_root,
            repo=repo,
            task_queue=task_queue,
            max_turns=max_turns,
        )
        self._sessions[issue_id] = mgr
        return mgr

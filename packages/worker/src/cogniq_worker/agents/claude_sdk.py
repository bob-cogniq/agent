"""Claude Code SDK runner — uses ClaudeSDKClient instead of CLI subprocess.

Provides the same ClaudeCodeResult interface as ClaudeCodeRunner so existing
code in BuildAgent and task_runner can use either implementation.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from claude_code_sdk import query, ClaudeCodeOptions, ClaudeSDKClient
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

from cogniq_worker.agents.base import CostTracker

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeResult:
    success: bool = False
    turns_used: int = 0
    output: str = ""
    error: str = ""
    files_changed: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    model: str = ""
    cli_session_id: str = ""  # Claude CLI session UUID for --resume


class ClaudeSDKRunner:
    """Claude Code SDK-based runner.

    Drop-in replacement for ClaudeCodeRunner. Uses `claude_code_sdk.query()`
    for one-shot runs (no subprocess, no parsing hacks).
    """

    def __init__(self, cost_tracker: CostTracker, max_turns: int = 50):
        self._cost_tracker = cost_tracker
        self._max_turns = max_turns

    def _base_options(
        self,
        workspace_root: Path,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        resume: str | None = None,
    ) -> ClaudeCodeOptions:
        opts = ClaudeCodeOptions(
            permission_mode="bypassPermissions",
            max_turns=max_turns or self._max_turns,
            cwd=str(workspace_root),
        )
        if allowed_tools:
            opts.allowed_tools = allowed_tools
        if resume:
            opts.resume = resume
        return opts

    async def run(
        self,
        workspace_root: Path,
        worktree_paths: dict[str, Path] | None = None,
        prompt: str = "",
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
    ) -> ClaudeCodeResult:
        """Execute a new Claude Code session via SDK."""
        opts = self._base_options(workspace_root, max_turns, allowed_tools)
        logger.info("Running Claude Code SDK in %s (max_turns=%d)", workspace_root, opts.max_turns or self._max_turns)
        return await self._execute(prompt, opts)

    async def resume(
        self,
        workspace_root: Path,
        cli_session_id: str,
        prompt: str,
        max_turns: int | None = None,
    ) -> ClaudeCodeResult:
        """Resume an existing session via SDK (--resume equivalent)."""
        opts = self._base_options(workspace_root, max_turns, resume=cli_session_id)
        logger.info("Resuming Claude Code SDK session %s in %s", cli_session_id, workspace_root)
        return await self._execute(prompt, opts)

    async def run_streaming(
        self,
        workspace_root: Path,
        prompt: str,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        on_event: Callable[[Any], Awaitable[None]] | None = None,
        resume: str | None = None,
    ) -> ClaudeCodeResult:
        """Run with a per-message callback for real-time streaming."""
        opts = self._base_options(workspace_root, max_turns, allowed_tools, resume)
        result = ClaudeCodeResult()
        turn = 0

        async for message in self._safe_query(prompt, opts):
            if on_event:
                await on_event(message)
            turn = self._process_message(message, result, turn)

        return result

    async def _execute(self, prompt: str, opts: ClaudeCodeOptions) -> ClaudeCodeResult:
        result = ClaudeCodeResult()
        turn = 0
        try:
            async for message in self._safe_query(prompt, opts):
                turn = self._process_message(message, result, turn)
        except Exception as e:
            logger.error("Claude Code SDK error: %s", e, exc_info=True)
            result.success = False
            result.error = str(e)

        # If stream ended without ResultMessage (e.g., rate_limit_event
        # closes the stream mid-tool-execution), mark as completed with
        # whatever we have. The cli_session_id is preserved so the user
        # can continue the conversation.
        if result.turns_used == 0 and result.cli_session_id:
            logger.warning(
                "Stream ended without ResultMessage (session=%s). "
                "Partial results saved. User can continue to resume.",
                result.cli_session_id,
            )
            result.success = True  # Partial but usable
            result.turns_used = turn

        return result

    @staticmethod
    async def _safe_query(prompt: str, opts: ClaudeCodeOptions):
        """Wrap query() to skip unknown message types (e.g. rate_limit_event)."""
        it = query(prompt=prompt, options=opts).__aiter__()
        while True:
            try:
                message = await it.__anext__()
                yield message
            except StopAsyncIteration:
                break
            except MessageParseError as e:
                logger.warning("Skipping unknown message type: %s", e)
                continue

    def _process_message(self, message: Any, result: ClaudeCodeResult, turn: int) -> int:
        """Update result from a single SDK message. Returns updated turn counter."""
        # Log all messages for debugging
        import json as _json
        try:
            if isinstance(message, StreamEvent):
                event = message.event or {}
                etype = event.get("type", "")
                if etype not in ("content_block_delta", "content_block_start", "content_block_stop", "message_start", "message_delta", "message_stop", "ping"):
                    logger.info("SDK StreamEvent: type=%s data=%s", etype, _json.dumps(event, default=str, ensure_ascii=False)[:200])
            else:
                logger.info("SDK msg: %s content=%s", type(message).__name__, str(getattr(message, 'content', ''))[:200])
        except Exception:
            pass

        if isinstance(message, AssistantMessage):
            turn += 1
            for block in message.content:
                if isinstance(block, TextBlock):
                    result.messages.append({
                        "turn": turn,
                        "role": "assistant",
                        "content": block.text,
                    })
                elif isinstance(block, ToolUseBlock):
                    result.messages.append({
                        "turn": turn,
                        "role": "tool_use",
                        "content": block.input,
                        "tool_name": block.name,
                        "tool_input": block.input,
                    })
                    # Track files written/edited
                    if block.name in ("Write", "Edit"):
                        path = block.input.get("file_path", "")
                        if path and path not in result.files_changed:
                            result.files_changed.append(path)

        elif isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        result.messages.append({
                            "turn": turn,
                            "role": "tool_result",
                            "content": block.content,
                            "tool_name": block.tool_use_id,
                        })
            else:
                result.messages.append({
                    "turn": turn,
                    "role": "user",
                    "content": str(content),
                })

        elif isinstance(message, ResultMessage):
            result.success = not message.is_error
            result.turns_used = message.num_turns
            result.cli_session_id = message.session_id
            result.total_cost_usd = message.total_cost_usd or 0.0
            if message.result:
                result.output = message.result
            if message.is_error:
                result.error = message.result or "Unknown error"
            usage = message.usage or {}
            result.total_input_tokens = usage.get("input_tokens", 0)
            result.total_output_tokens = usage.get("output_tokens", 0)
            self._cost_tracker.add(
                result.total_input_tokens,
                result.total_output_tokens,
                "claude-sonnet-4-5",
            )

        elif isinstance(message, StreamEvent):
            # Extract session_id from stream events (available before ResultMessage)
            event = message.event or {}
            if not result.cli_session_id and message.session_id:
                result.cli_session_id = message.session_id

        return turn

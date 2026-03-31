import asyncio
import logging
import json
from dataclasses import dataclass, field
from pathlib import Path

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
    cli_session_id: str = ""  # Claude CLI's session UUID for --resume


class ClaudeCodeRunner:
    """Wrapper for Claude Code CLI (subprocess execution)."""

    def __init__(self, cost_tracker: CostTracker, max_turns: int = 50, on_message=None):
        self._cost_tracker = cost_tracker
        self._max_turns = max_turns
        self._on_message = on_message  # async callback(data: dict, result: ClaudeCodeResult)

    async def run(
        self,
        workspace_root: Path,
        worktree_paths: dict[str, Path] | None = None,
        prompt: str = "",
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
    ) -> ClaudeCodeResult:
        """Execute a new Claude Code session."""
        max_turns = max_turns or self._max_turns

        cmd = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
        ]

        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])

        cmd.append(prompt)

        logger.info("Running Claude Code in %s (max_turns=%d)", workspace_root, max_turns)
        return await self._execute(cmd, workspace_root)

    async def resume(
        self,
        workspace_root: Path,
        cli_session_id: str,
        prompt: str,
        max_turns: int | None = None,
    ) -> ClaudeCodeResult:
        """Resume an existing Claude Code session with a follow-up prompt."""
        max_turns = max_turns or self._max_turns

        cmd = [
            "claude",
            "--resume", cli_session_id,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
        ]

        logger.info("Resuming Claude Code session %s in %s", cli_session_id, workspace_root)
        return await self._execute(cmd, workspace_root)

    async def _execute(self, cmd: list[str], cwd: Path) -> ClaudeCodeResult:
        """Execute a Claude Code CLI command and parse the output.

        Uses line-by-line streaming instead of communicate() so that
        on_message callbacks can save messages to DB in real-time.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            result = ClaudeCodeResult()
            turn = 0

            # Stream stdout line by line
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    turn = self._process_line(data, result, turn)
                    # Real-time callback for incremental DB saves
                    if self._on_message:
                        await self._on_message(data, result)
                except json.JSONDecodeError:
                    result.output += line_str + "\n"

            # Wait for process to finish and capture stderr
            await process.wait()
            if process.returncode != 0:
                stderr_data = await process.stderr.read()
                error = stderr_data.decode("utf-8", errors="replace")
                logger.error("Claude Code failed (rc=%d): %s", process.returncode, error[:500])
                if not result.cli_session_id:
                    return ClaudeCodeResult(success=False, error=error[:2000])

            return result

        except FileNotFoundError:
            error_msg = "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            logger.error(error_msg)
            return ClaudeCodeResult(success=False, error=error_msg)
        except Exception as e:
            logger.error("Claude Code execution error: %s", e, exc_info=True)
            return ClaudeCodeResult(success=False, error=str(e))

    def _process_line(self, data: dict, result: ClaudeCodeResult, turn: int) -> int:
        """Process a single JSON line from stream-json output. Returns updated turn."""
        msg_type = data.get("type", "")

        if msg_type == "result":
            result.success = not data.get("is_error", False)
            result.output = data.get("result", "")
            result.model = data.get("model", "")
            result.cli_session_id = data.get("session_id", "")
            usage = data.get("usage", {})
            if usage:
                result.total_input_tokens = usage.get("input_tokens", 0)
                result.total_output_tokens = usage.get("output_tokens", 0)
                self._cost_tracker.add(
                    result.total_input_tokens,
                    result.total_output_tokens,
                    result.model,
                )
            result.total_cost_usd = data.get("total_cost_usd", 0.0)
            result.turns_used = data.get("num_turns", 0)

        elif msg_type == "assistant":
            turn += 1
            content = data.get("message", {}).get("content", [])

            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    result.messages.append({
                        "turn": turn,
                        "role": "tool_use",
                        "content": block.get("input", {}),
                        "tool_name": block.get("name", ""),
                        "tool_input": block.get("input", {}),
                    })
                    if block.get("name") in ("Write", "Edit"):
                        path = block.get("input", {}).get("file_path", "")
                        if path and path not in result.files_changed:
                            result.files_changed.append(path)

            text_parts = []
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                result.messages.append({
                    "turn": turn,
                    "role": "assistant",
                    "content": "\n".join(text_parts),
                })

        elif msg_type == "user":
            content = data.get("message", {}).get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result.messages.append({
                            "turn": turn,
                            "role": "tool_result",
                            "content": block.get("content", ""),
                            "tool_name": block.get("tool_use_id", ""),
                        })
            else:
                result.messages.append({
                    "turn": turn,
                    "role": "user",
                    "content": str(content),
                })

        return turn

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


class ClaudeCodeRunner:
    """Wrapper for Claude Code CLI (subprocess execution)."""

    def __init__(self, cost_tracker: CostTracker, max_turns: int = 50):
        self._cost_tracker = cost_tracker
        self._max_turns = max_turns

    async def run(
        self,
        workspace_root: Path,
        worktree_paths: dict[str, Path] | None = None,
        prompt: str = "",
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
    ) -> ClaudeCodeResult:
        """Execute Claude Code CLI with cwd set to workspace root.

        Args:
            workspace_root: The project workspace root directory (CLI cwd).
                            This allows Claude Code to access all repos.
            worktree_paths: Optional map of repo_id → worktree path for reference.
            prompt: The prompt to send to Claude Code.
            max_turns: Max conversation turns.
            allowed_tools: Restrict which tools Claude Code can use.
        """
        max_turns = max_turns or self._max_turns

        cmd = [
            "claude",
            "--print",
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
        ]

        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])

        # Prompt is passed as positional argument (last)
        cmd.append(prompt)

        logger.info("Running Claude Code in %s (max_turns=%d)", workspace_root, max_turns)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.error("Claude Code failed (rc=%d): %s", process.returncode, error[:500])
                return ClaudeCodeResult(success=False, error=error[:2000], output=output)

            # Parse JSON output for cost tracking
            result = self._parse_output(output)
            return result

        except FileNotFoundError:
            error_msg = "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            logger.error(error_msg)
            return ClaudeCodeResult(success=False, error=error_msg)
        except Exception as e:
            logger.error("Claude Code execution error: %s", e, exc_info=True)
            return ClaudeCodeResult(success=False, error=str(e))

    def _parse_output(self, raw_output: str) -> ClaudeCodeResult:
        """Parse Claude Code JSON output."""
        result = ClaudeCodeResult()

        try:
            # Claude Code outputs JSON lines
            lines = raw_output.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    msg_type = data.get("type", "")

                    if msg_type == "result":
                        result.success = True
                        result.output = data.get("result", "")
                        # Track cost from usage stats
                        usage = data.get("usage", {})
                        if usage:
                            self._cost_tracker.add(
                                usage.get("input_tokens", 0),
                                usage.get("output_tokens", 0),
                                data.get("model", ""),
                            )
                        result.turns_used = data.get("turns_used", 0)

                    elif msg_type == "tool_use" and data.get("tool") == "write":
                        path = data.get("input", {}).get("file_path", "")
                        if path:
                            result.files_changed.append(path)

                except json.JSONDecodeError:
                    # Non-JSON line, accumulate as output
                    result.output += line + "\n"

        except Exception as e:
            logger.warning("Failed to parse Claude Code output: %s", e)
            result.output = raw_output

        return result

"""Claude Code session models for tracking code generation conversations."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex[:12]


class CodeMessage(BaseModel):
    """A single message in a Claude Code session."""

    message_id: str = Field(default_factory=_new_id)
    turn: int = 0
    role: str  # "user" | "assistant" | "tool_use" | "tool_result"
    content: Any = ""  # str for user/tool_result, list[dict] for assistant
    tool_name: str | None = None  # e.g. "Write", "Bash", "Read"
    tool_input: dict[str, Any] | None = None  # tool_use input params
    tokens: dict[str, int] = Field(default_factory=lambda: {"input": 0, "output": 0})
    timestamp: datetime = Field(default_factory=_utcnow)


class ChangedFile(BaseModel):
    """A file changed during a Claude Code session."""

    path: str
    status: str = "modified"  # "added" | "modified" | "deleted"
    additions: int = 0
    deletions: int = 0
    diff_content: str | None = None  # unified diff text (truncated to 50KB)


class QueuedMessage(BaseModel):
    """A user message queued while the session is already running."""

    message_id: str = Field(default_factory=_new_id)
    prompt: str
    queued_at: datetime = Field(default_factory=_utcnow)


class CodeSession(BaseModel):
    """A complete Claude Code CLI execution session."""

    session_id: str = Field(default_factory=_new_id)
    cli_session_id: str | None = None  # Claude CLI's UUID for --resume
    run_id: str | None = None
    status: str = "running"  # "running" | "completed" | "failed"
    model: str = "sonnet"
    messages: list[CodeMessage] = Field(default_factory=list)
    message_queue: list[QueuedMessage] = Field(default_factory=list)  # queued user prompts
    total_turns: int = 0
    total_tokens: dict[str, int] = Field(default_factory=lambda: {"input": 0, "output": 0})
    total_cost_usd: float = 0.0
    changed_files: list[ChangedFile] = Field(default_factory=list)
    file_tree: list[dict[str, Any]] = Field(default_factory=list)  # [{path, repo, changed}]
    error: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None

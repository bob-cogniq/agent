"""CodeChat — standalone code conversation, independent of issues.

Unlike CodeSession (embedded in Issue documents for build tracking),
CodeChat lives in its own `code_chats` collection and supports free-form
conversations with Claude Code via the project workspace.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from cogniq_shared.domain.code_session import CodeMessage, QueuedMessage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex[:12]


class CodeChat(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    project_id: str
    title: str = ""  # Auto-generated from first message
    status: str = "idle"  # "idle" | "running" | "completed" | "failed"
    cli_session_id: str | None = None
    model: str = "sonnet"
    messages: list[CodeMessage] = Field(default_factory=list)
    message_queue: list[QueuedMessage] = Field(default_factory=list)
    total_turns: int = 0
    total_tokens: dict[str, int] = Field(default_factory=lambda: {"input": 0, "output": 0})
    total_cost_usd: float = 0.0
    interrupt_requested: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex[:12]


class User(BaseModel):
    id: str = Field(default_factory=_new_id, alias="_id")
    name: str | None = None
    email: str
    image: str | None = None
    locale: str = "ko"
    created_at: datetime = Field(default_factory=_utcnow)

    model_config = {"populate_by_name": True}


class UserInDB(User):
    hashed_password: str = ""

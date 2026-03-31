"""Repository for CodeChat documents (code_chats collection)."""

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from cogniq_shared.domain.code_chat import CodeChat
from cogniq_shared.domain.code_session import CodeMessage, QueuedMessage


class CodeChatRepository:
    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["code_chats"]

    # ── CRUD ──

    async def create(self, chat: CodeChat) -> str:
        await self._col.insert_one(chat.model_dump(by_alias=True))
        return chat.id

    async def get(self, chat_id: str) -> CodeChat | None:
        doc = await self._col.find_one({"_id": chat_id})
        return CodeChat.model_validate(doc) if doc else None

    async def list_by_project(self, project_id: str, limit: int = 50) -> list[CodeChat]:
        cursor = self._col.find(
            {"project_id": project_id},
            {"messages": {"$slice": -1}, "message_queue": 0},  # Only last message for preview
        ).sort("updated_at", -1).limit(limit)
        return [CodeChat.model_validate(doc) async for doc in cursor]

    async def delete(self, chat_id: str) -> bool:
        result = await self._col.delete_one({"_id": chat_id})
        return result.deleted_count > 0

    # ── Status updates ──

    async def update(self, chat_id: str, updates: dict[str, Any], *, only_if_status: str | None = None) -> bool:
        updates["updated_at"] = datetime.now(timezone.utc)
        if only_if_status:
            query = {"_id": chat_id, "status": only_if_status}
        else:
            query = {"_id": chat_id}
        result = await self._col.update_one(query, {"$set": updates})
        return result.modified_count > 0

    # ── Messages ──

    async def add_message(self, chat_id: str, message: CodeMessage) -> None:
        await self._col.update_one(
            {"_id": chat_id},
            {
                "$push": {"messages": message.model_dump()},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    # ── Message queue ──

    async def enqueue_message(self, chat_id: str, message: QueuedMessage) -> None:
        await self._col.update_one(
            {"_id": chat_id},
            {
                "$push": {"message_queue": message.model_dump()},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )

    async def pop_queued_message(self, chat_id: str) -> QueuedMessage | None:
        doc = await self._col.find_one({"_id": chat_id}, {"message_queue": 1})
        if not doc:
            return None
        queue = doc.get("message_queue", [])
        if not queue:
            return None
        msg = queue[0]
        await self._col.update_one(
            {"_id": chat_id},
            {
                "$pull": {"message_queue": {"message_id": msg["message_id"]}},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return QueuedMessage.model_validate(msg)

    # ── Interrupt ──

    async def request_interrupt(self, chat_id: str) -> bool:
        result = await self._col.update_one(
            {"_id": chat_id, "status": "running"},
            {"$set": {"interrupt_requested": True, "updated_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count > 0

    async def clear_interrupt(self, chat_id: str) -> None:
        await self._col.update_one(
            {"_id": chat_id},
            {"$set": {"interrupt_requested": False}},
        )

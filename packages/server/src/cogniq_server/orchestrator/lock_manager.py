import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_HOURS = 24


class FileLockManager:
    """File-level locking to prevent concurrent modification of the same files.

    Uses a MongoDB 'locks' collection instead of a TOML file for atomicity.
    """

    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["locks"]

    async def acquire(self, files: list[str], issue_id: str, branch: str) -> list[dict[str, Any]]:
        """Attempt to lock files. Returns list of conflicts (empty if all acquired)."""
        conflicts = []
        now = datetime.now(timezone.utc)

        for file_path in files:
            existing = await self._col.find_one({"file": file_path})
            if existing:
                locked_at = existing.get("locked_at", now)
                hours_held = (now - locked_at).total_seconds() / 3600

                if hours_held > LOCK_TIMEOUT_HOURS:
                    # Stale lock — release and re-acquire
                    logger.warning("Stale lock released: %s (held by %s)", file_path, existing.get("issue_id"))
                    await self._col.delete_one({"_id": existing["_id"]})
                elif existing.get("issue_id") != issue_id:
                    conflicts.append({
                        "file": file_path,
                        "held_by": existing.get("issue_id", ""),
                        "branch": existing.get("branch", ""),
                    })
                    continue

            # Upsert lock
            await self._col.update_one(
                {"file": file_path},
                {
                    "$set": {
                        "file": file_path,
                        "issue_id": issue_id,
                        "branch": branch,
                        "locked_at": now,
                    }
                },
                upsert=True,
            )

        return conflicts

    async def release(self, issue_id: str) -> int:
        """Release all locks held by an issue."""
        result = await self._col.delete_many({"issue_id": issue_id})
        if result.deleted_count:
            logger.info("Released %d locks for issue %s", result.deleted_count, issue_id)
        return result.deleted_count

    async def get_conflicts(self, files: list[str], issue_id: str) -> list[dict[str, Any]]:
        """Check for conflicts without acquiring locks."""
        conflicts = []
        for file_path in files:
            existing = await self._col.find_one({"file": file_path, "issue_id": {"$ne": issue_id}})
            if existing:
                conflicts.append({
                    "file": file_path,
                    "held_by": existing.get("issue_id", ""),
                    "branch": existing.get("branch", ""),
                })
        return conflicts


class RunLockManager:
    """Prevents duplicate execution of the same issue+stage."""

    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["run_locks"]

    async def acquire(self, issue_id: str, stage: str) -> bool:
        """Returns True if lock acquired, False if already locked."""
        try:
            await self._col.insert_one({
                "_id": f"{issue_id}:{stage}",
                "issue_id": issue_id,
                "stage": stage,
                "locked_at": datetime.now(timezone.utc),
            })
            return True
        except Exception:
            # Duplicate key — already locked
            return False

    async def release(self, issue_id: str, stage: str) -> None:
        await self._col.delete_one({"_id": f"{issue_id}:{stage}"})

    async def is_locked(self, issue_id: str, stage: str) -> bool:
        doc = await self._col.find_one({"_id": f"{issue_id}:{stage}"})
        return doc is not None

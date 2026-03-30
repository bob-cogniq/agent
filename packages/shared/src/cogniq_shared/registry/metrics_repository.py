import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class MetricsRepository:
    """CRUD for the metrics collection (Reflect agent output)."""

    def __init__(self, db: AsyncIOMotorDatabase):  # type: ignore[type-arg]
        self._col = db["metrics"]

    async def save(self, period_id: str, data: dict[str, Any]) -> None:
        """Upsert a metrics document for a time period."""
        await self._col.update_one(
            {"_id": period_id},
            {"$set": {**data, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        logger.info("Metrics saved: %s", period_id)

    async def get(self, period_id: str) -> dict[str, Any] | None:
        doc = await self._col.find_one({"_id": period_id})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc

    async def get_recent(self, limit: int = 8) -> list[dict[str, Any]]:
        cursor = self._col.find().sort("period_start", -1).limit(limit)
        results = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            results.append(doc)
        return results

    async def get_trend(self, weeks: int = 4) -> dict[str, Any]:
        """Get trend data from recent metrics."""
        recent = await self.get_recent(weeks)
        if not recent:
            return {"weeks_tracked": 0}

        recent.reverse()  # Oldest first for trend arrays

        return {
            "weeks_tracked": len(recent),
            "success_rate": [m.get("metrics", {}).get("success_rate", 0) for m in recent],
            "first_pass_rate": [m.get("verification", {}).get("first_pass_rate", 0) for m in recent],
            "avg_cost_usd": [m.get("metrics", {}).get("avg_cost_usd", 0) for m in recent],
            "avg_duration_seconds": [m.get("metrics", {}).get("avg_duration_seconds", 0) for m in recent],
        }

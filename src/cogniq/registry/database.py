import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]
_db: AsyncIOMotorDatabase | None = None  # type: ignore[type-arg]


async def connect(uri: str, db_name: str) -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    global _client, _db
    logger.info("Connecting to MongoDB: %s", uri.split("@")[-1] if "@" in uri else uri)
    _client = AsyncIOMotorClient(uri)
    _db = _client[db_name]
    await _ensure_indexes(_db)
    logger.info("MongoDB connected: %s", db_name)
    return _db


async def disconnect() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB disconnected")


def get_database() -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:  # type: ignore[type-arg]
    issues = db["issues"]
    await issues.create_index("status")
    await issues.create_index([("project_id", 1), ("status", 1)])
    await issues.create_index([("team_key", 1), ("status", 1)])
    await issues.create_index([("updated_at", -1)])
    await issues.create_index("artifacts.type")
    await issues.create_index([("runs.stage", 1), ("runs.status", 1)])
    await issues.create_index([("events.type", 1), ("events.occurred_at", -1)])

    users = db["users"]
    await users.create_index("email", unique=True)

    teams = db["teams"]
    await teams.create_index("slug", unique=True)
    await teams.create_index("members.user_id")

    projects = db["projects"]
    await projects.create_index([("team_id", 1)])
    await projects.create_index([("team_id", 1), ("slug", 1)], unique=True)

    documents = db["documents"]
    await documents.create_index([("project_id", 1), ("type", 1)])
    await documents.create_index("issue_id")

    task_queue = db["task_queue"]
    await task_queue.create_index([("status", 1), ("priority", 1), ("created_at", 1)])
    await task_queue.create_index("project_id")
    await task_queue.create_index("worker_id")
    await task_queue.create_index([("status", 1), ("heartbeat_at", 1)])

    metrics = db["metrics"]
    await metrics.create_index([("period_start", -1)])

    logger.info("MongoDB indexes ensured")

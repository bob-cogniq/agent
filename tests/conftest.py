import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from cogniq_shared.config import Settings


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings():
    return Settings(
        mongodb_uri="mongodb://localhost:27017/cogniq_test?replicaSet=rs0",
        mongodb_db_name="cogniq_test",
        jwt_secret="test-secret",
        invite_codes="TESTCODE",
        anthropic_api_key="test-key",
    )


@pytest.fixture
def mock_db():
    """Mock MongoDB database for unit tests."""
    db = MagicMock()
    for collection_name in ("issues", "users", "projects", "documents", "metrics", "locks", "run_locks"):
        col = AsyncMock()
        col.find = MagicMock(return_value=AsyncMock())
        col.find_one = AsyncMock(return_value=None)
        col.insert_one = AsyncMock()
        col.update_one = AsyncMock()
        col.find_one_and_update = AsyncMock(return_value=None)
        col.delete_one = AsyncMock()
        col.delete_many = AsyncMock()
        col.count_documents = AsyncMock(return_value=0)
        col.create_index = AsyncMock()
        db.__getitem__ = MagicMock(side_effect=lambda name: getattr(db, f"_{name}", col))
        setattr(db, f"_{collection_name}", col)
    return db

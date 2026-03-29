"""Agent Worker process — polls task_queue, runs agents.

Usage:
    python -m cogniq.worker.main
"""

import asyncio
import logging
import signal
from uuid import uuid4

from cogniq.config import settings
from cogniq.registry.database import connect, disconnect
from cogniq.registry.repository import IssueRepository
from cogniq.taskqueue.models import TaskResult
from cogniq.taskqueue.repository import TaskQueueRepository
from cogniq.worker.heartbeat import heartbeat_loop
from cogniq.worker.task_runner import run_agent

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    worker_id = settings.worker_id or f"worker-{uuid4().hex[:8]}"
    poll_interval = settings.worker_poll_interval_seconds
    heartbeat_interval = settings.worker_heartbeat_interval_seconds

    logger.info("Worker starting: id=%s poll=%.1fs heartbeat=%.1fs", worker_id, poll_interval, heartbeat_interval)

    db = await connect(settings.mongodb_uri, settings.mongodb_db_name)
    repo = IssueRepository(db)
    task_queue = TaskQueueRepository(db)

    running = True

    def shutdown_handler(*_: object) -> None:
        nonlocal running
        running = False
        logger.info("Shutdown signal received")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    logger.info("Worker ready, polling for tasks...")

    while running:
        task = await task_queue.claim(worker_id)

        if not task:
            await asyncio.sleep(poll_interval)
            continue

        logger.info("Claimed task %s: issue=%s stage=%s", task.id, task.issue_id, task.stage)

        # Start heartbeat background
        hb = asyncio.create_task(heartbeat_loop(task_queue, task.id, heartbeat_interval))

        try:
            await task_queue.start(task.id)
            result = await run_agent(task, repo)
            await task_queue.complete(task.id, result)
            logger.info("Task %s completed: %s", task.id, result.status)
        except Exception as e:
            logger.error("Task %s failed: %s", task.id, e, exc_info=True)
            await task_queue.fail(task.id, str(e)[:500])
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

    await disconnect()
    logger.info("Worker stopped: %s", worker_id)


if __name__ == "__main__":
    asyncio.run(main())

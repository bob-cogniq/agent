import asyncio
import logging

from cogniq_shared.taskqueue.repository import TaskQueueRepository

logger = logging.getLogger(__name__)


async def heartbeat_loop(
    task_queue: TaskQueueRepository,
    task_id: str,
    interval: float = 30.0,
) -> None:
    """Background coroutine that updates heartbeat_at periodically."""
    try:
        while True:
            await asyncio.sleep(interval)
            await task_queue.heartbeat(task_id)
            logger.debug("Heartbeat: %s", task_id)
    except asyncio.CancelledError:
        pass

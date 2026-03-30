import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class RetryPolicy:
    """Exponential backoff retry for transient failures."""

    def __init__(self, max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 60.0):
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def execute(
        self,
        fn: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt == self.max_attempts:
                    break
                delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                logger.warning(
                    "Retry %d/%d after %.1fs: %s",
                    attempt, self.max_attempts, delay, e,
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

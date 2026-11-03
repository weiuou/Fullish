"""Generation slots and cooldown, independent of conversation content."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fullish.telemetry import emit


@dataclass
class GroupActivity:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_attempt: float = float("-inf")


class GenerationCoordinator:
    def __init__(self, *, min_interval: float = 5, capacity: int = 4):
        self.min_interval = min_interval
        self.capacity = asyncio.Semaphore(capacity)
        self.active = 0

    @asynccontextmanager
    async def slot(self, activity: GroupActivity) -> AsyncIterator[None]:
        if activity.lock.locked():
            emit("conversation.waiting", reason="group_busy")
        async with activity.lock:
            delay = self.min_interval - (time.monotonic() - activity.last_attempt)
            if delay > 0:
                emit("conversation.waiting", reason="cooldown", delay_ms=round(delay * 1000))
                await asyncio.sleep(delay)
            if self.capacity.locked():
                emit("conversation.waiting", reason="global_capacity")
            await self.capacity.acquire()
            self.active += 1
            activity.last_attempt = time.monotonic()
            try:
                yield
            finally:
                self.active -= 1
                self.capacity.release()

"""Per-group FIFO consumers covering generation, sending and send acknowledgement."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from fullish.telemetry import elapsed_ms, emit, message_trace

_active_group: ContextVar[tuple[object, str] | None] = ContextVar("active_group_job", default=None)


@dataclass
class Job:
    group_id: str
    message_id: str
    run: Callable[[], Awaitable[None]]
    created: float = field(default_factory=time.monotonic)


@dataclass
class Consumer:
    pending: asyncio.Queue[Job]
    task: asyncio.Task[None] | None = None


class GroupMessageQueue:
    def __init__(self, *, capacity: int = 100):
        self.capacity = capacity
        self.groups: dict[str, Consumer] = {}
        self.closed = False
        self.submitters: set[asyncio.Task] = set()
        self.waiters: set[asyncio.Future] = set()

    async def submit(
        self, group_id: str, message_id: str, run: Callable[[], Awaitable[None]]
    ) -> None:
        task = asyncio.current_task()
        if task:
            self.submitters.add(task)
        try:
            await self._submit(group_id, message_id, run)
        finally:
            if task:
                self.submitters.discard(task)

    async def execute[T](
        self, group_id: str, message_id: str, run: Callable[[], Awaitable[T]]
    ) -> T:
        """Wait for a queued operation; cancellation never leaves a pending notification behind."""
        if _active_group.get() == (self, group_id):
            raise RuntimeError("Cannot wait on the current group queue from its own job")
        if self.closed:
            raise RuntimeError("Group coordinator is closed")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self.waiters.add(future)
        running: asyncio.Task[T] | None = None

        async def complete() -> None:
            nonlocal running
            if future.cancelled():
                return

            async def invoke() -> T:
                return await run()

            running = asyncio.create_task(invoke())
            try:
                value = await running
            except asyncio.CancelledError:
                future.cancel()
                if (consumer := asyncio.current_task()) and consumer.cancelling():
                    raise
            except Exception as error:
                if not future.done():
                    future.set_exception(error)
                raise
            else:
                if not future.done():
                    future.set_result(value)

        try:
            await self.submit(group_id, message_id, complete)
            return await future
        except asyncio.CancelledError:
            future.cancel()
            if running:
                running.cancel()
            raise
        finally:
            self.waiters.discard(future)

    async def _submit(
        self, group_id: str, message_id: str, run: Callable[[], Awaitable[None]]
    ) -> None:
        with message_trace(group_id, message_id):
            if self.closed:
                emit("queue.rejected", reason="shutdown")
                return
            consumer = self.groups.setdefault(group_id, Consumer(asyncio.Queue(self.capacity)))
            if consumer.pending.full():
                emit("queue.waiting", reason="backpressure", depth=consumer.pending.qsize())
            # Apply backpressure instead of dropping an addressed message on a full queue.
            await consumer.pending.put(Job(group_id, message_id, run))
            emit("queue.enqueued", depth=consumer.pending.qsize())
            if consumer.task is None or consumer.task.done():
                consumer.task = asyncio.create_task(self._consume(consumer))

    async def _consume(self, consumer: Consumer) -> None:
        while True:
            job = await consumer.pending.get()
            with message_trace(job.group_id, job.message_id):
                emit(
                    "queue.dequeued",
                    depth=consumer.pending.qsize(),
                    wait_ms=elapsed_ms(job.created),
                )
                token = _active_group.set((self, job.group_id))
                try:
                    await job.run()
                except asyncio.CancelledError:
                    emit("queue.cancelled", reason="shutdown")
                    raise
                except Exception as error:
                    emit("queue.failed", level="WARNING", error_type=type(error).__name__)
                finally:
                    _active_group.reset(token)
                    consumer.pending.task_done()

    async def close(self, *, timeout: float = 20) -> None:
        self.closed = True
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*(asyncio.shield(task) for task in self.submitters))
                await asyncio.gather(*(group.pending.join() for group in self.groups.values()))
        except TimeoutError:
            emit("queue.drain.timeout", level="WARNING")
        finally:
            tasks = [group.task for group in self.groups.values() if group.task] + list(
                self.submitters
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for group in self.groups.values():
                while not group.pending.empty():
                    job = group.pending.get_nowait()
                    with message_trace(job.group_id, job.message_id):
                        emit("queue.cancelled", reason="shutdown_pending")
                    group.pending.task_done()
            self.groups.clear()
            for waiter in self.waiters:
                waiter.cancel()

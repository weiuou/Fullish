"""QQ event routing and platform objects; business processing lives in reply and sending."""

import asyncio
from collections.abc import Awaitable, Callable

from nonebot import get_adapter, on_message
from nonebot.adapters.qq import Adapter, Bot
from nonebot.adapters.qq.event import Event, GroupMessageCreateEvent
from nonebot.adapters.qq.models import Dispatch

from fullish.collection.qq import collect_group_message
from fullish.platforms.qq.menu import DeveloperMenu
from fullish.reply.handler import ChatHandler
from fullish.scheduling.queue import GroupMessageQueue
from fullish.sending.qq import QQTransport
from fullish.telemetry import emit, message_trace


class GroupOnlyAdapter(Adapter):
    private_handler: Callable[[Bot, Dispatch], Awaitable[None]] | None = None

    def dispatch_event(self, bot: Bot, payload: Dispatch) -> None:
        if payload.type == "C2C_MESSAGE_CREATE" and self.private_handler:
            task = asyncio.create_task(self._private(bot, payload))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        if payload.type in {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            emit("qq.event.skipped", event_type=payload.type, reason="private_message")
            return
        data = payload.data if isinstance(payload.data, dict) else {}
        with message_trace(str(data.get("group_openid", "")), str(data.get("id", ""))):
            emit("qq.event.received", event_type=payload.type, sequence=payload.sequence)
            super().dispatch_event(bot, payload)

    async def _private(self, bot: Bot, payload: Dispatch) -> None:
        try:
            if self.private_handler:
                await self.private_handler(bot, payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            emit("developer.command.failed", level="WARNING", error_type=type(error).__name__)

    @staticmethod
    def payload_to_event(payload: Dispatch) -> Event:
        # The base adapter catches parsing errors. Log a safe terminal stage first.
        try:
            event = Adapter.payload_to_event(payload)
        except Exception as error:
            emit("qq.event.failed", level="WARNING", error_type=type(error).__name__)
            raise
        emit("qq.event.dispatched", event_type=payload.type)
        return event


def install_group_handler(
    handler: ChatHandler,
    *,
    queue: GroupMessageQueue,
    developer: DeveloperMenu | None = None,
) -> None:
    if developer:

        async def private(bot: Bot, payload: Dispatch) -> None:
            # Separate FIFO keeps private menu operations ordered without waiting on LLM calls.
            await queue.submit(
                "developer-menu", payload.id or "", lambda: developer.handle(bot, payload)
            )

        get_adapter(GroupOnlyAdapter).private_handler = private

    async def is_group(event: GroupMessageCreateEvent) -> bool:
        return True

    matcher = on_message(rule=is_group, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageCreateEvent) -> None:
        await queue.submit(
            event.group_openid,
            event.id,
            lambda: handle_group(handler, bot, event),
        )


async def handle_group(handler: ChatHandler, bot: Bot, event: GroupMessageCreateEvent) -> None:
    try:
        message = collect_group_message(event)
        segment_types = [segment.type for segment in event.get_message()]
    except Exception as error:
        with message_trace(event.group_openid, event.id):
            emit(
                "qq.message.failed", level="WARNING", phase="parse", error_type=type(error).__name__
            )
            emit(
                "qq.message.completed",
                outcome="failed",
                phase="parse",
                sent_count=0,
                reply_count=0,
                partial_sent=False,
                elapsed_ms=0,
            )
        return
    await handler.handle(
        message, QQTransport(bot, message.group_id, event=event), segment_types=segment_types
    )

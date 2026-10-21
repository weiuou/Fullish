"""Assemble the whole candidate before making a single QQ send call."""

import asyncio
from collections.abc import Mapping

from nonebot.adapters.qq import Bot, Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent

from fullish.sending.adapters import ADAPTERS, SegmentAdapter
from fullish.sending.schema import Reply, outgoing


def build_message(reply: Reply, *, adapters: Mapping[str, SegmentAdapter] | None = None) -> Message:
    registry = ADAPTERS if adapters is None else adapters
    message = Message()
    for part in outgoing(reply).parts:
        if not (adapter := registry.get(part.kind)):
            raise ValueError("No sending adapter for message part")
        message.append(adapter(part))
    return message


async def send_reply(
    bot: Bot,
    event: GroupMessageCreateEvent,
    reply: Reply,
    *,
    adapters: Mapping[str, SegmentAdapter] | None = None,
) -> None:
    # Path encoders read files; keep that work off the event loop. Constructing the
    # full Message first also prevents a partial text send when an attachment fails.
    message = await asyncio.to_thread(build_message, reply, adapters=adapters)
    await bot.send(event, message)

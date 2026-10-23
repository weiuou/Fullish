"""Assemble every message in an expression plan before starting QQ delivery."""

import asyncio
from collections.abc import Mapping

from nonebot.adapters.qq import Bot, Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent

from fullish.sending.adapters import ADAPTERS, SegmentAdapter
from fullish.sending.schema import Reply, ReplyPlan, outgoing, reply_messages


def build_message(reply: Reply, *, adapters: Mapping[str, SegmentAdapter] | None = None) -> Message:
    registry = ADAPTERS if adapters is None else adapters
    message = Message()
    for part in outgoing(reply).parts:
        if not (adapter := registry.get(part.kind)):
            raise ValueError("No sending adapter for message part")
        message.append(adapter(part))
    return message


async def prepare_replies(
    reply: Reply | ReplyPlan,
    *,
    adapters: Mapping[str, SegmentAdapter] | None = None,
) -> tuple[Message, ...]:
    # Read every local attachment before sending any accompanying text. Keep the
    # complete plan's file reads and encoding off the event loop.
    def build() -> tuple[Message, ...]:
        return tuple(build_message(item, adapters=adapters) for item in reply_messages(reply))

    return await asyncio.to_thread(build)


async def send_reply(
    bot: Bot,
    event: GroupMessageCreateEvent,
    reply: Reply,
    *,
    adapters: Mapping[str, SegmentAdapter] | None = None,
) -> None:
    """Compatibility entry point for callers sending a single reply."""
    (message,) = await prepare_replies(reply, adapters=adapters)
    await bot.send(event, message)

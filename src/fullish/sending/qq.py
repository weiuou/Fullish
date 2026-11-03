"""Assemble every message in an expression plan before starting QQ delivery."""

import asyncio
from collections.abc import Mapping
from typing import Literal

import httpx
from nonebot.adapters.qq import Bot, Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.drivers import Request

from fullish.sending.adapters import ADAPTERS, SegmentAdapter
from fullish.sending.schema import Reply, ReplyPlan, outgoing, reply_messages
from fullish.sending.service import DeliveryError


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


MAX_EVENT_REPLIES = 5


def remaining_replies(event: GroupMessageCreateEvent) -> int:
    sequence = getattr(event, "_reply_seq", 0)
    return max(0, MAX_EVENT_REPLIES - (sequence if isinstance(sequence, int) else 0))


async def proactive_messages_allowed(bot: Bot, group_openid: str) -> bool:
    response = await bot._request(
        Request(
            "GET", bot.adapter.get_api_base().joinpath("v2", "groups", group_openid, "bot_state")
        )
    )
    allowed = response.get("allow_proactive_msg") if isinstance(response, dict) else None
    if not isinstance(allowed, bool):
        raise DeliveryError("invalid_group_permission_response")
    return allowed


class QQTransport:
    def __init__(self, bot: Bot, group_id: str, *, event: GroupMessageCreateEvent | None = None):
        if event is not None and event.group_openid != group_id:
            raise ValueError("Reply target must match its actual incoming event")
        self.bot = bot
        self.group_id = group_id
        self.event = event
        self.mode: Literal["reply", "proactive"] = "reply" if event is not None else "proactive"
        self.messages: tuple[Message, ...] = ()

    async def allowed(self) -> bool:
        return self.event is not None or await proactive_messages_allowed(self.bot, self.group_id)

    def remaining(self) -> int | None:
        return remaining_replies(self.event) if self.event is not None else None

    async def prepare(self, content: Reply | ReplyPlan) -> None:
        self.messages = await prepare_replies(content)

    async def send(self, index: int) -> object:
        try:
            if self.event is not None:
                return await self.bot.send(self.event, self.messages[index])
            # Reuse the same prepared message representation without inventing an inbound event.
            message = self.messages[index]
            if all(segment.type == "text" for segment in message):
                return await self.bot.post_group_messages(
                    group_openid=self.group_id, msg_type=0, content=message.extract_plain_text()
                )
            return await self.bot.send_to_group(group_openid=self.group_id, message=message)
        except ActionFailed as error:
            raise DeliveryError(
                "platform_rejected",
                ambiguous=error.status_code >= 500 or error.status_code == 408,
                status_code=error.status_code,
                platform_code=error.code if isinstance(error.code, int) else None,
                error_type=type(error).__name__,
            ) from None
        except (httpx.RequestError, TimeoutError) as error:
            raise DeliveryError(
                "delivery_unknown", ambiguous=True, error_type=type(error).__name__
            ) from None
        except Exception as error:
            raise DeliveryError(
                "delivery_unknown", ambiguous=True, error_type=type(error).__name__
            ) from None

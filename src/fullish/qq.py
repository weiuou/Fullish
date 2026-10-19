"""QQ official WebSocket transport. Private events stop before message dispatch."""

import asyncio
import time

from nonebot import on_message
from nonebot.adapters.qq import Adapter, Bot, Message
from nonebot.adapters.qq.event import Event, GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.adapters.qq.models import Dispatch

from fullish.conversation import Conversations
from fullish.model import ModelError
from fullish.telemetry import elapsed_ms, emit, message_trace


class GroupOnlyAdapter(Adapter):
    def dispatch_event(self, bot: Bot, payload: Dispatch) -> None:
        if payload.type in {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            emit("qq.event.skipped", event_type=payload.type, reason="private_message")
            return
        data = payload.data if isinstance(payload.data, dict) else {}
        with message_trace(str(data.get("group_openid", "")), str(data.get("id", ""))):
            emit("qq.event.received", event_type=payload.type, sequence=payload.sequence)
            super().dispatch_event(bot, payload)

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


def describe(message: Message) -> tuple[str, list[str]]:
    """Flatten a message into context text plus the image URLs it carries."""
    parts: list[str] = []
    images: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(segment.data["text"])
        elif segment.type == "emoji":
            parts.append("[表情]")
        elif segment.type == "mention_user":
            name = segment.data.get("username")
            parts.append(f"@{name}" if name else "@某人")
        elif segment.type == "image":
            parts.append("[图片]")
            if url := segment.data.get("url"):
                images.append(url)
    return "".join(parts).strip(), images


def install_group_handler(conversations: Conversations) -> None:
    async def is_group(event: GroupMessageCreateEvent) -> bool:
        return True

    matcher = on_message(rule=is_group, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageCreateEvent) -> None:
        await handle_group(conversations, bot, event)


async def handle_group(
    conversations: Conversations, bot: Bot, event: GroupMessageCreateEvent
) -> None:
    with message_trace(event.group_openid, event.id):
        start = time.monotonic()
        phase = "parse"
        outcome = "failed"
        try:
            text, images = describe(event.get_message())
            emit(
                "qq.message.parsed",
                text_chars=len(text),
                image_count=len(images),
                segment_types=[segment.type for segment in event.get_message()],
                addressed=event.is_tome(),
            )
            phase = "conversation"
            reply = await conversations.receive(
                event.group_openid,
                event.id,
                event.author.member_openid,
                text,
                addressed=event.is_tome(),
                images=images,
            )
            if reply:
                phase = "send"
                send_start = time.monotonic()
                emit("qq.send.started", text_chars=len(reply))
                await bot.send(event, reply)
                emit("qq.send.succeeded", elapsed_ms=elapsed_ms(send_start))
                phase = "record"
                conversations.sent(event.group_openid, reply)
                outcome = "sent"
            else:
                outcome = "no_reply"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ModelError as error:
            emit("qq.message.failed", level="WARNING", phase=phase, error_code=str(error))
        except ActionFailed as error:
            emit(
                "qq.message.failed",
                level="WARNING",
                phase=phase,
                error_type=type(error).__name__,
                status_code=error.status_code,
                error_code=error.code if isinstance(error.code, int) else None,
            )
        except Exception as error:
            # No message payloads, API response bodies or credentials in our logs.
            emit("qq.message.failed", level="WARNING", phase=phase, error_type=type(error).__name__)
        finally:
            emit("qq.message.completed", outcome=outcome, phase=phase, elapsed_ms=elapsed_ms(start))

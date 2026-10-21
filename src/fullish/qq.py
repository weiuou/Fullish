"""QQ official WebSocket transport. Private events stop before message dispatch."""

import asyncio
import time

from nonebot import on_message
from nonebot.adapters.qq import Adapter, Bot
from nonebot.adapters.qq.event import Event, GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.adapters.qq.models import Dispatch

from fullish.collection.qq import collect_group_message
from fullish.conversation import Conversations
from fullish.message_queue import GroupMessageQueue
from fullish.model import ModelError
from fullish.sending.qq import send_reply
from fullish.sending.schema import outgoing
from fullish.telemetry import elapsed_ms, emit, message_trace

GENERATION_FAILURE_REPLY = "这次没能生成回复，暂时没处理好，请稍后再试。"
IMAGE_FAILURE_REPLY = "这张图没能生成出来，这次没完成。"
IMAGE_RATE_LIMIT_REPLY = "图片服务现在限流了，这张没生成出来，稍后再试。"


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


def install_group_handler(conversations: Conversations) -> GroupMessageQueue:
    queue = GroupMessageQueue()

    async def is_group(event: GroupMessageCreateEvent) -> bool:
        return True

    matcher = on_message(rule=is_group, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageCreateEvent) -> None:
        await queue.submit(
            event.group_openid, event.id, lambda: handle_group(conversations, bot, event)
        )

    return queue


async def handle_group(
    conversations: Conversations, bot: Bot, event: GroupMessageCreateEvent
) -> None:
    with message_trace(event.group_openid, event.id):
        start = time.monotonic()
        phase = "parse"
        outcome = "failed"
        fallback = False
        try:
            message = collect_group_message(event)
            emit(
                "qq.message.parsed",
                text_chars=len(message.content.text),
                image_count=len(message.content.image_urls),
                reply_image_count=len(message.reply.content.image_urls) if message.reply else 0,
                segment_types=[segment.type for segment in event.get_message()],
                addressed=message.addressed,
            )
            phase = "conversation"
            try:
                reply = await conversations.receive_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Only acknowledge an authorized reply request. Do not turn context-only
                # events or permission failures into unsolicited group messages.
                called = message.addressed or "fullish" in message.content.text.lower()
                allowed = not conversations.allowed_groups or (
                    message.group_id in conversations.allowed_groups
                )
                if not called or not allowed:
                    raise
                fields = (
                    {"error_code": str(error)}
                    if isinstance(error, ModelError)
                    else {"error_type": type(error).__name__}
                )
                emit("qq.reply.fallback", level="WARNING", **fields)
                if isinstance(error, ModelError) and str(error).startswith("image_"):
                    reply = (
                        IMAGE_RATE_LIMIT_REPLY
                        if str(error) == "image_http_429"
                        else IMAGE_FAILURE_REPLY
                    )
                else:
                    reply = GENERATION_FAILURE_REPLY
                fallback = True
            if reply:
                phase = "send"
                send_start = time.monotonic()
                candidate = outgoing(reply)
                emit(
                    "qq.send.started",
                    text_chars=len(candidate.text),
                    image_count=sum(part.kind == "image" for part in candidate.parts),
                    audio_count=sum(part.kind == "audio" for part in candidate.parts),
                )
                await send_reply(bot, event, candidate)
                emit("qq.send.succeeded", fallback=fallback, elapsed_ms=elapsed_ms(send_start))
                phase = "record"
                conversations.sent(event.group_openid, reply)
                outcome = "fallback_sent" if fallback else "sent"
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

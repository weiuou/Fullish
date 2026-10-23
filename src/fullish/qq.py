"""QQ official WebSocket transport. Private events stop before message dispatch."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from nonebot import get_adapter, on_message
from nonebot.adapters.qq import Adapter, Bot
from nonebot.adapters.qq.event import Event, GroupMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.adapters.qq.models import Dispatch

from fullish.collection.qq import collect_group_message
from fullish.conversation import Conversations
from fullish.developer_menu import DeveloperMenu
from fullish.message_queue import GroupMessageQueue
from fullish.model import ModelError
from fullish.sending.qq import prepare_replies
from fullish.sending.schema import ReplyPlan, reply_messages
from fullish.telemetry import elapsed_ms, emit, message_trace

GENERATION_FAILURE_REPLY = "这次没能生成回复，暂时没处理好，请稍后再试。"
IMAGE_FAILURE_REPLY = "这张图没能生成出来，这次没完成。"
IMAGE_RATE_LIMIT_REPLY = "图片服务现在限流了，这张没生成出来，稍后再试。"
MAX_EVENT_REPLIES = 5


def remaining_replies(event: GroupMessageCreateEvent) -> int:
    # NoneBot increments this before each send attempt, including failed uploads.
    # Some replay/test event substitutes do not have the adapter's private field.
    sequence = getattr(event, "_reply_seq", 0)
    return max(0, MAX_EVENT_REPLIES - (sequence if isinstance(sequence, int) else 0))


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
    conversations: Conversations, *, developer: DeveloperMenu | None = None
) -> GroupMessageQueue:
    queue = GroupMessageQueue()
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
        sent_count = 0
        reply_count = 0
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
                allowed = conversations.group_allowed(message.group_id)
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
                candidates = reply_messages(reply)
                reply_count = len(candidates)
                if not conversations.reply_allowed(event.group_openid, reply):
                    emit("qq.reply.skipped", reason="group_policy_changed")
                    outcome = "no_reply"
                    return
                if reply_count > remaining_replies(event):
                    emit("qq.reply.skipped", reason="budget_exhausted", reply_count=reply_count)
                    outcome = "no_reply"
                    return
                phase = "send"
                messages = await prepare_replies(reply)
                for index, (candidate, prepared) in enumerate(
                    zip(candidates, messages, strict=True)
                ):
                    # Check the whole remainder, so a revoked media permission also
                    # prevents sending text that announces an image still to come.
                    remainder = ReplyPlan(candidates[index:])
                    if not conversations.reply_allowed(event.group_openid, remainder):
                        emit("qq.reply.skipped", reason="group_policy_changed")
                        outcome = "partial_sent" if sent_count else "no_reply"
                        return
                    if len(remainder.messages) > remaining_replies(event):
                        emit("qq.reply.skipped", reason="budget_exhausted")
                        outcome = "partial_sent" if sent_count else "no_reply"
                        return
                    phase = "send"
                    send_start = time.monotonic()
                    emit(
                        "qq.send.started",
                        text_chars=len(candidate.text),
                        image_count=sum(part.kind == "image" for part in candidate.parts),
                        audio_count=sum(part.kind == "audio" for part in candidate.parts),
                        reply_index=index + 1,
                        reply_count=reply_count,
                    )
                    await bot.send(event, prepared)
                    sent_count += 1
                    emit(
                        "qq.send.succeeded",
                        fallback=fallback,
                        reply_index=index + 1,
                        elapsed_ms=elapsed_ms(send_start),
                    )
                    phase = "record"
                    conversations.sent(event.group_openid, candidate)
                outcome = "fallback_sent" if fallback else "sent"
            else:
                outcome = "no_reply"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ModelError as error:
            outcome = "partial_sent" if sent_count else "failed"
            emit("qq.message.failed", level="WARNING", phase=phase, error_code=str(error))
        except ActionFailed as error:
            outcome = "partial_sent" if sent_count else "failed"
            emit(
                "qq.message.failed",
                level="WARNING",
                phase=phase,
                error_type=type(error).__name__,
                status_code=error.status_code,
                error_code=error.code if isinstance(error.code, int) else None,
            )
        except Exception as error:
            outcome = "partial_sent" if sent_count else "failed"
            # No message payloads, API response bodies or credentials in our logs.
            emit("qq.message.failed", level="WARNING", phase=phase, error_type=type(error).__name__)
        finally:
            emit(
                "qq.message.completed",
                outcome=outcome,
                phase=phase,
                sent_count=sent_count,
                reply_count=reply_count,
                partial_sent=0 < sent_count < reply_count,
                elapsed_ms=elapsed_ms(start),
            )

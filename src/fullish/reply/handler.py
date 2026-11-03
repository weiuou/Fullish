"""Platform-independent chat handling, including failure replies and delivery outcomes."""

import asyncio
import time

from fullish.collection import CollectedMessage
from fullish.errors import ModelError
from fullish.reply.service import ConversationService, is_called
from fullish.sending.service import (
    DeliveryRequest,
    DeliveryResult,
    DeliveryTransport,
    SendingService,
)
from fullish.telemetry import elapsed_ms, emit, message_trace

GENERATION_FAILURE_REPLY = "这次没能生成回复，暂时没处理好，请稍后再试。"
IMAGE_FAILURE_REPLY = "这张图没能生成出来，这次没完成。"
IMAGE_RATE_LIMIT_REPLY = "图片服务现在限流了，这张没生成出来，稍后再试。"


def failure_reply(error: Exception) -> str:
    if isinstance(error, ModelError) and str(error).startswith("image_"):
        return IMAGE_RATE_LIMIT_REPLY if str(error) == "image_http_429" else IMAGE_FAILURE_REPLY
    return GENERATION_FAILURE_REPLY


class ChatHandler:
    def __init__(self, conversations: ConversationService, sending: SendingService):
        self.conversations = conversations
        self.sending = sending

    async def handle(
        self, message: CollectedMessage, transport: DeliveryTransport, *, segment_types: list[str]
    ) -> None:
        with message_trace(message.group_id, message.message_id):
            start = time.monotonic()
            phase = "conversation"
            outcome = "failed"
            fallback = False
            delivery = DeliveryResult()
            try:
                emit(
                    "qq.message.parsed",
                    text_chars=len(message.content.text),
                    image_count=len(message.content.image_urls),
                    reply_image_count=len(message.reply.content.image_urls) if message.reply else 0,
                    segment_types=segment_types,
                    addressed=message.addressed,
                )
                try:
                    reply = await self.conversations.receive_message(message)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if not is_called(message) or not self.conversations.access.group_allowed(
                        message.group_id
                    ):
                        raise
                    fields = (
                        {"error_code": str(error)}
                        if isinstance(error, ModelError)
                        else {"error_type": type(error).__name__}
                    )
                    emit("qq.reply.fallback", level="WARNING", **fields)
                    reply = failure_reply(error)
                    fallback = True
                if reply:
                    phase = "send"
                    await self.sending.deliver(
                        DeliveryRequest(message.group_id, reply, fallback=fallback),
                        transport,
                        result=delivery,
                    )
                    phase = delivery.phase
                    if delivery.confirmed:
                        outcome = "fallback_sent" if fallback else "sent"
                    elif delivery.error:
                        outcome = "partial_sent" if delivery.sent_count else "failed"
                        error = delivery.error
                        emit(
                            "qq.message.failed",
                            level="WARNING",
                            phase=phase,
                            error_type=error.error_type,
                            status_code=error.status_code,
                            error_code=error.platform_code,
                        )
                    else:
                        outcome = "partial_sent" if delivery.sent_count else "no_reply"
                else:
                    outcome = "no_reply"
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception as error:
                outcome = "partial_sent" if delivery.sent_count else "failed"
                fields = (
                    {"error_code": str(error)}
                    if isinstance(error, ModelError)
                    else {"error_type": type(error).__name__}
                )
                emit("qq.message.failed", level="WARNING", phase=phase, **fields)
            finally:
                if delivery.items:
                    phase = delivery.phase
                emit(
                    "qq.message.completed",
                    outcome=outcome,
                    phase=phase,
                    sent_count=delivery.sent_count,
                    reply_count=len(delivery.items),
                    partial_sent=0 < delivery.sent_count < len(delivery.items),
                    delivery_states=[item.state for item in delivery.items],
                    delivery_reason=delivery.reason,
                    elapsed_ms=elapsed_ms(start),
                )

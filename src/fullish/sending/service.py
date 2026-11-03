"""Platform-independent plan delivery with explicit per-message acknowledgement."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from fullish.policies.access import AccessPolicy
from fullish.sending.schema import Reply, ReplyPlan, reply_messages
from fullish.telemetry import elapsed_ms, emit


@dataclass(frozen=True)
class DeliveryRequest:
    group_id: str
    content: Reply | ReplyPlan
    source: str = "chat"
    mode: Literal["reply", "proactive"] = "reply"
    fallback: bool = False


class DeliveryError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        ambiguous: bool = False,
        status_code: int | None = None,
        platform_code: int | None = None,
        error_type: str = "DeliveryError",
    ):
        super().__init__(reason)
        self.ambiguous = ambiguous
        self.status_code = status_code
        self.platform_code = platform_code
        self.error_type = error_type


class DeliveryTransport(Protocol):
    mode: Literal["reply", "proactive"]
    group_id: str

    async def allowed(self) -> bool: ...
    def remaining(self) -> int | None: ...
    async def prepare(self, content: Reply | ReplyPlan) -> None: ...
    async def send(self, index: int) -> object: ...


@dataclass
class DeliveryItem:
    state: Literal["pending", "confirmed", "failed", "unknown", "blocked"] = "pending"
    receipt: object = None


@dataclass
class DeliveryResult:
    items: list[DeliveryItem] = field(default_factory=list)
    reason: str = ""
    error: DeliveryError | None = None
    phase: str = "send"

    @property
    def sent_count(self) -> int:
        return sum(item.state == "confirmed" for item in self.items)

    @property
    def confirmed(self) -> bool:
        return bool(self.items) and all(item.state == "confirmed" for item in self.items)


class SendingService:
    def __init__(
        self, access: AccessPolicy, *, confirmed: Callable[[str, Reply, str], None] | None = None
    ):
        self.access = access
        self.record_confirmed = confirmed

    async def deliver(
        self,
        request: DeliveryRequest,
        transport: DeliveryTransport,
        *,
        result: DeliveryResult | None = None,
    ) -> DeliveryResult:
        progress = result if result is not None else DeliveryResult()
        candidates = reply_messages(request.content)
        progress.items = [DeliveryItem() for _ in candidates]
        if transport.mode != request.mode or transport.group_id != request.group_id:
            raise ValueError("Delivery target or mode does not match its transport")
        if request.source == "chat" and request.mode != "reply":
            raise ValueError("Chat replies require an actual reply transport")

        async def check(index: int) -> bool:
            remainder = ReplyPlan(candidates[index:])
            if not self.access.delivery_allowed(request.group_id, remainder, source=request.source):
                progress.reason = "group_policy_changed"
            elif not await transport.allowed():
                progress.reason = "proactive_messages_disabled"
            elif not self.access.delivery_allowed(
                request.group_id, remainder, source=request.source
            ):
                # The platform capability query can yield while local policy is revoked.
                progress.reason = "group_policy_changed"
            elif (remaining := transport.remaining()) is not None and len(
                remainder.messages
            ) > remaining:
                progress.reason = "budget_exhausted"
            else:
                return True
            for item in progress.items[index:]:
                item.state = "blocked"
            emit("qq.reply.skipped", reason=progress.reason)
            return False

        if not await check(0):
            return progress
        await transport.prepare(request.content)
        for index, candidate in enumerate(candidates):
            if not await check(index):
                return progress
            progress.phase = "send"
            started = time.monotonic()
            emit(
                "qq.send.started",
                text_chars=len(candidate.text),
                image_count=sum(part.kind == "image" for part in candidate.parts),
                audio_count=sum(part.kind == "audio" for part in candidate.parts),
                reply_index=index + 1,
                reply_count=len(candidates),
            )
            item = progress.items[index]
            try:
                item.receipt = await transport.send(index)
            except asyncio.CancelledError:
                item.state = "unknown"
                progress.reason = "delivery_cancelled_in_flight"
                emit(
                    "delivery.cancelled",
                    source=request.source,
                    delivery_states=[part.state for part in progress.items],
                    reason=progress.reason,
                )
                raise
            except DeliveryError as error:
                item.state = "unknown" if error.ambiguous else "failed"
                progress.error = error
                progress.reason = str(error)
                emit(
                    "delivery.failed",
                    source=request.source,
                    delivery_states=[part.state for part in progress.items],
                    reason=progress.reason,
                )
                return progress
            item.state = "confirmed"
            emit(
                "qq.send.succeeded",
                fallback=request.fallback,
                reply_index=index + 1,
                elapsed_ms=elapsed_ms(started),
            )
            progress.phase = "record"
            if self.record_confirmed:
                self.record_confirmed(request.group_id, candidate, request.source)
        return progress

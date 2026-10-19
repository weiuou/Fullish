"""Small, bounded, ephemeral group context and a single in-flight reply per group."""

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TypedDict

from fullish.model import ChatModel
from fullish.model import Turn as ModelTurn
from fullish.telemetry import emit, message_trace, reference, trace_id

HISTORY_MESSAGES = 60
HISTORY_CHARS = 6000
GROUP_IDLE_SECONDS = 1800
IMAGE_WINDOW_SECONDS = 150
MAX_IMAGES = 4


class Turn(TypedDict):
    role: str
    content: str
    images: list[str]
    created: float
    trace_id: str


def turn(role: str, content: str, *, images: list[str] | None = None) -> Turn:
    return {
        "role": role,
        "content": content,
        "images": images or [],
        "created": time.monotonic(),
        "trace_id": trace_id(),
    }


@dataclass
class Group:
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=HISTORY_MESSAGES))
    seen: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = 0
    last_attempt: float = float("-inf")


class Conversations:
    def __init__(self, model: ChatModel, *, allowed_groups: frozenset[str] = frozenset()):
        self.model = model
        self.allowed_groups = allowed_groups
        self.groups: OrderedDict[str, Group] = OrderedDict()
        self.active = 0

    async def receive(
        self,
        group_id: str,
        message_id: str,
        sender: str,
        text: str,
        *,
        addressed: bool,
        images: list[str] | None = None,
    ) -> str | None:
        with message_trace(group_id, message_id):
            return await self._receive(
                group_id, message_id, sender, text, addressed=addressed, images=images
            )

    async def _receive(
        self,
        group_id: str,
        message_id: str,
        sender: str,
        text: str,
        *,
        addressed: bool,
        images: list[str] | None,
    ) -> str | None:
        images = images or []
        emit(
            "conversation.received",
            sender_ref=reference(sender),
            text_chars=len(text),
            image_count=len(images),
            addressed=addressed,
        )
        if self.allowed_groups and group_id not in self.allowed_groups:
            emit("conversation.skipped", reason="group_not_allowed")
            return None
        # The adapter strips our mention. An empty remainder is still a valid call.
        if not text.strip():
            text = "（@了 Fullish）" if addressed else "[图片]" if images else "[无文字内容]"
            emit("conversation.normalized", reason="mention_only" if addressed else "no_text")
        now = time.monotonic()
        # Never evict an in-flight group's lock, including when its context expires.
        for key, group in list(self.groups.items()):
            if not group.lock.locked() and now - group.touched > GROUP_IDLE_SECONDS:
                del self.groups[key]
                emit("context.evicted", evicted_group_ref=reference(key), reason="idle")
        if group_id not in self.groups:
            if len(self.groups) >= 50:
                victim = next((k for k, v in self.groups.items() if not v.lock.locked()), None)
                if victim is None:
                    emit("conversation.skipped", reason="group_capacity")
                    return None
                del self.groups[victim]
                emit("context.evicted", evicted_group_ref=reference(victim), reason="capacity")
            self.groups[group_id] = Group()
        group = self.groups[group_id]
        group.touched = now
        self.groups.move_to_end(group_id)
        if message_id in group.seen:
            emit("conversation.skipped", reason="duplicate")
            return None
        group.seen.append(message_id)
        group.turns.append(turn("user", f"群成员 {sender}: {text[:2000]}", images=list(images)))
        self._trim(group.turns)
        emit("context.recorded", role="user", history_turns=len(group.turns))
        called = addressed or "fullish" in text.lower()
        # Initially speak only when addressed or named. Other messages provide context.
        reason = (
            "context_only"
            if not called
            else "group_busy"
            if group.lock.locked()
            else "global_capacity"
            if self.active >= 4
            else None
        )
        if reason:
            emit("conversation.skipped", reason=reason)
            return None
        if now - group.last_attempt < 5:
            emit("conversation.skipped", reason="cooldown")
            return None
        group.last_attempt = now
        async with group.lock:
            self.active += 1
            try:
                # Only recent pictures are worth re-downloading; older ones stay as
                # [图片] placeholders so context keeps its shape without extra cost.
                history: list[ModelTurn] = []
                remaining = MAX_IMAGES
                for item in reversed(group.turns):
                    recent = (
                        item["images"][-remaining:]
                        if remaining and now - item["created"] <= IMAGE_WINDOW_SECONDS
                        else []
                    )
                    remaining -= len(recent)
                    history.append(
                        {
                            "role": item["role"],
                            "content": item["content"],
                            "images": recent,
                            "trace_id": item["trace_id"],
                        }
                    )
                emit(
                    "conversation.generating",
                    trigger="mention" if addressed else "name",
                    history_turns=len(history),
                    context_trace_ids=[item["trace_id"] for item in group.turns],
                    image_count=MAX_IMAGES - remaining,
                    omitted_images=sum(len(item["images"]) for item in group.turns)
                    - (MAX_IMAGES - remaining),
                )
                return await self.model.reply(history[::-1], addressed=called)
            finally:
                self.active -= 1

    def sent(self, group_id: str, text: str) -> None:
        # Record only replies that QQ accepted, not failed sends.
        if group := self.groups.get(group_id):
            group.turns.append(turn("assistant", text))
            self._trim(group.turns)
            emit("context.recorded", role="assistant", history_turns=len(group.turns))

    @staticmethod
    def _trim(turns: deque[Turn]) -> None:
        # Character budget tracks the real prompt cost better than a fixed count.
        total = sum(len(item["content"]) for item in turns)
        removed: list[str] = []
        while total > HISTORY_CHARS and len(turns) > 1:
            item = turns.popleft()
            total -= len(item["content"])
            removed.append(item["trace_id"])
        if removed:
            emit("context.trimmed", reason="character_budget", removed_trace_ids=removed)

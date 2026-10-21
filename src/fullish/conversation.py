"""Small, bounded, ephemeral group context and a single in-flight reply per group."""

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import TypedDict

from fullish.collection import CollectedMessage, MessagePart, ParsedContent, Sender
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
    message: CollectedMessage | None


def turn(
    role: str,
    content: str,
    *,
    images: list[str] | None = None,
    message: CollectedMessage | None = None,
) -> Turn:
    return {
        "role": role,
        "content": content,
        "images": images or [],
        "created": time.monotonic(),
        "trace_id": trace_id(),
        "message": message,
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
        """Convenience entry for text-based replay and callers without an adapter."""
        parts = (MessagePart("text", text),) + tuple(
            MessagePart("image", "", {"url": url}, status="pending") for url in images or ()
        )
        return await self.receive_message(
            CollectedMessage(group_id, message_id, Sender(sender), ParsedContent(parts), addressed)
        )

    async def receive_message(self, message: CollectedMessage) -> str | None:
        with message_trace(message.group_id, message.message_id):
            return await self._receive(message)

    async def _receive(self, message: CollectedMessage) -> str | None:
        group_id, message_id = message.group_id, message.message_id
        text, images = message.content.text, message.content.image_urls
        addressed = message.addressed
        called = addressed or "fullish" in text.lower()
        emit(
            "conversation.received",
            sender_ref=reference(message.sender.id),
            text_chars=len(text),
            image_count=len(images),
            reply_image_count=len(message.reply.content.image_urls) if message.reply else 0,
            addressed=addressed,
        )
        if self.allowed_groups and group_id not in self.allowed_groups:
            emit("conversation.skipped", reason="group_not_allowed")
            return None
        content = message.content.limited(2000)
        reply = message.reply
        if reply:
            reply = replace(reply, content=reply.content.limited(2000 - len(content.text)))
        message = replace(message, content=content, reply=reply)
        text = message.content.text
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
        group.turns.append(turn("user", text, images=list(images), message=message))
        self._trim(group.turns)
        emit("context.recorded", role="user", history_turns=len(group.turns))
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
                # Explicit quotes get the picture budget first, including pictures
                # whose original message was never received or has aged out.
                history: list[ModelTurn] = []
                selected: set[str] = set()

                def select_images(urls: list[str]) -> list[str]:
                    chosen = []
                    for url in reversed(urls):
                        if len(selected) >= MAX_IMAGES:
                            break
                        if url not in selected:
                            selected.add(url)
                            chosen.append(url)
                    return chosen[::-1]

                image_count = 0
                for item in reversed(group.turns):
                    source = item["message"]
                    quote = source.reply if source else None
                    recent = now - item["created"] <= IMAGE_WINDOW_SECONDS
                    image_count += len(item["images"]) + (
                        len(quote.content.image_urls) if quote else 0
                    )
                    model_turn: ModelTurn = {
                        "role": item["role"],
                        "content": item["content"],
                        "trace_id": item["trace_id"],
                    }
                    if quote:
                        model_turn["reply"] = quote
                        model_turn["reply_images"] = (
                            select_images(quote.content.image_urls) if recent else []
                        )
                    model_turn["images"] = select_images(item["images"]) if recent else []
                    if source:
                        model_turn["sender_id"] = source.sender.id
                        model_turn["sender_name"] = source.sender.display_name
                        model_turn["message_id"] = source.message_id
                        model_turn["sent_at"] = source.sent_at
                    history.append(model_turn)
                emit(
                    "conversation.generating",
                    trigger="mention" if addressed else "name",
                    history_turns=len(history),
                    context_trace_ids=[item["trace_id"] for item in group.turns],
                    image_count=len(selected),
                    omitted_images=image_count - len(selected),
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
        # Bound body text as well as turn count; source metadata is retained separately.
        def text_chars(item: Turn) -> int:
            source = item["message"]
            quote = source.reply if source else None
            return len(item["content"]) + (len(quote.content.text) if quote else 0)

        total = sum(text_chars(item) for item in turns)
        removed: list[str] = []
        while total > HISTORY_CHARS and len(turns) > 1:
            item = turns.popleft()
            total -= text_chars(item)
            removed.append(item["trace_id"])
        if removed:
            emit("context.trimmed", reason="character_budget", removed_trace_ids=removed)

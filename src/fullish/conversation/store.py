"""Bounded ephemeral conversation records and snapshots; no model or policy dependencies."""

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import TypedDict

from fullish.collection import CollectedMessage
from fullish.conversation.schema import Turn as ModelTurn
from fullish.scheduling.generation import GroupActivity
from fullish.sending import Reply
from fullish.sending.schema import outgoing
from fullish.telemetry import emit, reference, trace_id

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
    source: str


def turn(
    role: str,
    content: str,
    *,
    images: list[str] | None = None,
    message: CollectedMessage | None = None,
    source: str = "chat",
) -> Turn:
    return {
        "role": role,
        "content": content,
        "images": images or [],
        "created": time.monotonic(),
        "trace_id": trace_id(),
        "message": message,
        "source": source,
    }


@dataclass
class Group:
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=HISTORY_MESSAGES))
    seen: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    activity: GroupActivity = field(default_factory=GroupActivity)
    touched: float = 0


class ConversationStore:
    def __init__(self):
        self.groups: OrderedDict[str, Group] = OrderedDict()

    def record(self, message: CollectedMessage) -> tuple[Group, Turn] | None:
        group_id, message_id = message.group_id, message.message_id
        addressed = message.addressed
        images = message.content.image_urls
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
            if not group.activity.lock.locked() and now - group.touched > GROUP_IDLE_SECONDS:
                del self.groups[key]
                emit("context.evicted", evicted_group_ref=reference(key), reason="idle")
        if group_id not in self.groups:
            if len(self.groups) >= 50:
                victim = next(
                    (k for k, v in self.groups.items() if not v.activity.lock.locked()), None
                )
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
        current_turn = turn("user", text, images=list(images), message=message)
        group.turns.append(current_turn)
        self._trim(group.turns)
        emit("context.recorded", role="user", history_turns=len(group.turns))

        return group, current_turn

    def snapshot(self, group: Group, current_turn: Turn, *, addressed: bool) -> list[ModelTurn]:
        now = time.monotonic()
        message = current_turn["message"]
        assert message is not None
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
        # Keep this request's target last even for concurrent non-QQ callers.
        snapshot = [
            item
            for item in group.turns
            if item is not current_turn
            and (item["role"] == "assistant" or item["created"] <= current_turn["created"])
        ] + [current_turn]
        for item in reversed(snapshot):
            source = item["message"]
            quote = source.reply if source else None
            recent = now - item["created"] <= IMAGE_WINDOW_SECONDS
            image_count += len(item["images"]) + (len(quote.content.image_urls) if quote else 0)
            model_turn: ModelTurn = {
                "role": item["role"],
                "content": item["content"],
                "trace_id": item["trace_id"],
                "source": item["source"],
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
            context_trace_ids=[item["trace_id"] for item in snapshot],
            image_count=len(selected),
            omitted_images=image_count - len(selected),
        )
        return history[::-1]

    def sent(self, group_id: str, reply: Reply, source: str = "chat") -> None:
        # Record only replies that QQ accepted, not failed sends.
        if group := self.groups.get(group_id):
            group.turns.append(
                turn("assistant", outgoing(reply).context_text[:2000], source=source)
            )
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

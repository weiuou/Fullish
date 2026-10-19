"""Small, bounded, ephemeral group context and a single in-flight reply per group."""

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from fullish.model import ChatModel, Turn


@dataclass
class Group:
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=20))
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
        self, group_id: str, message_id: str, sender: str, text: str, *, addressed: bool
    ) -> str | None:
        if not text.strip() or (self.allowed_groups and group_id not in self.allowed_groups):
            return None
        now = time.monotonic()
        # Never evict an in-flight group's lock, including when its context expires.
        for key, group in list(self.groups.items()):
            if not group.lock.locked() and now - group.touched > 900:
                del self.groups[key]
        if group_id not in self.groups:
            if len(self.groups) >= 50:
                victim = next((k for k, v in self.groups.items() if not v.lock.locked()), None)
                if victim is None:
                    return None
                del self.groups[victim]
            self.groups[group_id] = Group()
        group = self.groups[group_id]
        group.touched = now
        self.groups.move_to_end(group_id)
        if message_id in group.seen:
            return None
        group.seen.append(message_id)
        group.turns.append({"role": "user", "content": f"群成员 {sender}: {text[:2000]}"})
        called = addressed or "fullish" in text.lower()
        # Initially speak only when addressed or named. Other messages provide context.
        if not called or group.lock.locked() or self.active >= 4:
            return None
        if now - group.last_attempt < 5:
            return None
        group.last_attempt = now
        async with group.lock:
            self.active += 1
            try:
                return await self.model.reply(list(group.turns), addressed=called)
            finally:
                self.active -= 1

    def sent(self, group_id: str, text: str) -> None:
        # Record only replies that QQ accepted, not failed sends.
        if group := self.groups.get(group_id):
            group.turns.append({"role": "assistant", "content": text})

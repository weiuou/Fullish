"""Private developer commands bypass the conversational model and ordinary event logging."""

import asyncio
import time
from collections import OrderedDict, deque

from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.event import C2CMessageCreateEvent
from nonebot.adapters.qq.models import Dispatch

from fullish.control import GroupControl
from fullish.telemetry import emit, message_trace, reference


class DeveloperMenu:
    def __init__(self, control: GroupControl):
        self.control = control
        self.usage: OrderedDict[str, deque[float]] = OrderedDict()

    def _charge(self, user: str) -> bool:
        now = time.monotonic()
        for key, limit in [("all", 60), ("user:" + user, 12)]:
            bucket = self.usage.setdefault(key, deque())
            while bucket and bucket[0] < now - 60:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
        for key in ("all", "user:" + user):
            self.usage[key].append(now)
            self.usage.move_to_end(key)
        while len(self.usage) > 1024:
            self.usage.popitem(last=False)
        return True

    async def handle(self, bot: Bot, payload: Dispatch) -> None:
        # Parsing failures are caught by the caller without dumping the raw payload.
        event = C2CMessageCreateEvent.model_validate(payload.data)
        user = event.author.user_openid
        with message_trace("", event.id):
            if not self._charge(user):
                emit("developer.command.skipped", reason="rate_limit", sender_ref=reference(user))
                return
            reply = await asyncio.to_thread(self.control.command, user, event.id, event.content)
            if reply:
                await bot.post_c2c_messages(
                    openid=user, msg_type=0, content=reply[:1500], msg_id=event.id, msg_seq=1
                )
            emit(
                "developer.command.completed",
                sender_ref=reference(user),
                outcome="replied" if reply else "ignored",
            )

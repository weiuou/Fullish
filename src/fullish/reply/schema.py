"""Platform-independent input to a reply engine."""

from typing import Protocol

from fullish.conversation.schema import Turn as Turn
from fullish.sending import Reply
from fullish.tools.runtime import ToolContext


class ReplyModel(Protocol):
    async def reply(
        self, turns: list[Turn], *, addressed: bool, context: ToolContext | None = None
    ) -> Reply | None: ...

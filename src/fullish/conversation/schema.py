"""Platform-independent conversation snapshots consumed by reply providers."""

from typing import NotRequired, TypedDict

from fullish.collection import QuotedMessage


class Turn(TypedDict):
    role: str
    content: str
    images: NotRequired[list[str]]
    trace_id: NotRequired[str]
    source: NotRequired[str]
    sender_id: NotRequired[str]
    sender_name: NotRequired[str | None]
    message_id: NotRequired[str]
    sent_at: NotRequired[str | None]
    reply: NotRequired[QuotedMessage]
    reply_images: NotRequired[list[str]]

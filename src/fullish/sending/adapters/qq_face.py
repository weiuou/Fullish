"""Readable fallback until native group QQ face support is verified."""

from nonebot.adapters.qq import MessageSegment

from fullish.sending.schema import MessagePart, QQFace


def encode(part: MessagePart) -> MessageSegment:
    if not isinstance(part, QQFace):
        raise TypeError("QQ face adapter requires a QQFace part")
    return MessageSegment.text(part.fallback)

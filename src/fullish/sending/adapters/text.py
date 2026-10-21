"""QQ text segment encoder."""

from nonebot.adapters.qq import MessageSegment

from fullish.sending.schema import MessagePart, Text


def encode(part: MessagePart) -> MessageSegment:
    if not isinstance(part, Text):
        raise TypeError("Text adapter requires a Text part")
    return MessageSegment.text(part.text)

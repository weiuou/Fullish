"""QQ image encoder, including images attached to text in the same message."""

from nonebot.adapters.qq import MessageSegment

from fullish.sending.schema import Image, MessagePart


def encode(part: MessagePart) -> MessageSegment:
    if not isinstance(part, Image):
        raise TypeError("Image adapter requires an Image part")
    if isinstance(part.source, str):
        return MessageSegment.image(part.source)
    return MessageSegment.file_image(part.source)

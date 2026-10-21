"""QQ voice encoder. The source must already use a format QQ accepts."""

from nonebot.adapters.qq import MessageSegment

from fullish.sending.schema import Audio, MessagePart


def encode(part: MessagePart) -> MessageSegment:
    if not isinstance(part, Audio):
        raise TypeError("Audio adapter requires an Audio part")
    if isinstance(part.source, str):
        return MessageSegment.audio(part.source)
    return MessageSegment.file_audio(part.source)

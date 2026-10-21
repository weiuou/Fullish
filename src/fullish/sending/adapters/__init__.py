"""Replace individual segment encoders without changing conversation policy."""

from collections.abc import Callable

from nonebot.adapters.qq import MessageSegment

from fullish.sending.adapters import audio, image, text
from fullish.sending.schema import MessagePart

type SegmentAdapter = Callable[[MessagePart], MessageSegment]

ADAPTERS: dict[str, SegmentAdapter] = {
    "text": text.encode,
    "image": image.encode,
    "audio": audio.encode,
}

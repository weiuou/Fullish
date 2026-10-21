"""Segment dispatch: add a parser without changing the QQ handler or conversation policy."""

from collections.abc import Iterable, Mapping

from fullish.collection.parsers import audio, emoji, file, image, mention, text, video
from fullish.collection.parsers.base import SegmentParser
from fullish.collection.schema import MessagePart, ParsedContent

PARSERS: dict[str, SegmentParser] = {
    "text": text.parse,
    "emoji": emoji.parse,
    "image": image.parse,
    "mention_user": mention.parse,
    "group_mention_user": mention.parse,
    "mention_everyone": mention.parse_everyone,
    "group_mention_everyone": mention.parse_everyone,
    "audio": audio.parse,
    "video": video.parse,
    "file": file.parse,
}


def parse_segments(
    segments: Iterable[tuple[str, Mapping[str, object]]],
    *,
    parsers: Mapping[str, SegmentParser] | None = None,
) -> ParsedContent:
    registry = PARSERS if parsers is None else parsers
    parts = tuple(
        parser(data)
        if (parser := registry.get(kind))
        else MessagePart(kind, "[未支持内容]", status="unsupported")
        for kind, data in segments
    )
    return ParsedContent(parts)

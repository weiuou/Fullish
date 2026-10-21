"""Audio placeholder. Speech recognition can be added here later."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    return MessagePart(
        "audio",
        "[语音]",
        metadata(data, "url", "filename", "content_type", "size"),
        status="pending",
    )

"""Video placeholder. Frame extraction and understanding are not implemented yet."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    return MessagePart(
        "video",
        "[视频]",
        metadata(data, "url", "filename", "content_type", "size"),
        status="pending",
    )

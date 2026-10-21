"""File placeholder. Collection never downloads or executes file contents."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    return MessagePart(
        "file",
        "[文件]",
        metadata(data, "url", "filename", "content_type", "size"),
        status="pending",
    )

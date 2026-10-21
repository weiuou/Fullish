"""Collect image references; the existing model path downloads selected images later."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    return MessagePart(
        "image",
        "[图片]",
        metadata(data, "url", "filename", "content_type", "size", "width", "height"),
        status="pending",
    )

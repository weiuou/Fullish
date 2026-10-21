"""Plain text needs no semantic processing during collection."""

from collections.abc import Mapping

from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    text = data.get("text", "")
    return MessagePart("text", text if isinstance(text, str) else "")

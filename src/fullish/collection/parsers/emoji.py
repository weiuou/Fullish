"""Preserve the emoji ID; interpreting its meaning is a future extension."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    return MessagePart("emoji", "[表情]", metadata(data, "id"), status="pending")

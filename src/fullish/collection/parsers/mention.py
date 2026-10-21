"""Keep mention targets separate from their human-readable names."""

from collections.abc import Mapping

from fullish.collection.parsers.base import metadata
from fullish.collection.schema import MessagePart


def parse(data: Mapping[str, object]) -> MessagePart:
    name = data.get("username")
    return MessagePart(
        "mention",
        f"@{name}" if isinstance(name, str) and name else "@某人",
        metadata(data, "user_id", "member_openid", "username", "is_bot", "is_you"),
    )


def parse_everyone(data: Mapping[str, object]) -> MessagePart:
    return MessagePart("mention", "@全体成员", {"scope": "all"})

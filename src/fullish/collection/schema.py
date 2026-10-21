"""Platform-independent input records; message bodies never define their sender."""

from dataclasses import dataclass, field, replace
from typing import Literal

type MetadataValue = str | int | float | bool | None


@dataclass(frozen=True)
class Sender:
    id: str
    display_name: str | None = None


@dataclass(frozen=True)
class MessagePart:
    kind: str
    text: str
    metadata: dict[str, MetadataValue] = field(default_factory=dict)
    status: Literal["parsed", "pending", "unsupported"] = "parsed"


@dataclass(frozen=True)
class ParsedContent:
    parts: tuple[MessagePart, ...]

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts).strip()

    @property
    def image_urls(self) -> list[str]:
        return [
            url
            for part in self.parts
            if part.kind == "image" and isinstance(url := part.metadata.get("url"), str) and url
        ]

    def limited(self, max_chars: int) -> "ParsedContent":
        """Bound retained body text without discarding attachment metadata."""
        remaining = max_chars
        parts = []
        for part in self.parts:
            text = part.text[:remaining]
            parts.append(replace(part, text=text))
            remaining -= len(text)
        return ParsedContent(tuple(parts))


@dataclass(frozen=True)
class QuotedMessage:
    content: ParsedContent
    reference_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None


@dataclass(frozen=True)
class CollectedMessage:
    group_id: str
    message_id: str
    sender: Sender
    content: ParsedContent
    addressed: bool
    sent_at: str | None = None
    reply: QuotedMessage | None = None

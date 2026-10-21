"""Transport-independent reply candidates. Media is supplied by a caller/provider."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

type MediaSource = str | bytes | Path


def validate_source(source: MediaSource) -> None:
    if isinstance(source, str):
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Media URLs must use HTTP or HTTPS; use Path for local files")
    elif isinstance(source, bytes):
        if not source:
            raise ValueError("Media bytes must not be empty")
    elif not isinstance(source, Path):
        raise TypeError("Media must be a URL, bytes or Path")


@dataclass(frozen=True)
class Text:
    text: str
    kind: ClassVar[str] = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("Text must be a string")


@dataclass(frozen=True)
class Image:
    source: MediaSource
    kind: ClassVar[str] = "image"

    def __post_init__(self) -> None:
        validate_source(self.source)


@dataclass(frozen=True)
class Audio:
    source: MediaSource
    kind: ClassVar[str] = "audio"

    def __post_init__(self) -> None:
        validate_source(self.source)


type MessagePart = Text | Image | Audio


@dataclass(frozen=True)
class OutgoingMessage:
    parts: tuple[MessagePart, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.parts, tuple):
            raise TypeError("Message parts must be a tuple")
        if any(not isinstance(part, (Text, Image, Audio)) for part in self.parts):
            raise TypeError("Unsupported outgoing message part")
        # QQ's group sender picks only one attachment. Reject rather than lose media.
        if sum(isinstance(part, (Image, Audio)) for part in self.parts) > 1:
            raise ValueError("A QQ reply supports at most one image or audio attachment")
        if not self.context_text.strip():
            raise ValueError("An outgoing message must not be empty")

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts if isinstance(part, Text))

    @property
    def context_text(self) -> str:
        # Never put local paths, media URLs or encoded bytes into assistant history.
        return "".join(
            part.text
            if isinstance(part, Text)
            else "[图片]"
            if isinstance(part, Image)
            else "[语音]"
            for part in self.parts
        )


type Reply = str | OutgoingMessage


def outgoing(reply: Reply) -> OutgoingMessage:
    return OutgoingMessage((Text(reply),)) if isinstance(reply, str) else reply

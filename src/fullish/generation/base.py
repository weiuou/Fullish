"""Image generation contract. Providers return bytes, never send to a platform."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class GeneratedImage:
    image: bytes = field(repr=False)
    text: str = ""


class ImageGenerator(Protocol):
    async def generate(self, prompt: str) -> GeneratedImage: ...

    async def close(self) -> None: ...

"""Operator-curated expression assets; models choose IDs, never file paths."""

from __future__ import annotations

import io
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image as PILImage

from fullish.sending.schema import Image, QQFace

MAX_ASSETS = 24
MAX_MANIFEST_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}


@dataclass(frozen=True)
class ReplyAsset:
    id: str
    description: str
    part: Image | QQFace

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id) is None:
            raise ValueError("Asset ID must contain 1 to 64 ASCII letters, digits, '_' or '-'")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description) > 200
        ):
            raise ValueError("Asset description must contain 1 to 200 characters")
        if not isinstance(self.part, (Image, QQFace)):
            raise TypeError("Reply assets must be images or QQ faces")


@dataclass(frozen=True)
class ReplyAssets:
    items: tuple[ReplyAsset, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, ReplyAsset) for item in self.items
        ):
            raise TypeError("Reply assets must be a tuple of ReplyAsset objects")
        if len(self.items) > MAX_ASSETS:
            raise ValueError("Reply asset catalog must not exceed 24 items")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Reply asset IDs must be unique")

    @classmethod
    def from_file(cls, path: Path) -> ReplyAssets:
        """Validate a small manifest and freeze local image bytes at startup.

        HTTPS images are passed to the existing QQ URL transport without fetching.
        Their description should accurately describe the operator-controlled image.
        """
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Reply asset manifest must not exceed 64 KiB")
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or set(manifest) != {"assets"}:
            raise ValueError("Reply asset manifest must contain only an assets list")
        entries = manifest["assets"]
        if not isinstance(entries, list) or len(entries) > MAX_ASSETS:
            raise ValueError("Reply asset manifest must contain a list of at most 24 items")
        items = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Reply asset entry must be an object")
            common = {"id", "description", "kind"}
            kind = entry.get("kind")
            part: Image | QQFace
            if kind == "qq_face":
                if set(entry) != common | {"face_id", "fallback"}:
                    raise ValueError("QQ face asset requires face_id and fallback")
                part = QQFace(entry["face_id"], entry["fallback"])
            elif kind == "image":
                if set(entry) == common | {"path"}:
                    source = entry["path"]
                    if not isinstance(source, str) or not source.strip():
                        raise ValueError("Image asset path must be a nonempty string")
                    part = Image(_local_image(path.parent / source))
                elif set(entry) == common | {"url"}:
                    source = entry["url"]
                    if not isinstance(source, str):
                        raise ValueError("Image asset URL must be a string")
                    parsed = urlsplit(source)
                    if parsed.scheme != "https" or not parsed.hostname:
                        raise ValueError("Image asset URL must use HTTPS")
                    if parsed.username or parsed.password:
                        raise ValueError("Image asset URL must not contain credentials")
                    part = Image(source)
                else:
                    raise ValueError("Image asset requires exactly one path or url")
            else:
                raise ValueError("Unsupported reply asset kind")
            items.append(ReplyAsset(entry["id"], entry["description"], part))
        return cls(tuple(items))


def _local_image(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Reply image asset must not exceed 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data)) as image:
                if image.format not in IMAGE_FORMATS:
                    raise ValueError("Reply images must be PNG, JPEG, GIF or WebP")
                image.verify()
            # JPEG verify() does not decode the raster, so truncated scan data can
            # pass it. Reopen after verify() and decode the first frame as well.
            # Keep both opens inside the decompression-bomb warning boundary.
            with PILImage.open(io.BytesIO(data)) as image:
                image.load()
    except (OSError, SyntaxError, PILImage.DecompressionBombError, Warning) as exc:
        raise ValueError("Reply image asset is not a valid supported image") from exc
    return data

"""Prepare bounded reference images for the static-image edit endpoint."""

from io import BytesIO

from PIL import Image, UnidentifiedImageError

from fullish.errors import ModelError
from fullish.media import sniff_image_type

MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_PIXELS = 16_000_000
EDIT_TYPES = {"image/png", "image/jpeg", "image/webp"}


class _BoundedPNG(BytesIO):
    def write(self, data, /):
        if self.tell() + len(data) > MAX_REFERENCE_BYTES:
            raise ModelError("image_invalid_reference")
        return super().write(data)


def _gif_first_frame(body: bytes) -> bytes:
    try:
        # Check the logical canvas before Pillow can allocate decoded pixels.
        if len(body) < 10:
            raise ModelError("image_invalid_reference")
        width = int.from_bytes(body[6:8], "little")
        height = int.from_bytes(body[8:10], "little")
        if not width or not height or width * height > MAX_REFERENCE_PIXELS:
            raise ModelError("image_invalid_reference")
        with Image.open(BytesIO(body), formats=["GIF"]) as image:
            if image.width * image.height > MAX_REFERENCE_PIXELS:
                raise ModelError("image_invalid_reference")
            # The initial frame supplies a static reference, including palette transparency.
            with image.convert("RGBA") as frame, _BoundedPNG() as output:
                frame.save(output, format="PNG")
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ModelError("image_invalid_reference") from None


def prepare_reference_images(images: tuple[bytes, ...]) -> tuple[bytes, ...]:
    """Keep supported formats intact and convert only GIFs, with limits on both sides."""
    if (
        len(images) > 4
        or sum(len(body) for body in images) > MAX_TOTAL_REFERENCE_BYTES
        or any(
            not body
            or len(body) > MAX_REFERENCE_BYTES
            or sniff_image_type(body) not in EDIT_TYPES | {"image/gif"}
            for body in images
        )
    ):
        raise ModelError("image_invalid_reference")
    prepared = []
    size = 0
    for body in images:
        result = _gif_first_frame(body) if sniff_image_type(body) == "image/gif" else body
        size += len(result)
        if size > MAX_TOTAL_REFERENCE_BYTES:
            raise ModelError("image_invalid_reference")
        prepared.append(result)
    return tuple(prepared)

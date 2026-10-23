"""Prepare bounded reference images for the static-image edit endpoint."""

from fullish.animated_images import GifError, gif_preview
from fullish.errors import ModelError
from fullish.media import sniff_image_type

MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_REFERENCE_BYTES = 20 * 1024 * 1024
EDIT_TYPES = {"image/png", "image/jpeg", "image/webp"}


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
        try:
            result = gif_preview(body).image if sniff_image_type(body) == "image/gif" else body
        except GifError:
            raise ModelError("image_invalid_reference") from None
        size += len(result)
        if len(result) > MAX_REFERENCE_BYTES or size > MAX_TOTAL_REFERENCE_BYTES:
            raise ModelError("image_invalid_reference")
        prepared.append(result)
    return tuple(prepared)

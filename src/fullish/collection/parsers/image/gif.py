"""Bounded GIF timelines shared by chat vision and reference editing."""

from bisect import bisect_right
from dataclasses import dataclass, field
from io import BytesIO
from math import ceil

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

MAX_GIF_BYTES = 10 * 1024 * 1024
MAX_GIF_PIXELS = 16_000_000
MAX_GIF_FRAMES = 512
MAX_DECODE_PIXELS = 64_000_000
MAX_SAMPLES = 12
MAX_PREVIEW_BYTES = 3 * 1024 * 1024
CELL_SIZE = 256
LABEL_HEIGHT = 24


class GifError(ValueError):
    pass


class _BoundedPNG(BytesIO):
    def write(self, data, /):
        if self.tell() + len(data) > MAX_PREVIEW_BYTES:
            raise GifError("gif_preview_too_large")
        return super().write(data)


@dataclass(frozen=True)
class GifPreview:
    image: bytes = field(repr=False)
    frame_count: int
    frame_indices: tuple[int, ...]
    timestamps_ms: tuple[int, ...]
    duration_ms: int

    @property
    def description(self) -> str:
        if self.frame_count == 1:
            return "静态 GIF，已转为 PNG。"
        return (
            f"GIF 动作序列：原动画 {self.frame_count} 帧、{self.duration_ms / 1000:.2f} 秒；"
            f"下图抽样 {len(self.frame_indices)} 帧，包含开头和结尾。"
            "按从左到右、从上到下的时间顺序阅读，格子标有原帧号和时间。"
            "这些是同一主体连续动作，不是多个角色；结合整个动作过程理解图片。"
        )


def _sample_indices(starts: list[int], duration: int) -> tuple[int, ...]:
    count = min(len(starts), MAX_SAMPLES)
    if len(starts) <= count:
        return tuple(range(count))
    chosen = {0, len(starts) - 1}
    for i in range(1, count - 1):
        target = i * (duration - 1) // (count - 1)
        chosen.add(max(0, bisect_right(starts, target) - 1))
    # Long held frames should not consume every slot and hide brief movements.
    while len(chosen) < count:
        ordered = sorted(chosen)
        left, right = max(
            zip(ordered, ordered[1:], strict=False), key=lambda pair: pair[1] - pair[0]
        )
        chosen.add((left + right) // 2)
    return tuple(sorted(chosen))


def gif_preview(body: bytes) -> GifPreview:
    """Decode the full bounded timeline, then render representative composited frames."""
    try:
        if len(body) < 10 or len(body) > MAX_GIF_BYTES:
            raise GifError("gif_invalid_or_too_large")
        width = int.from_bytes(body[6:8], "little")
        height = int.from_bytes(body[8:10], "little")
        if not width or not height or width * height > MAX_GIF_PIXELS:
            raise GifError("gif_pixel_limit")
        with Image.open(BytesIO(body), formats=["GIF"]) as image:
            starts = []
            duration = decoded_pixels = 0
            # Public seek composes disposal and partial frames correctly. Both passes
            # stay bounded; never query n_frames, which scans an unlimited timeline.
            for index in range(MAX_GIF_FRAMES + 1):
                try:
                    image.seek(index)
                except EOFError:
                    break
                pixels = image.width * image.height
                decoded_pixels += pixels
                if (
                    index == MAX_GIF_FRAMES
                    or pixels > MAX_GIF_PIXELS
                    or decoded_pixels > MAX_DECODE_PIXELS
                ):
                    raise GifError("gif_decode_limit")
                starts.append(duration)
                duration += max(10, int(image.info.get("duration", 100) or 100))
            indices = _sample_indices(starts, duration)
            with _BoundedPNG() as output:
                if len(starts) == 1:
                    image.seek(0)
                    with image.convert("RGBA") as frame:
                        frame.save(output, format="PNG")
                else:
                    columns = min(4, len(indices))
                    rows = ceil(len(indices) / columns)
                    with Image.new(
                        "RGB", (columns * CELL_SIZE, rows * (CELL_SIZE + LABEL_HEIGHT)), "white"
                    ) as sheet:
                        draw = ImageDraw.Draw(sheet)
                        for position, index in enumerate(indices):
                            image.seek(index)
                            with (
                                image.convert("RGBA") as original,
                                ImageOps.contain(
                                    original, (CELL_SIZE, CELL_SIZE), Image.Resampling.LANCZOS
                                ) as frame,
                            ):
                                left = (position % columns) * CELL_SIZE
                                top = (position // columns) * (CELL_SIZE + LABEL_HEIGHT)
                                draw.text(
                                    (left + 4, top + 4),
                                    f"#{index + 1}  {starts[index] / 1000:.2f}s",
                                    fill="black",
                                )
                                sheet.paste(
                                    frame,
                                    (
                                        left + (CELL_SIZE - frame.width) // 2,
                                        top + LABEL_HEIGHT + (CELL_SIZE - frame.height) // 2,
                                    ),
                                    frame,
                                )
                        sheet.save(output, format="PNG")
                return GifPreview(
                    output.getvalue(),
                    len(starts),
                    indices,
                    tuple(starts[index] for index in indices),
                    duration,
                )
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        if isinstance(error, GifError):
            raise
        raise GifError("gif_invalid") from None

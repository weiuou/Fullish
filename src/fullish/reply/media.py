"""Bounded public-media reads; model credentials never enter the media client."""

import asyncio
import base64
import time
from urllib.parse import urlsplit

import httpx

from fullish.collection.parsers.image.formats import sniff_image_type
from fullish.telemetry import elapsed_ms, emit

MAX_IMAGE_BYTES = 2 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class ImageReader:
    def __init__(self):
        self.media = httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=10),
            follow_redirects=True,
            max_redirects=3,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.media.aclose()

    async def download(
        self, url: str, *, source_trace_id: str = "", image_index: int = 0
    ) -> str | None:
        """Return a data URL for a bounded image fetch, or None when unusable."""
        start = time.monotonic()
        fields = {"source_trace_id": source_trace_id, "image_index": image_index}
        emit("image.download.started", **fields)
        outcome = "cancelled"
        size = 0
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                outcome = "invalid_url"
                return None
            async with asyncio.timeout(15):
                async with self.media.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            outcome = "too_large"
                            return None
                        chunks.append(chunk)
            body = b"".join(chunks)
            media_type = sniff_image_type(body)
            if media_type not in IMAGE_TYPES or not body:
                outcome = "invalid_image"
                return None
            encoded = base64.b64encode(body).decode("ascii")
            outcome = "success"
            return f"data:{media_type};base64,{encoded}"
        except httpx.HTTPStatusError as error:
            outcome = f"http_{error.response.status_code}"
            return None
        except (httpx.TimeoutException, TimeoutError):
            outcome = "timeout"
            return None
        except ValueError:
            outcome = "invalid_url"
            return None
        except httpx.HTTPError:
            outcome = "connection_error"
            return None
        except Exception:
            outcome = "unexpected_error"
            raise
        finally:
            emit(
                "image.download.completed",
                **fields,
                outcome=outcome,
                bytes_received=size,
                elapsed_ms=elapsed_ms(start),
            )

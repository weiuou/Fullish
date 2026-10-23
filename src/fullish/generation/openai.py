"""GPT Image generation through CPA, reusing the chat credentials and socket."""

import asyncio
import base64
import json
import time

import httpx

from fullish.errors import ModelError
from fullish.generation.base import GeneratedImage
from fullish.media import sniff_image_type
from fullish.settings import ImageGenerationSettings, ModelSettings
from fullish.telemetry import elapsed_ms, emit, operation_trace

MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = (MAX_GENERATED_IMAGE_BYTES + 2) // 3 * 4 + 256 * 1024
MAX_PROMPT_CHARS = 4000


class OpenAIImageGenerator:
    def __init__(
        self,
        connection: ModelSettings,
        settings: ImageGenerationSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=connection.base_url + "/",
            headers={"Authorization": "Bearer " + connection.api_key},
            transport=transport or httpx.AsyncHTTPTransport(uds=connection.unix_socket),
            timeout=httpx.Timeout(settings.timeout, connect=10),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def select_model(self) -> str:
        # Retain compatibility with older deployments configured with auto.
        return "gpt-image-2.5" if self.settings.model == "auto" else self.settings.model

    async def generate(
        self, prompt: str, *, reference_images: tuple[bytes, ...] = ()
    ) -> GeneratedImage:
        with operation_trace():
            start = time.monotonic()
            model = await self.select_model()
            emit("image_generation.started", model=model, timeout_seconds=self.settings.timeout)
            try:
                if (
                    not isinstance(prompt, str)
                    or not prompt.strip()
                    or len(prompt) > MAX_PROMPT_CHARS
                ):
                    raise ModelError("image_invalid_prompt")
                if (
                    len(reference_images) > 4
                    or sum(len(body) for body in reference_images) > 20 * 1024 * 1024
                    or any(
                        not body
                        or len(body) > MAX_GENERATED_IMAGE_BYTES
                        or sniff_image_type(body) not in {"image/png", "image/jpeg", "image/webp"}
                        for body in reference_images
                    )
                ):
                    raise ModelError("image_invalid_reference")
                request: dict = {"json": {"model": model, "prompt": prompt, "n": 1}}
                endpoint = "images/generations"
                if reference_images:
                    endpoint = "images/edits"
                    extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
                    request = {
                        "data": {"model": model, "prompt": prompt, "n": "1"},
                        "files": [
                            (
                                "image[]",
                                (
                                    f"reference-{i}.{extensions[str(sniff_image_type(body))]}",
                                    body,
                                    sniff_image_type(body),
                                ),
                            )
                            for i, body in enumerate(reference_images)
                        ],
                    }
                async with asyncio.timeout(self.settings.timeout):
                    emit(
                        "image_generation.request.started",
                        model=model,
                        mode="edit" if reference_images else "generate",
                        reference_count=len(reference_images),
                    )
                    async with self.client.stream(
                        "POST",
                        endpoint,
                        **request,
                    ) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise ModelError("image_response_too_large")
                            body.extend(chunk)
                    result = decode_image(json.loads(body))
            except httpx.HTTPStatusError as error:
                code = f"image_http_{error.response.status_code}"
            except (httpx.RequestError, TimeoutError):
                code = "image_connection_or_timeout"
            except (ValueError, UnicodeError):
                code = "image_invalid_response"
            except ModelError as error:
                code = str(error)
            except asyncio.CancelledError:
                emit("image_generation.cancelled", model=model, elapsed_ms=elapsed_ms(start))
                raise
            else:
                emit(
                    "image_generation.completed",
                    model=model,
                    mode="edit" if reference_images else "generate",
                    reference_count=len(reference_images),
                    bytes_received=len(result.image),
                    elapsed_ms=elapsed_ms(start),
                )
                return result
            emit(
                "image_generation.failed",
                level="WARNING",
                model=model,
                mode="edit" if reference_images else "generate",
                reference_count=len(reference_images),
                error_code=code,
                elapsed_ms=elapsed_ms(start),
            )
            raise ModelError(code) from None


def decode_image(data: dict) -> GeneratedImage:
    try:
        rows = data.get("data")
        if not rows:
            raise ModelError("image_missing")
        if not isinstance(rows, list):
            raise ModelError("image_invalid_response")
        if len(rows) != 1:
            raise ModelError("image_multiple_outputs")
        encoded = rows[0].get("b64_json")
        if not encoded:
            raise ModelError("image_missing")
        if not isinstance(encoded, str) or len(encoded) > (MAX_GENERATED_IMAGE_BYTES + 2) // 3 * 4:
            raise ModelError("image_data_invalid_or_too_large")
        body = base64.b64decode(encoded, validate=True)
        if len(body) > MAX_GENERATED_IMAGE_BYTES:
            raise ModelError("image_data_invalid_or_too_large")
        if not sniff_image_type(body):
            raise ModelError("image_invalid_format")
        return GeneratedImage(body)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ModelError("image_invalid_response") from None

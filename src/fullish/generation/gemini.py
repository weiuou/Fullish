"""Gemini native image generation through CPA, reusing the chat credentials."""

import asyncio
import base64
import json
import time
from urllib.parse import quote

import httpx

from fullish.errors import ModelError
from fullish.generation.base import GeneratedImage
from fullish.media import sniff_image_type
from fullish.settings import ImageGenerationSettings, ModelSettings
from fullish.telemetry import elapsed_ms, emit, operation_trace

MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = (MAX_GENERATED_IMAGE_BYTES + 2) // 3 * 4 + 256 * 1024
MAX_PROMPT_CHARS = 4000


class GeminiImageGenerator:
    def __init__(
        self,
        connection: ModelSettings,
        settings: ImageGenerationSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.native_base = connection.base_url.removesuffix("/v1") + "/v1beta/models/"
        self.client = httpx.AsyncClient(
            base_url=connection.base_url + "/",
            headers={"Authorization": "Bearer " + connection.api_key},
            transport=transport or httpx.AsyncHTTPTransport(uds=connection.unix_socket),
            timeout=httpx.Timeout(settings.timeout, connect=10),
            follow_redirects=False,
            trust_env=False,
        )
        self._model: str | None = None
        self._selection_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _json_request(
        self, method: str, url: str, *, payload: dict | None = None, limit: int | None = None
    ) -> dict:
        limit = MAX_RESPONSE_BYTES if limit is None else limit
        try:
            async with self.client.stream(method, url, json=payload) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise ModelError("image_response_too_large")
                    body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ModelError("image_invalid_response")
            return data
        except httpx.HTTPStatusError as error:
            raise ModelError(f"image_http_{error.response.status_code}") from None
        except (httpx.RequestError, TimeoutError):
            raise ModelError("image_connection_or_timeout") from None
        except (ValueError, UnicodeError):
            raise ModelError("image_invalid_response") from None

    async def select_model(self) -> str:
        """Resolve only against the catalog; this does not spend image-generation quota."""
        async with asyncio.timeout(self.settings.timeout), self._selection_lock:
            if self._model:
                return self._model
            if self.settings.model != "auto":
                self._model = self.settings.model
                return self._model
            data = await self._json_request("GET", "models", limit=1024 * 1024)
            rows = data.get("data")
            if not isinstance(rows, list):
                raise ModelError("image_invalid_catalog")
            candidates = []
            for row in rows:
                name = row.get("id") if isinstance(row, dict) else None
                if not isinstance(name, str):
                    continue
                lower = name.lower()
                if "gemini" not in lower or not ("image" in lower or "banana" in lower):
                    continue
                try:
                    ImageGenerationSettings(name)
                except ValueError:
                    continue
                candidates.append(name)
            if not candidates:
                raise ModelError("image_model_unavailable")
            # Prefer a Flash image model when present; keep selection deterministic.
            selected = min(candidates, key=lambda name: ("flash" not in name.lower(), name))
            self._model = selected
            return selected

    async def generate(self, prompt: str) -> GeneratedImage:
        with operation_trace():
            start = time.monotonic()
            emit("image_generation.started", timeout_seconds=self.settings.timeout)
            try:
                if (
                    not isinstance(prompt, str)
                    or not prompt.strip()
                    or len(prompt) > MAX_PROMPT_CHARS
                ):
                    raise ModelError("image_invalid_prompt")
                # One deadline includes model selection and the native generation request.
                async with asyncio.timeout(self.settings.timeout):
                    model = await self.select_model()
                    emit("image_generation.request.started", model=model)
                    data = await self._json_request(
                        "POST",
                        self.native_base + quote(model, safe="") + ":generateContent",
                        payload={
                            "systemInstruction": {
                                "parts": [{"text": "根据用户的描述生成且只返回一张最终图片。"}]
                            },
                            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                            "generationConfig": {
                                "candidateCount": 1,
                                "responseModalities": ["TEXT", "IMAGE"],
                            },
                        },
                    )
                    result = decode_image(data)
            except TimeoutError:
                emit(
                    "image_generation.failed",
                    level="WARNING",
                    error_code="image_connection_or_timeout",
                    elapsed_ms=elapsed_ms(start),
                )
                raise ModelError("image_connection_or_timeout") from None
            except ModelError as error:
                emit(
                    "image_generation.failed",
                    level="WARNING",
                    error_code=str(error),
                    elapsed_ms=elapsed_ms(start),
                )
                raise
            except asyncio.CancelledError:
                emit("image_generation.cancelled", elapsed_ms=elapsed_ms(start))
                raise
            emit(
                "image_generation.completed",
                bytes_received=len(result.image),
                elapsed_ms=elapsed_ms(start),
            )
            return result


def decode_image(data: dict) -> GeneratedImage:
    try:
        if data.get("promptFeedback", {}).get("blockReason"):
            raise ModelError("image_blocked")
        candidates = data.get("candidates", [])
        if not candidates:
            raise ModelError("image_missing")
        candidate = candidates[0]
        if candidate.get("finishReason") not in {None, "STOP"}:
            raise ModelError("image_incomplete_or_blocked")
        parts = candidate.get("content", {}).get("parts", [])
        images: list[bytes] = []
        text: list[str] = []
        for part in parts:
            # Never send reasoning text or interim images to the group.
            if part.get("thought"):
                continue
            if isinstance(value := part.get("text"), str):
                text.append(value)
            inline = part.get("inlineData", part.get("inline_data"))
            if inline is None:
                continue
            encoded = inline["data"]
            if (
                not isinstance(encoded, str)
                or len(encoded) > (MAX_GENERATED_IMAGE_BYTES + 2) // 3 * 4
            ):
                raise ModelError("image_data_invalid_or_too_large")
            body = base64.b64decode(encoded, validate=True)
            mime = inline.get("mimeType", inline.get("mime_type"))
            if len(body) > MAX_GENERATED_IMAGE_BYTES:
                raise ModelError("image_data_invalid_or_too_large")
            if not (actual := sniff_image_type(body)) or mime != actual:
                raise ModelError("image_invalid_format")
            images.append(body)
        if not images:
            raise ModelError("image_missing")
        if len(images) != 1:
            raise ModelError("image_multiple_outputs")
        return GeneratedImage(images[0], "\n".join(text).strip()[:1500])
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ModelError("image_invalid_response") from None

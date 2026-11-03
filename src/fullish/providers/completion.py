"""Single bounded completion request, shared by optional response processing."""

import json

import httpx

from fullish.settings import ModelSettings


class CompletionClient:
    def __init__(
        self,
        settings: ModelSettings,
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            headers={"Authorization": "Bearer " + settings.api_key},
            transport=transport or httpx.AsyncHTTPTransport(uds=settings.unix_socket),
            timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def complete(
        self,
        messages: list[dict[str, object]],
        *,
        model: str,
        max_tokens: int,
        max_response_bytes: int,
    ) -> dict:
        async with self.client.stream(
            "POST",
            "chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False},
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > max_response_bytes:
                    raise ValueError("oversized_completion_response")
        return json.loads(body)["choices"][0]["message"]

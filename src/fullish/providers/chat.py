"""Authenticated model transport and bounded model fallback; never executes tools."""

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol, cast

import httpx

from fullish.errors import ModelError
from fullish.settings import ModelSettings
from fullish.telemetry import elapsed_ms, emit

FINAL_ANSWER_SECONDS = 15


@dataclass
class ChatSession:
    model: str
    used_fallback: bool = False


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    rejection_status: int = 0

    def ensure_success(self) -> None:
        if self.rejection_status:
            raise ModelError(f"http_{self.rejection_status}")


class ChatBackend(Protocol):
    settings: ModelSettings

    async def request(
        self,
        request: dict[str, object],
        session: ChatSession,
        *,
        deadline: float,
        final_answer: bool = False,
    ) -> ModelResponse: ...


class ChatProvider:
    def __init__(
        self, settings: ModelSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            headers={"Authorization": "Bearer " + settings.api_key},
            transport=transport or httpx.AsyncHTTPTransport(uds=settings.unix_socket),
            timeout=httpx.Timeout(settings.timeout, connect=10),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def available(self) -> bool:
        try:
            response = await self.client.get("models", timeout=10)
            response.raise_for_status()
            return any(item.get("id") == self.settings.model for item in response.json()["data"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return False

    async def request(
        self,
        request: dict[str, object],
        session: ChatSession,
        *,
        deadline: float,
        final_answer: bool = False,
    ) -> ModelResponse:
        """Switch provider once within the deadline, without replaying any tools."""
        while True:
            messages = cast(list[dict[str, object]], request["messages"])
            can_fallback = bool(
                not session.used_fallback
                and self.settings.fallback_model
                and self.settings.fallback_model != session.model
            )
            remaining = max(0, deadline - time.monotonic())
            budget = (
                remaining - min(FINAL_ANSWER_SECONDS, remaining / 3) if can_fallback else remaining
            )
            body = {**request, "model": session.model}
            if session.used_fallback:
                body["messages"] = _portable_messages(messages)
            start = time.monotonic()
            emit(
                "model.request.started",
                model=session.model,
                prompt_messages=len(messages),
                final_answer=final_answer,
            )
            try:
                async with asyncio.timeout(budget):
                    response = await self.client.post("chat/completions", json=body)
                    emit(
                        "model.response.received",
                        model=session.model,
                        status_code=response.status_code,
                        elapsed_ms=elapsed_ms(start),
                        final_answer=final_answer,
                    )
                    # Preserve the existing tools-unavailable handling after both models fail.
                    if not can_fallback and response.status_code in {400, 422}:
                        return ModelResponse({}, rejection_status=response.status_code)
                    response.raise_for_status()
                    message = response.json()["choices"][0]["message"]
                    calls, content = message.get("tool_calls"), message.get("content")
                    if final_answer and (
                        calls or (isinstance(content, str) and content.strip() == "<SILENCE>")
                    ):
                        raise ModelError("invalid_final_response")
                    if not calls and (not isinstance(content, str) or not content.strip()):
                        raise ModelError("empty_response")
                    return ModelResponse(message)
            except httpx.HTTPStatusError as error:
                reason = f"http_{error.response.status_code}"
                if not can_fallback:
                    raise
            except (httpx.RequestError, TimeoutError):
                reason = "connection_or_timeout"
                if not can_fallback:
                    raise
            except ModelError as error:
                reason = str(error)
                if not can_fallback:
                    raise
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                reason = "invalid_response"
                if not can_fallback:
                    raise ModelError(reason) from None
            previous = session.model
            session.model = self.settings.fallback_model
            session.used_fallback = True
            emit(
                "model.fallback.started",
                level="WARNING",
                from_model=previous,
                model=session.model,
                reason=reason,
                final_answer=final_answer,
            )

    async def complete_text(self, messages: list[dict[str, object]], *, max_tokens: int) -> str:
        response = await self.client.post(
            "chat/completions",
            json={
                "model": self.settings.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ModelError("empty_response")
        return content.strip()


def _portable_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep the tool protocol and results, excluding Gemini-only reasoning metadata."""
    portable = []
    for message in messages:
        item = {
            key: value
            for key, value in message.items()
            if key not in {"reasoning_content", "extra_content"}
        }
        calls = item.get("tool_calls")
        if isinstance(calls, list):
            item["tool_calls"] = [
                {key: call[key] for key in ("id", "type", "function")} for call in calls
            ]
        portable.append(item)
    return portable

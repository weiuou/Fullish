"""Exercise CPA authentication, model selection, failures and credential redaction."""

import asyncio
import json

import httpx
import pytest

from fullish.model import ChatModel, ModelError
from fullish.settings import ModelSettings, QQSettings


def test_authenticated_model_and_chat_contract() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash-high"}]})
        assert request.url.path == "/v1/chat/completions"
        data = json.loads(request.content)
        assert data["model"] == "gemini-3.8-flash-high"
        assert data["messages"][0]["role"] == "system"
        assert data["messages"][1]["content"] == "你好"
        assert data["stream"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "聊什么呢"}}]})

    async def run() -> None:
        model = ChatModel(
            ModelSettings("http://localhost/v1", "test-secret"),
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await model.available()
            assert (
                await model.reply([{"role": "user", "content": "你好"}], addressed=True)
                == "聊什么呢"
            )
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {"secret": "do-not-log-this"}, "http_401"),
        (429, {"error": "test-secret"}, "http_429"),
        (200, {"choices": []}, "invalid_response"),
        (200, {"choices": [{"message": {"content": None}}]}, "empty_response"),
    ],
)
def test_errors_never_include_upstream_bodies(status: int, body: dict, code: str) -> None:
    async def run() -> None:
        model = ChatModel(
            ModelSettings("http://localhost/v1", "test-secret"),
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
        )
        try:
            with pytest.raises(ModelError, match=f"^{code}$"):
                await model.reply([], addressed=True)
        finally:
            await model.close()

    asyncio.run(run())


def test_timeout_and_silence() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    async def run() -> None:
        model = ChatModel(
            ModelSettings("http://localhost/v1", "secret", timeout=0.01),
            transport=httpx.MockTransport(slow),
        )
        try:
            with pytest.raises(ModelError, match="connection_or_timeout"):
                await model.reply([], addressed=True)
        finally:
            await model.close()
        model = ChatModel(
            ModelSettings("http://localhost/v1", "secret"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"choices": [{"message": {"content": "<SILENCE>"}}]}
                )
            ),
        )
        try:
            assert await model.reply([], addressed=False) is None
            assert not await model.available()
        finally:
            await model.close()

    asyncio.run(run())


def test_settings_fail_closed_and_redact_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AI_BASE_URL", "AI_API_KEY", "QQ_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert ModelSettings.from_env() is None
    assert QQSettings.from_env() is None
    monkeypatch.setenv("AI_API_KEY", "sensitive-value")
    with pytest.raises(ValueError, match="together"):
        ModelSettings.from_env()
    monkeypatch.setenv("AI_BASE_URL", "http://public.example/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        ModelSettings.from_env()
    monkeypatch.setenv("AI_BASE_URL", "https://public.example/v1/")
    assert "sensitive-value" not in repr(ModelSettings.from_env())
    monkeypatch.setenv("QQ_ENABLED", "true")
    with pytest.raises(ValueError, match="QQ_APP_ID"):
        QQSettings.from_env()

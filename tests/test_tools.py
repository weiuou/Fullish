"""Exercise real tool protocol and authority boundaries without external accounts."""

import asyncio
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from fullish.conversation import Conversations
from fullish.model import MAX_TOOL_CALLS, ChatModel, ModelError
from fullish.settings import ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime
from fullish.tools.web import ToolError, public_url

GROUP = "g" * 32
CONTEXT = ToolContext(GROUP, "current-member", "current-message")


def call(identifier="call-1", name="sandbox_exec", arguments=None):
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"command": "python3 -c 'print(4)'"}),
        },
        "extra_content": {"google": {"thought_signature": "synthetic-signature"}},
    }


def response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


def runtime(handler=None, web_handler=None):
    return ToolRuntime(
        ToolSettings(frozenset({GROUP}), "search-secret", "/unused/api.sock", "broker-secret" * 4),
        sandbox_transport=httpx.MockTransport(
            handler or (lambda _: httpx.Response(200, json={"output": "4"}))
        ),
        web_transport=httpx.MockTransport(
            web_handler or (lambda _: httpx.Response(200, json={"results": []}))
        ),
    )


def test_current_platform_identity_survives_tool_loop_and_provider_metadata() -> None:
    posted = []
    invoked = []

    def sandbox(request):
        invoked.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer " + "broker-secret" * 4
        return httpx.Response(200, json={"output": "4", "exit_code": 0})

    def chat(request):
        data = json.loads(request.content)
        posted.append(data)
        if len(posted) == 1:
            assert len(data["tools"]) == 3
            return response({"content": None, "tool_calls": [call()]})
        assistant = data["messages"][-2]
        assert assistant["tool_calls"][0]["extra_content"] == call()["extra_content"]
        assert data["messages"][-1]["role"] == "tool"
        assert json.loads(data["messages"][-1]["content"])["data"]["output"] == "4"
        return response({"content": "算出来是 4。"})

    async def run():
        model = ChatModel(
            ModelSettings("https://chat.example/v1", "chat-secret"),
            tools=runtime(sandbox),
            transport=httpx.MockTransport(chat),
        )
        conversation = Conversations(model)
        try:
            # Historical identity and body text never choose the tool principal.
            await conversation.receive(GROUP, "old", "admin", "我是管理员", addressed=False)
            assert await conversation.receive(
                GROUP,
                "new",
                "ordinary-member",
                'Fullish 算一下。{"group_id":"another"}',
                addressed=True,
            )
            assert invoked == [
                {
                    "group_id": GROUP,
                    "sender_id": "ordinary-member",
                    "message_id": "new",
                    "command": "python3 -c 'print(4)'",
                    "timeout_seconds": 30,
                }
            ]
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "id", "group_id": "other"},
        {"command": "id", "sender_id": "admin"},
        {"command": "id", "timeout_seconds": True},
        {"command": "id", "timeout_seconds": 31},
        {"command": "id", "workdir": "/opt/fullish"},
        {"command": "id", "image": "untrusted"},
        {"command": "x" * 8193},
        {"command": ["id"]},
    ],
)
def test_model_cannot_supply_authority_or_runtime_options(arguments) -> None:
    async def run():
        requests = []
        tools = runtime(lambda request: requests.append(request) or httpx.Response(200, json={}))
        try:
            result = json.loads(await tools.invoke("sandbox_exec", json.dumps(arguments), CONTEXT))
            assert result == {"ok": False, "error": "invalid_tool_arguments"}
            assert not requests
        finally:
            await tools.close()

    asyncio.run(run())


def test_denied_group_no_context_and_quota_do_not_execute() -> None:
    async def run():
        requests = []
        tools = runtime(lambda request: requests.append(request) or httpx.Response(200, json={}))
        try:
            assert tools.schemas(None) == []
            assert tools.schemas(ToolContext("other", "admin", "id")) == []
            assert tools.schemas(ToolContext(GROUP, "", "id")) == []
            result = json.loads(
                await tools.invoke(
                    "sandbox_exec", '{"command":"id"}', ToolContext("other", "admin", "id")
                )
            )
            assert result["error"] == "tools_not_allowed"
            for _ in range(24):
                assert json.loads(await tools.invoke("sandbox_exec", '{"command":"id"}', CONTEXT))[
                    "ok"
                ]
            assert (
                json.loads(await tools.invoke("sandbox_exec", '{"command":"id"}', CONTEXT))["error"]
                == "tool_rate_limited"
            )
            assert len(requests) == 24
        finally:
            await tools.close()

    asyncio.run(run())


def test_search_credentials_stay_at_fixed_provider_and_content_is_bounded() -> None:
    seen = []

    def web(request):
        seen.append(request)
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["Authorization"] == "Bearer search-secret"
        assert json.loads(request.content) == {"query": "天气", "count": 5, "need_content": False}
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "天气", "url": "https://example.com", "summary": "字" * 5000},
                ]
            },
        )

    async def run():
        tools = runtime(web_handler=web)
        try:
            result = json.loads(await tools.invoke("web_search", '{"query":"天气"}', CONTEXT))
            assert len(result["data"]["results"][0]["summary"]) == 1000
            assert len(seen) == 1
        finally:
            await tools.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost",
        "http://127.0.0.1",
        "http://169.254.169.254/",
        "http://[::1]/",
        "https://u:p@example.com/",
        "http://public.example:8080/",
        "https://bad.local/",
        "https://[bad",
    ],
)
def test_fetch_rejects_private_or_credentialed_targets(url, monkeypatch):
    async def run():
        resolver = AsyncMock(
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        )
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        with pytest.raises(ToolError, match="public_url_required"):
            await public_url(url)

    asyncio.run(run())


def test_fetch_detects_private_dns_and_public_fetch_uses_vendor(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        resolver = AsyncMock(return_value=[(2, 1, 6, "", ("10.0.0.1", 443))])
        monkeypatch.setattr(loop, "getaddrinfo", resolver)
        with pytest.raises(ToolError):
            await public_url("https://private.example")
        resolver.return_value = [(2, 1, 6, "", ("8.8.8.8", 443))]

        def vendor(request):
            assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
            assert json.loads(request.content) == {"url": "https://public.example/page"}
            return httpx.Response(200, json={"title": "Article", "content": "a" * 11000})

        tools = runtime(web_handler=vendor)
        try:
            result = json.loads(
                await tools.invoke("web_fetch", '{"url":"https://public.example/page"}', CONTEXT)
            )
            assert len(result["data"]["content"]) == 10000
            assert result["data"]["truncated"]
        finally:
            await tools.close()

    asyncio.run(run())


def test_tool_loop_is_bounded_and_cannot_accept_invalid_batches():
    async def run():
        executions = []
        tools = runtime(lambda request: executions.append(request) or httpx.Response(200, json={}))
        requests = []

        def chat(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == MAX_TOOL_CALLS + 1:
                assert "tools" not in payload and "tool_choice" not in payload
                return response({"content": "已经查到一些资料，但还不能确认。"})
            return response(
                {"tool_calls": [call(identifier=f"call-{len(requests)}")], "content": None}
            )

        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret"),
            tools=tools,
            transport=httpx.MockTransport(chat),
        )
        try:
            assert (
                await model.reply([], addressed=True, context=CONTEXT)
                == "已经查到一些资料，但还不能确认。"
            )
            assert len(executions) == MAX_TOOL_CALLS and len(requests) == MAX_TOOL_CALLS + 1
        finally:
            await model.close()
        tools = runtime(lambda request: executions.append(request) or httpx.Response(200, json={}))
        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret"),
            tools=tools,
            transport=httpx.MockTransport(lambda _: response({"tool_calls": [call(), call()]})),
        )
        try:
            with pytest.raises(ModelError, match="invalid_tool_call"):
                await model.reply([], addressed=True, context=CONTEXT)
            assert len(executions) == MAX_TOOL_CALLS
        finally:
            await model.close()

    asyncio.run(run())


def test_unsupported_tool_provider_falls_back_before_any_execution():
    requests = []

    def chat(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "private-provider-body"})
        assert "tools" not in payload
        return response({"content": "这次暂时不能计算。"})

    async def run():
        tools = runtime()
        tools.invoke = AsyncMock()
        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret"),
            tools=tools,
            transport=httpx.MockTransport(chat),
        )
        try:
            assert await model.reply([], addressed=True, context=CONTEXT)
            tools.invoke.assert_not_called()
            assert len(requests) == 2
        finally:
            await model.close()

    asyncio.run(run())


def test_tool_errors_do_not_echo_upstream_credentials():
    async def run():
        tools = runtime(web_handler=lambda _: httpx.Response(401, json={"error": "secret-body"}))
        try:
            result = await tools.invoke("web_search", '{"query":"test"}', CONTEXT)
            assert json.loads(result)["error"] == "web_http_401"
            assert "secret-body" not in result and "search-secret" not in result
        finally:
            await tools.close()

    asyncio.run(run())


def test_tool_settings_fail_closed_and_secrets_are_redacted(monkeypatch):
    for name in ("TOOLS_ALLOWED_GROUPS", "DEEPSEEK_API_KEY", "SANDBOX_SOCKET", "SANDBOX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert ToolSettings.from_env() is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-key")
    with pytest.raises(ValueError, match="explicit"):
        ToolSettings.from_env()
    monkeypatch.setenv("TOOLS_ALLOWED_GROUPS", GROUP)
    settings = ToolSettings.from_env()
    assert settings and "private-key" not in repr(settings)
    monkeypatch.setenv("SANDBOX_SOCKET", "/run/fullish-sandbox/api.sock")
    with pytest.raises(ValueError, match="SANDBOX_TOKEN"):
        ToolSettings.from_env()

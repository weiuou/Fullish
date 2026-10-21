"""Provider failures switch once, preserve tool results, and stay within the turn deadline."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot import logger

from fullish.model import MAX_TOOL_CALLS, ChatModel, ModelError
from fullish.settings import ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime


def response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


@pytest.mark.parametrize("failure", ["http", "connection", "timeout", "empty", "invalid_json"])
def test_primary_failure_falls_back_on_same_cpa_within_deadline(failure):
    async def run():
        posted = []

        async def respond(request):
            assert request.headers["Authorization"] == "Bearer private-key"
            assert request.url == "https://cpa.example/v1/chat/completions"
            body = json.loads(request.content)
            posted.append(body)
            if len(posted) == 1:
                if failure == "http":
                    return httpx.Response(503, text="upstream-private-body")
                if failure == "connection":
                    raise httpx.ConnectError("upstream-private-body")
                if failure == "timeout":
                    await asyncio.Event().wait()
                if failure == "empty":
                    return response({"content": ""})
                return httpx.Response(200, text="private-invalid-json")
            assert body["messages"] == posted[0]["messages"]
            return response({"content": "备用模型的回复"})

        model = ChatModel(
            ModelSettings("https://cpa.example/v1", "private-key", timeout=0.3),
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await model.reply([], addressed=True) == "备用模型的回复"
            assert [p["model"] for p in posted] == ["gemini-3.8-flash-high", "gpt-6-luna"]
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("primary", ["gemini-3.8-flash-high", "gpt-6-luna"])
def test_healthy_reply_or_silence_does_not_switch(primary):
    async def run():
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return response({"content": "<SILENCE>"})

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key", model=primary),
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await model.reply([], addressed=False) is None
            assert len(requests) == 1
        finally:
            await model.close()

    asyncio.run(run())


def tool(identifier):
    return {
        "id": str(identifier),
        "type": "function",
        "function": {"name": "sandbox_exec", "arguments": '{"command":"echo ok"}'},
        "extra_content": {"google": {"thought_signature": "private-signature"}},
    }


@pytest.mark.parametrize("finalizing", [False, True])
def test_switch_after_tools_preserves_results_without_replaying_actions(finalizing):
    async def run():
        posted = []
        context = ToolContext("g" * 32, "user", "message")
        runtime = ToolRuntime(ToolSettings(frozenset({context.group_id}), "fake"))
        runtime.invoke = AsyncMock(return_value='{"ok":true,"data":{"output":"done"}}')
        count = MAX_TOOL_CALLS if finalizing else 1

        def respond(request):
            body = json.loads(request.content)
            posted.append(body)
            if len(posted) == 1:
                return response(
                    {
                        "tool_calls": [tool(i) for i in range(count)],
                        "reasoning_content": "private-reasoning",
                    }
                )
            if len(posted) == 2:
                return httpx.Response(503)
            assert body["model"] == "gpt-6-luna"
            assert "private-signature" not in json.dumps(body)
            assert "private-reasoning" not in json.dumps(body)
            results = [m for m in body["messages"] if m["role"] == "tool"]
            assert len(results) == count
            assert all(json.loads(m["content"])["data"]["output"] == "done" for m in results)
            assert ("tools" not in body) == finalizing
            return response({"content": "已依据工具结果回答"})

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            tools=runtime,
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await model.reply([], addressed=True, context=context) == "已依据工具结果回答"
            assert runtime.invoke.await_count == count
            assert len(posted) == 3
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "primary,fallback,expected",
    [
        ("gemini-3.8-flash-high", "gpt-6-luna", 2),
        ("gpt-6-luna", "gpt-6-luna", 1),
        ("gemini-3.8-flash-high", "", 1),
    ],
)
def test_both_models_failed_or_fallback_disabled_does_not_loop(primary, fallback, expected):
    async def run():
        posted = []

        def respond(request):
            posted.append(json.loads(request.content))
            return httpx.Response(503)

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key", model=primary, fallback_model=fallback),
            transport=httpx.MockTransport(respond),
        )
        try:
            with pytest.raises(ModelError, match="^http_503$"):
                await model.reply([], addressed=True)
            assert len(posted) == expected
        finally:
            await model.close()

    asyncio.run(run())


def test_fallback_model_configuration(monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://cpa.example/v1")
    monkeypatch.setenv("AI_API_KEY", "key")
    monkeypatch.delenv("AI_FALLBACK_MODEL", raising=False)
    settings = ModelSettings.from_env()
    assert settings and settings.fallback_model == "gpt-6-luna"
    monkeypatch.setenv("AI_FALLBACK_MODEL", "")
    settings = ModelSettings.from_env()
    assert settings and settings.fallback_model == ""


def test_fallback_stays_for_remaining_tool_rounds_then_resets_and_logs_metadata():
    async def run():
        posted, records = [], []
        context = ToolContext("g" * 32, "user", "message")
        runtime = ToolRuntime(ToolSettings(frozenset({context.group_id}), "fake"))
        runtime.invoke = AsyncMock(return_value='{"ok":true,"data":{"output":"ok"}}')

        def collect(message):
            if record := message.record["extra"].get("flow"):
                records.append(record)

        sink = logger.add(collect)

        def respond(request):
            data = json.loads(request.content)
            posted.append(data["model"])
            if len(posted) == 1:
                return httpx.Response(503, text="private-upstream-body")
            if len(posted) == 2:
                return response({"tool_calls": [tool("one")]})
            return response({"content": "ok"})

        model = ChatModel(
            ModelSettings("http://localhost/v1", "private-key"),
            tools=runtime,
            transport=httpx.MockTransport(respond),
        )
        try:
            assert await model.reply([], addressed=True, context=context) == "ok"
            assert await model.reply([], addressed=True, context=context) == "ok"
            assert posted == [
                "gemini-3.8-flash-high",
                "gpt-6-luna",
                "gpt-6-luna",
                "gemini-3.8-flash-high",
            ]
            events = [r for r in records if r["stage"] == "model.fallback.started"]
            assert len(events) == 1 and events[0]["reason"] == "http_503"
            assert events[0]["model"] == "gpt-6-luna"
            assert "private" not in json.dumps(records)
        finally:
            await model.close()
            logger.remove(sink)

    asyncio.run(run())

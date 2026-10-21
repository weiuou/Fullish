"""Tool exhaustion always terminates with a final answer, including hostile providers."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from fullish.model import INCOMPLETE_REPLY, MAX_TOOL_CALLS, ChatModel
from fullish.settings import ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime


def tool(identifier):
    return {
        "id": str(identifier),
        "type": "function",
        "function": {
            "name": "sandbox_exec",
            "arguments": '{"command":"true"}',
        },
    }


def response(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


@pytest.mark.parametrize(
    "ending", ["text", "http_error", "tool_again", "silence", "empty", "timeout"]
)
def test_tool_limit_reserves_one_tools_free_final_attempt(ending):
    async def run():
        context = ToolContext("g" * 32, "u", "m")
        runtime = ToolRuntime(ToolSettings(frozenset({context.group_id}), "fake"))
        runtime.invoke = AsyncMock(return_value='{"ok":true,"data":{"output":"partial"}}')
        posted = []

        async def chat(request):
            data = json.loads(request.content)
            posted.append(data)
            if len(posted) == 1:
                return response(
                    {"content": None, "tool_calls": [tool(i) for i in range(MAX_TOOL_CALLS)]}
                )
            assert "tools" not in data and "tool_choice" not in data
            assert len([m for m in data["messages"] if m["role"] == "tool"]) == MAX_TOOL_CALLS
            if ending == "http_error":
                return httpx.Response(503, json={"private": "secret"})
            if ending == "tool_again":
                return response({"content": None, "tool_calls": [tool("extra")]})
            if ending == "timeout":
                await asyncio.sleep(1)
            return response(
                {
                    "content": {
                        "text": "只能确认已完成的部分。",
                        "silence": "<SILENCE>",
                        "empty": "",
                    }.get(ending, "late")
                }
            )

        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret", timeout=0.1),
            tools=runtime,
            transport=httpx.MockTransport(chat),
        )
        try:
            result = await model.reply([], addressed=True, context=context)
            assert result == ("只能确认已完成的部分。" if ending == "text" else INCOMPLETE_REPLY)
            assert runtime.invoke.await_count == MAX_TOOL_CALLS
            assert len(posted) == (2 if ending == "text" else 3)
            if ending != "text":
                assert posted[-1]["model"] == "gpt-6-luna"
        finally:
            await model.close()

    asyncio.run(run())


def test_oversized_batch_is_not_executed_and_still_gets_a_final_answer():
    async def run():
        calls = 0

        def chat(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return response({"tool_calls": [tool(i) for i in range(MAX_TOOL_CALLS + 1)]})
            assert "tools" not in json.loads(request.content)
            return response({"content": "这次没能完成，不能确认。"})

        context = ToolContext("g" * 32, "u", "m")
        runtime = ToolRuntime(ToolSettings(frozenset({context.group_id}), "fake"))
        runtime.invoke = AsyncMock()
        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret"),
            tools=runtime,
            transport=httpx.MockTransport(chat),
        )
        try:
            assert (
                await model.reply([], addressed=True, context=context) == "这次没能完成，不能确认。"
            )
            runtime.invoke.assert_not_awaited()
        finally:
            await model.close()

    asyncio.run(run())


def test_tool_timeout_closes_pending_protocol_and_keeps_time_for_final_answer():
    async def run():
        context = ToolContext("g" * 32, "u", "m")
        runtime = ToolRuntime(ToolSettings(frozenset({context.group_id}), "fake"))

        async def slow(*args, **kwargs):
            await asyncio.sleep(1)

        runtime.invoke = AsyncMock(side_effect=slow)
        posted = []

        def chat(request):
            data = json.loads(request.content)
            posted.append(data)
            if len(posted) == 1:
                return response({"tool_calls": [tool(1)]})
            assert "tools" not in data
            result = next(m for m in data["messages"] if m["role"] == "tool")
            assert json.loads(result["content"])["error"] == "tool_time_limit"
            return response({"content": "工具没来得及完成，这次还没查清。"})

        model = ChatModel(
            ModelSettings("https://chat.example/v1", "secret", timeout=0.09),
            tools=runtime,
            transport=httpx.MockTransport(chat),
        )
        try:
            assert (
                await model.reply([], addressed=True, context=context)
                == "工具没来得及完成，这次还没查清。"
            )
            assert len(posted) == 2
        finally:
            await model.close()

    asyncio.run(run())

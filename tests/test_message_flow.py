"""Follow real dispatch, model HTTP and send acknowledgements without live QQ traffic."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot import logger
from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.bot import _check_at_me
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.adapters.qq.models import Dispatch
from nonebot.drivers import Response

from fullish.conversation import Conversations
from fullish.model import ChatModel
from fullish.qq import GroupOnlyAdapter, handle_group
from fullish.settings import ModelSettings
from fullish.telemetry import emit, message_trace, trace_id


@pytest.fixture
def flow():
    records = []

    def collect(message):
        if record := message.record["extra"].get("flow"):
            records.append(record)

    sink = logger.add(collect)
    try:
        yield records
    finally:
        logger.remove(sink)


def event(message_id: str = "message-private-id", content: str = ""):
    return GroupAtMessageCreateEvent.model_validate(
        {
            "id": message_id,
            "content": content,
            "timestamp": "2026-10-19T16:00:00Z",
            "group_id": "group-private-id",
            "group_openid": "group-private-id",
            "author": {
                "id": "sender-private-id",
                "bot": False,
                "member_openid": "sender-private-id",
                "member_role": "member",
            },
        }
    )


@pytest.mark.parametrize("content", ["", "  \t"])
def test_bare_mention_flows_from_dispatch_to_accepted_reply(flow, content) -> None:
    async def run() -> None:
        payloads = []

        def respond(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "reply-private"}}]}
            )

        model = ChatModel(
            ModelSettings("http://localhost/v1", "credential-private"),
            transport=httpx.MockTransport(respond),
        )
        conversations = Conversations(model)
        adapter = object.__new__(GroupOnlyAdapter)
        adapter.tasks = set()
        bot = Mock(spec=Bot)
        bot.self_info = Mock(id="bot-private-id")
        bot.send = AsyncMock()

        async def handle(incoming):
            _check_at_me(bot, incoming)
            await handle_group(conversations, bot, incoming)

        bot.handle_event = AsyncMock(side_effect=handle)
        incoming = event(content=content)
        payload = Dispatch.model_validate(
            {"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "d": incoming.model_dump()}
        )
        try:
            adapter.dispatch_event(bot, payload)
            await asyncio.gather(*adapter.tasks)
            assert json.loads(payloads[0]["messages"][1]["content"])["text"] == "（@了 Fullish）"
            bot.send.assert_awaited_once()
            assert [turn["role"] for turn in conversations.groups[incoming.group_openid].turns] == [
                "user",
                "assistant",
            ]
            stages = [record["stage"] for record in flow]
            expected = [
                "qq.event.received",
                "qq.event.dispatched",
                "qq.message.parsed",
                "conversation.received",
                "conversation.normalized",
                "context.recorded",
                "conversation.generating",
                "model.preparing",
                "model.request.started",
                "model.response.received",
                "model.completed",
                "qq.send.started",
                "qq.send.succeeded",
                "context.recorded",
                "qq.message.completed",
            ]
            assert stages == expected
            assert len({record["trace_id"] for record in flow}) == 1
            assert flow[-1]["outcome"] == "sent"
            assert flow[2]["text_chars"] == 0
            assert not trace_id()  # dispatch and handler restore the caller's context

            adapter.dispatch_event(bot, payload)
            await asyncio.gather(*adapter.tasks)
            assert any(record.get("reason") == "duplicate" for record in flow)
            bot.send.assert_awaited_once()
            assert flow[-1]["outcome"] == "no_reply"
            serialized = json.dumps(flow)
            for private in (
                "credential-private",
                "reply-private",
                "group-private-id",
                "sender-private-id",
                "message-private-id",
            ):
                assert private not in serialized
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["model_failure", "silence", "send_failure", "timeout"])
def test_failure_or_silence_is_explained_without_recording_a_sent_reply(flow, case) -> None:
    async def run() -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            if case == "timeout":
                await asyncio.Event().wait()
            if case == "model_failure":
                return httpx.Response(503, text="upstream-body-private")
            content = "<SILENCE>" if case == "silence" else "reply-private"
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        model = ChatModel(
            ModelSettings("http://localhost/v1", "credential-private", timeout=0.05),
            transport=httpx.MockTransport(respond),
        )
        conversations = Conversations(model)
        bot = Mock(spec=Bot)
        bot.send = AsyncMock()
        if case == "send_failure":
            bot.send.side_effect = ActionFailed(
                Response(403, content=json.dumps({"code": 112233, "message": "qq-body-private"}))
            )
        try:
            await handle_group(conversations, bot, event(content="message-body-private"))
            assert conversations.active == 0
            assert not conversations.groups["group-private-id"].lock.locked()
            assert all(
                turn["role"] == "user" for turn in conversations.groups["group-private-id"].turns
            )
            assert len({record["trace_id"] for record in flow}) == 1
            assert flow[-1]["stage"] == "qq.message.completed"
            assert flow[-1]["outcome"] == ("no_reply" if case == "silence" else "failed")
            if case == "silence":
                assert any(record.get("outcome") == "silence" for record in flow)
                bot.send.assert_not_awaited()
            elif case == "send_failure":
                failure = next(record for record in flow if record["stage"] == "qq.message.failed")
                assert failure["phase"] == "send"
                assert failure["status_code"] == 403 and failure["error_code"] == 112233
            else:
                failure = next(record for record in flow if record["stage"] == "model.failed")
                assert failure["error_code"] == (
                    "http_503" if case == "model_failure" else "connection_or_timeout"
                )
                bot.send.assert_not_awaited()
            serialized = json.dumps(flow)
            for private in (
                "credential-private",
                "upstream-body-private",
                "qq-body-private",
                "message-body-private",
                "reply-private",
            ):
                assert private not in serialized
        finally:
            await model.close()

    asyncio.run(run())


def test_context_and_media_sources_can_be_followed_into_the_next_request(flow) -> None:
    async def run() -> None:
        model = ChatModel(
            ModelSettings("http://localhost/v1", "credential-private"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})
            ),
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(404))
        )
        conversations = Conversations(model)
        try:
            await conversations.receive(
                "g",
                "1",
                "u",
                "[图片]",
                addressed=False,
                images=["https://cdn.example/private?token=credential-private"],
            )
            await conversations.receive("g", "2", "u", "", addressed=True)
            sources = next(r for r in flow if r["stage"] == "conversation.generating")
            download = next(r for r in flow if r["stage"] == "image.download.completed")
            assert sources["context_trace_ids"] == [flow[0]["trace_id"], flow[-1]["trace_id"]]
            assert download["trace_id"] == flow[-1]["trace_id"]
            assert download["source_trace_id"] == flow[0]["trace_id"]
            assert download["outcome"] == "http_404"
            assert "credential-private" not in json.dumps(flow)
            assert "cdn.example" not in json.dumps(flow)
        finally:
            await model.close()

    asyncio.run(run())


def test_concurrent_traces_are_isolated_and_restored_after_cancellation(flow) -> None:
    async def run() -> None:
        started = asyncio.Event()

        async def worker(message_id: str):
            with message_trace("g", message_id):
                emit("test.started")
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    emit("test.stopped")

        with message_trace("g", "parent"):
            parent = trace_id()
            task = asyncio.create_task(worker("child"))
            await started.wait()
            assert trace_id() == parent
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            emit("test.parent")
        assert not trace_id()
        assert flow[0]["trace_id"] == flow[1]["trace_id"] != flow[2]["trace_id"]

    asyncio.run(run())


def test_decisions_explain_context_only_allowlist_and_cooldown(flow) -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        model.reply = AsyncMock(return_value="好")
        conversations = Conversations(model, allowed_groups=frozenset({"g"}), min_interval=0.01)
        try:
            await conversations.receive("other", "1", "u", "", addressed=True)
            await conversations.receive("g", "1", "u", "", addressed=False)
            assert conversations.groups["g"].turns[-1]["content"].endswith("[无文字内容]")
            assert await conversations.receive("g", "2", "u", "", addressed=True) == "好"
            await conversations.receive("g", "3", "u", "", addressed=True)
            assert [r["reason"] for r in flow if r["stage"] == "conversation.skipped"] == [
                "group_not_allowed",
                "context_only",
            ]
            assert any(
                r.get("reason") == "cooldown" for r in flow if r["stage"] == "conversation.waiting"
            )
            assert model.reply.await_count == 2
        finally:
            await model.close()

    asyncio.run(run())

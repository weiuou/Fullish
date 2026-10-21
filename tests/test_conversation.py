"""Group boundaries, duplicate suppression and concurrent message behavior."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from fullish.conversation import Conversations
from fullish.model import ChatModel, ModelError
from fullish.settings import ModelSettings


def test_group_context_deduplication_and_send_ack() -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        reply = AsyncMock(return_value="可以试试")
        model.reply = reply
        conversations = Conversations(model, allowed_groups=frozenset({"a"}))
        try:
            assert await conversations.receive("other", "0", "u", "Fullish", addressed=True) is None
            assert not conversations.groups
            assert await conversations.receive("a", "1", "alice", "聊编程", addressed=False) is None
            assert await conversations.receive("a", "2", "bob", "Fullish 怎么看", addressed=False)
            turns = reply.call_args.args[0]
            assert turns[0]["sender_id"] == "alice" and turns[1]["sender_id"] == "bob"
            assert turns[0]["content"] == "聊编程" and turns[1]["content"] == "Fullish 怎么看"
            assert all(t["role"] == "user" for t in conversations.groups["a"].turns)
            conversations.sent("a", "可以试试")
            assert conversations.groups["a"].turns[-1]["role"] == "assistant"
            assert await conversations.receive("a", "2", "bob", "重复", addressed=True) is None
            assert reply.call_count == 1
        finally:
            await model.close()

    asyncio.run(run())


def test_busy_group_has_one_request_and_other_groups_are_isolated() -> None:
    async def run() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def respond(*args, **kwargs):
            started.set()
            await finish.wait()
            return "好"

        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        reply = AsyncMock(side_effect=respond)
        model.reply = reply
        conversations = Conversations(model)
        try:
            first = asyncio.create_task(conversations.receive("a", "1", "u", "hi", addressed=True))
            await started.wait()
            assert await conversations.receive("a", "2", "v", "hello", addressed=True) is None
            assert await conversations.receive("b", "1", "w", "旁观", addressed=False) is None
            finish.set()
            assert await first == "好"
            assert reply.call_count == 1
            assert len(reply.call_args.args[0]) == 1
            assert conversations.active == 0
            assert "w" not in str(conversations.groups["a"].turns)
        finally:
            await model.close()

    asyncio.run(run())


def test_image_only_messages_join_context_and_reach_the_model() -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        reply = AsyncMock(return_value="这图有点意思")
        model.reply = reply
        conversations = Conversations(model)
        try:
            # A sticker or picture without text is still worth remembering.
            assert (
                await conversations.receive(
                    "a", "1", "alice", "[图片]", addressed=False, images=["http://cdn/1.jpg"]
                )
                is None
            )
            assert await conversations.receive("a", "2", "bob", "Fullish 看下", addressed=False)
            turns = reply.call_args.args[0]
            assert turns[0]["content"] == "[图片]"
            assert turns[0]["sender_id"] == "alice"
            assert turns[0]["images"] == ["http://cdn/1.jpg"]
            assert turns[1]["images"] == []
            # Messages without text are retained even without a mention or image.
            assert await conversations.receive("a", "3", "bob", "   ", addressed=False) is None
            assert conversations.groups["a"].turns[-1]["content"] == "[无文字内容]"
        finally:
            await model.close()

    asyncio.run(run())


def test_only_recent_images_are_forwarded_and_budget_is_trimmed() -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        reply = AsyncMock(return_value="嗯")
        model.reply = reply
        conversations = Conversations(model)
        try:
            for index in range(7):
                await conversations.receive(
                    "a",
                    str(index),
                    "alice",
                    "[图片]",
                    addressed=False,
                    images=[f"http://cdn/{index}.jpg"],
                )
            group = conversations.groups["a"]
            group.turns[-1]["created"] -= 151
            assert await conversations.receive("a", "7", "bob", "hi", addressed=True)
            history = reply.call_args.args[0]
            assert [url for item in history for url in item["images"]] == [
                f"http://cdn/{i}.jpg" for i in (2, 3, 4, 5)
            ]
            assert history[0]["images"] == []
            assert history[6]["images"] == []
            assert group.turns[0]["images"] == ["http://cdn/0.jpg"]

            for index in range(80):
                conversations.sent("a", f"回复 {index} " + "字" * 190)
            total = sum(len(item["content"]) for item in group.turns)
            assert len(group.turns) <= 60
            assert total <= 6000
            assert group.turns[-1]["content"].endswith("回复 79 " + "字" * 190)
        finally:
            await model.close()

    asyncio.run(run())


def test_images_keep_their_senders_when_one_download_fails() -> None:
    png = b"\x89PNG\r\n\x1a\n"
    payloads = []

    def media(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/expired.png":
            return httpx.Response(404)
        return httpx.Response(200, content=png + request.url.path.encode())

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "看到了"}}]})

    async def run() -> None:
        model = ChatModel(
            ModelSettings("http://localhost/v1", "fake-key"),
            transport=httpx.MockTransport(respond),
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(transport=httpx.MockTransport(media))
        conversations = Conversations(model)
        try:
            await conversations.receive(
                "g",
                "1",
                "alice",
                "[图片][图片]",
                addressed=False,
                images=["https://cdn.example/alice.png", "https://cdn.example/expired.png"],
            )
            await conversations.receive(
                "g",
                "2",
                "bob",
                "[图片]",
                addressed=False,
                images=["https://cdn.example/bob.png"],
            )
            assert await conversations.receive(
                "g", "3", "carol", "Fullish，Alice 发的是哪张？", addressed=True
            )
            messages = payloads[0]["messages"]
            for index, sender in ((1, "alice"), (2, "bob")):
                content = messages[index]["content"]
                envelope = json.loads(content[0]["text"])
                assert envelope["sender"]["id"] == sender
                assert envelope["text"] in {"[图片][图片]", "[图片]"}
                assert len(content) == 2
                data = content[1]["image_url"]["url"].split(",", 1)[1]
                assert base64.b64decode(data) == png + f"/{sender}.png".encode()
            assert messages[3]["role"] == "user"
            envelope = json.loads(messages[3]["content"])
            assert envelope["sender"]["id"] == "carol"
            assert envelope["text"] == "Fullish，Alice 发的是哪张？"
        finally:
            await model.close()

    asyncio.run(run())


def test_media_timeout_closes_stream_and_releases_conversation_capacity() -> None:
    async def run() -> None:
        started = asyncio.Event()
        wait_forever = asyncio.Event()

        class SlowImage(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                started.set()
                await wait_forever.wait()
                yield b"\x89PNG\r\n\x1a\n"

            async def aclose(self) -> None:
                self.closed = True

        stream = SlowImage()
        posted = []

        def respond(request: httpx.Request) -> httpx.Response:
            posted.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        model = ChatModel(
            ModelSettings("http://localhost/v1", "fake-key", timeout=0.05),
            transport=httpx.MockTransport(respond),
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        )
        conversations = Conversations(model)
        task = asyncio.create_task(
            conversations.receive(
                "g", "1", "alice", "[图片]", addressed=True, images=["https://cdn.example/slow.png"]
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            assert conversations.active == 1
            assert conversations.groups["g"].lock.locked()
            with pytest.raises(ModelError, match="^connection_or_timeout$"):
                await asyncio.wait_for(task, timeout=1)
            assert stream.closed
            assert conversations.active == 0
            assert not conversations.groups["g"].lock.locked()
            assert not posted
            # A text request in another group can use the released capacity.
            assert (
                await conversations.receive("other", "1", "bob", "Fullish", addressed=True) == "ok"
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await model.close()

    asyncio.run(run())


def test_model_failure_releases_capacity() -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))
        model.reply = AsyncMock(side_effect=ModelError("http_503"))
        conversations = Conversations(model)
        try:
            try:
                await conversations.receive("a", "1", "u", "hi", addressed=True)
            except ModelError:
                pass
            assert conversations.active == 0
            assert not conversations.groups["a"].lock.locked()
        finally:
            await model.close()

    asyncio.run(run())

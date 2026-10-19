"""Group boundaries, duplicate suppression and concurrent message behavior."""

import asyncio
from unittest.mock import AsyncMock

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
            assert "alice" in turns[0]["content"] and "bob" in turns[1]["content"]
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

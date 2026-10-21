"""FIFO delivery, bounded backpressure and draining without real QQ traffic."""

import asyncio
from unittest.mock import AsyncMock, Mock

from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from fullish.conversation import Conversations
from fullish.message_queue import GroupMessageQueue
from fullish.model import ChatModel
from fullish.qq import handle_group
from fullish.settings import ModelSettings
from fullish.telemetry import trace_id


def test_fifo_keeps_generation_send_ack_and_context_before_next_mention():
    async def run():
        generating = asyncio.Event()
        finish_generation = asyncio.Event()
        sending = asyncio.Event()
        finish_send = asyncio.Event()
        calls = []
        sent = []
        model = ChatModel(ModelSettings("http://localhost/v1", "test"))

        async def respond(turns, **kwargs):
            calls.append(turns)
            if len(calls) == 1:
                generating.set()
                await finish_generation.wait()
            return "reply-" + str(len(calls))

        async def send(event, message):
            if not sent:
                sending.set()
                await finish_send.wait()
            sent.append(event.id)

        model.reply = AsyncMock(side_effect=respond)
        conversations = Conversations(model, min_interval=0)
        bot = Mock(spec=Bot)
        bot.send = AsyncMock(side_effect=send)
        queue = GroupMessageQueue()

        def event(identifier, sender, text):
            incoming = GroupAtMessageCreateEvent.model_validate(
                {
                    "id": identifier,
                    "content": text,
                    "timestamp": "2026-10-21T14:00:00Z",
                    "group_id": "g",
                    "group_openid": "g",
                    "author": {
                        "id": sender,
                        "bot": False,
                        "member_openid": sender,
                        "member_role": "member",
                    },
                }
            )
            incoming.to_me = True
            return incoming

        first = event("one", "alice", "第一句")
        second = event("two", "bob", "第二句")
        try:
            await queue.submit("g", "one", lambda: handle_group(conversations, bot, first))
            await generating.wait()
            await queue.submit("g", "two", lambda: handle_group(conversations, bot, second))
            assert len(calls) == 1
            finish_generation.set()
            await sending.wait()
            assert len(calls) == 1  # sending is part of the queued job
            finish_send.set()
            await queue.close()
            assert sent == ["one", "two"]
            assert len(calls) == 2
            assert [(t["role"], t["content"]) for t in calls[1]] == [
                ("user", "第一句"),
                ("assistant", "reply-1"),
                ("user", "第二句"),
            ]
            assert calls[1][-1]["sender_id"] == "bob"
            assert model.reply.call_args.kwargs["context"].message_id == "two"
        finally:
            finish_generation.set()
            finish_send.set()
            await queue.close()
            await model.close()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_full_queue_applies_backpressure_and_preserves_all_messages():
    async def run():
        queue = GroupMessageQueue(capacity=1)
        started = asyncio.Event()
        finish = asyncio.Event()
        handled = []

        async def process(identifier):
            if identifier == 1:
                started.set()
                await finish.wait()
            handled.append(identifier)

        await queue.submit("g", "1", lambda: process(1))
        await started.wait()
        await queue.submit("g", "2", lambda: process(2))
        third = asyncio.create_task(queue.submit("g", "3", lambda: process(3)))
        await asyncio.sleep(0)
        assert not third.done()
        finish.set()
        await third
        await queue.close()
        assert handled == [1, 2, 3]

    asyncio.run(asyncio.wait_for(run(), 3))


def test_different_groups_continue_and_worker_failure_does_not_poison_queue():
    async def run():
        queue = GroupMessageQueue()
        slow = asyncio.Event()
        other = asyncio.Event()
        continued = asyncio.Event()
        traces = []

        async def blocked():
            traces.append(trace_id())
            await slow.wait()
            raise RuntimeError("private-error")

        async def fast():
            traces.append(trace_id())
            other.set()

        async def next_message():
            traces.append(trace_id())
            continued.set()

        await queue.submit("a", "1", blocked)
        await queue.submit("a", "2", next_message)
        await queue.submit("b", "1", fast)
        await other.wait()
        assert not continued.is_set()
        slow.set()
        await queue.close()
        assert continued.is_set() and len(set(traces)) == 3

    asyncio.run(asyncio.wait_for(run(), 3))


def test_shutdown_drains_or_cancels_workers_without_leaking_tasks():
    async def run():
        queue = GroupMessageQueue(capacity=1)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def block():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pending = AsyncMock()
        await queue.submit("g", "1", block)
        await started.wait()
        await queue.submit("g", "2", pending)
        producer = asyncio.create_task(queue.submit("g", "3", pending))
        await asyncio.sleep(0)
        await queue.close(timeout=0.01)
        assert cancelled.is_set() and not queue.groups
        assert producer.done()
        await asyncio.gather(producer, return_exceptions=True)
        pending.assert_not_awaited()
        await queue.submit("g", "4", pending)
        pending.assert_not_awaited()

    asyncio.run(asyncio.wait_for(run(), 3))

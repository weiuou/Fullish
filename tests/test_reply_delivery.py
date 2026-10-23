"""Expression plans keep platform sequence, permission and confirmed-history semantics."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from nonebot import logger
from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.drivers import Response

from fullish.control import GroupControl
from fullish.conversation import Conversations
from fullish.qq import handle_group
from fullish.sending.qq import prepare_replies
from fullish.sending.schema import Image, OutgoingMessage, ReplyPlan, Text


def event():
    return GroupAtMessageCreateEvent.model_validate(
        {
            "id": "incoming",
            "content": "说说看",
            "timestamp": "2026-10-21T08:00:00Z",
            "group_id": "g",
            "group_openid": "g",
            "author": {"id": "u", "member_openid": "u", "bot": False, "member_role": "member"},
        }
    )


def text(value: str) -> OutgoingMessage:
    return OutgoingMessage((Text(value),))


def history(conversations: Conversations) -> list[str]:
    return [
        item["content"] for item in conversations.groups["g"].turns if item["role"] == "assistant"
    ]


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


def test_plan_uses_actual_qq_sequences_and_records_each_acknowledged_message(flow):
    async def run():
        plan = ReplyPlan(
            (
                text("终于过了 😂"),
                OutgoingMessage((Text("庆祝一下"), Image(b"synthetic-picture"))),
                text("先歇会儿吧"),
            )
        )
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)))
        incoming = event()
        incoming._reply_seq = 2
        sent = []

        async def post(**kwargs):
            # The last message must already be in history before the next send.
            assert len(history(conversations)) == len(sent)
            sent.append(kwargs)

        with (
            patch.object(
                Bot,
                "post_group_files",
                new=AsyncMock(return_value=SimpleNamespace(file_info="media")),
            ) as upload,
            patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=post)),
        ):
            await handle_group(conversations, object.__new__(Bot), incoming)
        assert [item["msg_seq"] for item in sent] == [3, 4, 5]
        assert [item["msg_id"] for item in sent] == ["incoming"] * 3
        assert [item["msg_type"] for item in sent] == [0, 7, 0]
        assert [item["content"] for item in sent] == ["终于过了 😂", "庆祝一下", "先歇会儿吧"]
        assert sent[1]["media"].file_info == "media"
        upload.assert_awaited_once_with(
            group_openid="g", srv_send_msg=False, file_type=1, file_data=b"synthetic-picture"
        )
        assert history(conversations) == ["终于过了 😂", "庆祝一下[图片]", "先歇会儿吧"]
        assert flow[-1]["outcome"] == "sent"
        assert flow[-1]["sent_count"] == 3
        assert flow[-1]["partial_sent"] is False

    asyncio.run(run())


@pytest.mark.parametrize("failure_stage", ["upload", "send"])
def test_failure_stops_remainder_and_keeps_only_accepted_history(flow, failure_stage):
    async def run():
        plan = ReplyPlan((text("收到"), OutgoingMessage((Image(b"picture"),)), text("补充")))
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)))
        failure = ActionFailed(Response(403, content=json.dumps({"code": 112233})))
        upload = AsyncMock(return_value=SimpleNamespace(file_info="media"))
        post = AsyncMock()
        if failure_stage == "upload":
            upload.side_effect = failure
        else:
            post.side_effect = [None, failure]
        incoming = event()
        with (
            patch.object(Bot, "post_group_files", new=upload),
            patch.object(Bot, "post_group_messages", new=post),
        ):
            await handle_group(conversations, object.__new__(Bot), incoming)
        assert history(conversations) == ["收到"]
        assert post.await_count == (1 if failure_stage == "upload" else 2)
        assert incoming._reply_seq == 2
        assert flow[-1]["outcome"] == "partial_sent"
        assert flow[-1]["sent_count"] == 1
        assert flow[-1]["partial_sent"] is True
        assert not any(item["stage"] == "qq.reply.fallback" for item in flow)

    asyncio.run(run())


def test_cancellation_propagates_after_preserving_earlier_acknowledgement(flow):
    async def run():
        plan = ReplyPlan((text("第一条"), text("第二条"), text("第三条")))
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)))
        pending = asyncio.Event()

        async def post(**kwargs):
            if kwargs["msg_seq"] == 2:
                pending.set()
                await asyncio.Event().wait()

        with patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=post)) as send:
            task = asyncio.create_task(handle_group(conversations, object.__new__(Bot), event()))
            await pending.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert history(conversations) == ["第一条"]
        assert send.await_count == 2
        assert flow[-1]["outcome"] == "cancelled"
        assert flow[-1]["sent_count"] == 1
        assert flow[-1]["partial_sent"] is True

    asyncio.run(run())


@pytest.mark.parametrize("feature", ["chat", "image"])
def test_permission_revocation_blocks_entire_remainder_including_text(tmp_path, flow, feature):
    async def run():
        control = GroupControl(tmp_path)
        control.policies.change(None, "chat", True)
        control.policies.change(None, "image", True)
        plan = ReplyPlan((text("收到"), text("这张图给你"), OutgoingMessage((Image(b"picture"),))))
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)), control=control)

        async def post(**kwargs):
            control.policies.change(None, feature, False)

        with (
            patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=post)) as send,
            patch.object(Bot, "post_group_files", new=AsyncMock()) as upload,
        ):
            await handle_group(conversations, object.__new__(Bot), event())
        send.assert_awaited_once()
        upload.assert_not_awaited()
        assert history(conversations) == ["收到"]
        assert flow[-1]["outcome"] == "partial_sent"
        assert any(item.get("reason") == "group_policy_changed" for item in flow)

    asyncio.run(run())


def test_permission_is_checked_again_after_preparing_local_files(tmp_path, flow):
    async def run():
        control = GroupControl(tmp_path)
        control.policies.change(None, "chat", True)
        control.policies.change(None, "image", True)
        plan = ReplyPlan((text("图在这里"), OutgoingMessage((Image(b"picture"),))))
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)), control=control)

        async def prepare(reply):
            messages = await prepare_replies(reply)
            control.policies.change(None, "image", False)
            return messages

        bot = Mock(spec=Bot)
        bot.send = AsyncMock()
        with patch("fullish.qq.prepare_replies", side_effect=prepare):
            await handle_group(conversations, bot, event())
        bot.send.assert_not_awaited()
        assert history(conversations) == []
        assert flow[-1]["outcome"] == "no_reply"

    asyncio.run(run())


def test_missing_later_file_prevents_earlier_text_delivery(tmp_path, flow):
    async def run():
        plan = ReplyPlan(
            (text("这张图给你"), OutgoingMessage((Image(tmp_path / "missing-private.png"),)))
        )
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)))
        with (
            patch.object(Bot, "post_group_messages", new=AsyncMock()) as send,
            patch.object(Bot, "post_group_files", new=AsyncMock()) as upload,
        ):
            await handle_group(conversations, object.__new__(Bot), event())
        send.assert_not_awaited()
        upload.assert_not_awaited()
        assert history(conversations) == []
        assert flow[-1]["outcome"] == "failed"
        assert flow[-1]["phase"] == "send"
        assert "missing-private" not in json.dumps(flow)

    asyncio.run(run())


@pytest.mark.parametrize("previous_attempts", [4, 5])
def test_insufficient_event_budget_does_not_truncate_plan(flow, previous_attempts):
    async def run():
        plan = ReplyPlan((text("前半句"), text("后半句")))
        conversations = Conversations(Mock(reply=AsyncMock(return_value=plan)))
        incoming = event()
        incoming._reply_seq = previous_attempts
        with patch.object(Bot, "post_group_messages", new=AsyncMock()) as send:
            await handle_group(conversations, object.__new__(Bot), incoming)
        send.assert_not_awaited()
        assert incoming._reply_seq == previous_attempts
        assert history(conversations) == []
        assert flow[-1]["outcome"] == "no_reply"
        assert any(item.get("reason") == "budget_exhausted" for item in flow)

    asyncio.run(run())

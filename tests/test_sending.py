"""Exercise actual QQ media upload/send assembly without live platform traffic."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from nonebot import logger
from nonebot.adapters.qq import Bot, MessageSegment
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.exception import ActionFailed
from nonebot.drivers import Response

from fullish.conversation import Conversations
from fullish.model import ChatModel
from fullish.qq import handle_group
from fullish.sending import Audio, Image, OutgoingMessage, Text
from fullish.sending.adapters import ADAPTERS
from fullish.sending.qq import build_message, send_reply


def event():
    return GroupAtMessageCreateEvent.model_validate(
        {
            "id": "incoming",
            "content": "发一下",
            "timestamp": "2026-10-21T08:00:00Z",
            "group_id": "g",
            "group_openid": "g",
            "author": {"id": "u", "member_openid": "u", "bot": False, "member_role": "member"},
        }
    )


@pytest.mark.parametrize("part_type", [Image, Audio])
@pytest.mark.parametrize("source_type", ["url", "bytes", "path"])
def test_media_sources_with_text_are_encoded_together(tmp_path, part_type, source_type):
    data = b"synthetic-media-bytes"
    source = "https://cdn.example/media?private-token=secret"
    if source_type == "bytes":
        source = data
    elif source_type == "path":
        source = tmp_path / "media.bin"
        source.write_bytes(data)
    reply = OutgoingMessage((Text('说明 <qqbot-at-user id="u" />'), part_type(source)))
    message = build_message(reply)
    kind = "image" if part_type is Image else "audio"
    assert [segment.type for segment in message] == [
        "text",
        kind if source_type == "url" else "file_" + kind,
    ]
    assert message[0].data["text"] == reply.text
    if source_type == "url":
        assert message[1].data["url"] == source
    else:
        assert message[1].data["content"] == data
    assert reply.context_text == reply.text + ("[图片]" if kind == "image" else "[语音]")


@pytest.mark.parametrize("part_type", [Image, Audio])
@pytest.mark.parametrize("source", ["", "local.png", "file:///tmp/local.png", "ftp://cdn/a", b""])
def test_unusable_media_sources_are_rejected(part_type, source):
    with pytest.raises(ValueError):
        part_type(source)


@pytest.mark.parametrize(
    "parts",
    [
        (),
        (Text(" \t"),),
        (Image(b"one"), Image(b"two")),
        (Image(b"one"), Audio(b"two")),
        (Audio(b"one"), Audio(b"two")),
    ],
)
def test_empty_messages_and_multiple_media_are_rejected(parts):
    with pytest.raises(ValueError):
        OutgoingMessage(parts)


def test_text_remains_literal_and_encoders_can_be_replaced():
    literal = '<qqbot-at-user id="123" /> [image:untrusted]'
    message = build_message(literal)
    assert len(message) == 1 and message[0].type == "text"
    assert message[0].data["text"] == literal
    reply = OutgoingMessage((Text("说明"), Image(b"data")))
    replacement = Mock(return_value=MessageSegment.image("https://cdn.example/replaced"))
    message = build_message(reply, adapters={**ADAPTERS, "image": replacement})
    replacement.assert_called_once_with(reply.parts[1])
    assert message[1].data["url"] == "https://cdn.example/replaced"
    with pytest.raises(ValueError, match="No sending adapter"):
        build_message(reply, adapters={"text": ADAPTERS["text"]})


@pytest.mark.parametrize("part_type, file_type", [(Image, 1), (Audio, 3)])
@pytest.mark.parametrize("with_text", [True, False])
def test_real_qq_sender_uploads_then_sends_one_message(part_type, file_type, with_text):
    async def run():
        # These concrete methods do not need credentials when the API calls are mocked.
        bot = object.__new__(Bot)
        incoming = event()
        calls = []

        async def upload(**kwargs):
            calls.append("upload")
            assert kwargs == {
                "group_openid": "g",
                "srv_send_msg": False,
                "file_type": file_type,
                "url": "https://cdn.example/media",
            }
            return SimpleNamespace(file_info="opaque-media-handle")

        async def send(**kwargs):
            calls.append("send")
            assert kwargs["group_openid"] == "g"
            assert kwargs["msg_id"] == "incoming"
            assert kwargs["msg_seq"] == 1
            assert kwargs["msg_type"] == 7
            assert kwargs["content"] == ("这是说明" if with_text else None)
            assert kwargs["media"].file_info == "opaque-media-handle"

        parts = (part_type("https://cdn.example/media"),)
        reply = OutgoingMessage((Text("这是说明"), *parts) if with_text else parts)
        with (
            patch.object(Bot, "post_group_files", new=AsyncMock(side_effect=upload)),
            patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=send)) as post,
        ):
            await send_reply(bot, incoming, reply)
            post.assert_awaited_once()
        assert calls == ["upload", "send"]

    asyncio.run(run())


@pytest.mark.parametrize("part_type, file_type", [(Image, 1), (Audio, 3)])
def test_media_bytes_reach_qq_upload(part_type, file_type):
    async def run():
        bot = object.__new__(Bot)
        with (
            patch.object(
                Bot,
                "post_group_files",
                new=AsyncMock(return_value=SimpleNamespace(file_info="handle")),
            ) as upload,
            patch.object(Bot, "post_group_messages", new=AsyncMock()) as post,
        ):
            await send_reply(bot, event(), OutgoingMessage((part_type(b"media-bytes"),)))
            upload.assert_awaited_once_with(
                group_openid="g", srv_send_msg=False, file_type=file_type, file_data=b"media-bytes"
            )
            post.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["success", "upload_failure", "send_failure", "missing_file"])
@pytest.mark.parametrize("part_type, marker", [(Image, "[图片]"), (Audio, "[语音]")])
def test_candidate_flows_through_handler_and_records_only_accepted_media(case, part_type, marker):
    async def run():
        records = []

        def collect(message):
            if record := message.record["extra"].get("flow"):
                records.append(record)

        model = Mock(spec=ChatModel)
        source = (
            Path("missing-private-media-file")
            if case == "missing_file"
            else "https://cdn.example/private?token=secret"
        )
        reply = OutgoingMessage((Text("说明文字"), part_type(source)))
        model.reply = AsyncMock(return_value=reply)
        conversations = Conversations(model)
        failure = ActionFailed(Response(403, content=json.dumps({"code": 112233})))
        upload = AsyncMock(return_value=SimpleNamespace(file_info="handle"))
        post = AsyncMock()
        if case == "upload_failure":
            upload.side_effect = failure
        elif case == "send_failure":
            post.side_effect = failure
        sink = logger.add(collect)
        try:
            with (
                patch.object(Bot, "post_group_files", new=upload),
                patch.object(Bot, "post_group_messages", new=post),
            ):
                await handle_group(conversations, object.__new__(Bot), event())
            history = conversations.groups["g"].turns
            if case == "success":
                assert history[-1]["role"] == "assistant"
                assert history[-1]["content"] == "说明文字" + marker
                assert history[-1]["images"] == []
                assert records[-1]["outcome"] == "sent"
            else:
                assert all(turn["role"] == "user" for turn in history)
                assert records[-1]["outcome"] == "failed"
                assert records[-1]["phase"] == "send"
            if case in {"upload_failure", "missing_file"}:
                post.assert_not_awaited()
            serialized = json.dumps(records, ensure_ascii=False)
            for private in ("secret", "cdn.example", "missing-private-media-file", "说明文字"):
                assert private not in serialized
        finally:
            logger.remove(sink)

    asyncio.run(run())


def test_cancelled_send_propagates_without_recording_reply():
    async def run():
        started = asyncio.Event()

        async def send(*args):
            started.set()
            await asyncio.Event().wait()

        model = Mock(spec=ChatModel)
        model.reply = AsyncMock(return_value=OutgoingMessage((Audio(b"voice"),)))
        conversations = Conversations(model)
        bot = Mock(spec=Bot)
        bot.send = AsyncMock(side_effect=send)
        task = asyncio.create_task(handle_group(conversations, bot, event()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(turn["role"] == "user" for turn in conversations.groups["g"].turns)

    asyncio.run(run())

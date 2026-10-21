"""Quoted QQ pictures survive collection, context selection and model serialization."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.bot import _check_at_me, _check_reply
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.adapters.qq.models import Dispatch
from nonebot.adapters.qq.models.qq import Attachment

from fullish.collection.qq import collect_group_message
from fullish.conversation import Conversations
from fullish.model import ChatModel
from fullish.qq import GroupOnlyAdapter, handle_group
from fullish.settings import ModelSettings

PNG = b"\x89PNG\r\n\x1a\nsynthetic-quoted-picture"


def quoted_event(*, content="评价下", author=None, attachments=None):
    return GroupAtMessageCreateEvent.model_validate(
        {
            "id": "current-message",
            "content": content,
            "timestamp": "2026-10-21T08:00:00Z",
            "group_id": "g",
            "group_openid": "g",
            "author": {
                "id": "current-author",
                "member_openid": "current-author",
                "bot": False,
                "member_role": "member",
            },
            "message_type": 103,
            "message_scene": {"ext": ["ref_msg_idx=quoted-message"], "source": "synthetic"},
            "msg_elements": [
                {
                    "msg_idx": "unrelated-message",
                    "content": "不要把这条当成被引用内容",
                    "attachments": [{"content_type": "image/png", "url": "https://cdn/wrong"}],
                },
                {
                    "msg_idx": "quoted-message",
                    "content": "原消息正文",
                    "author": author,
                    "attachments": attachments
                    if attachments is not None
                    else [
                        {
                            "content_type": "image/png",
                            "url": "https://cdn/quoted.png",
                            "width": 20,
                            "filename": "quoted.png",
                        }
                    ],
                },
            ],
        }
    )


def bot_mock():
    bot = Mock(spec=Bot)
    bot.self_info = Mock(id="bot-id", username="Fullish")
    bot.send = AsyncMock()
    return bot


@pytest.mark.parametrize("download_ok", [True, False])
def test_quoted_picture_reaches_model_through_real_dispatch(download_ok: bool) -> None:
    async def run() -> None:
        payloads = []
        downloads = []

        def respond(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "看到了"}}]})

        def media(request):
            downloads.append(str(request.url))
            return httpx.Response(200, content=PNG) if download_ok else httpx.Response(404)

        model = ChatModel(
            ModelSettings("http://localhost/v1", "fake-key"),
            transport=httpx.MockTransport(respond),
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(transport=httpx.MockTransport(media))
        conversations = Conversations(model)
        adapter = object.__new__(GroupOnlyAdapter)
        adapter.tasks = set()
        bot = bot_mock()

        async def handle(incoming):
            await _check_reply(bot, incoming)
            _check_at_me(bot, incoming)
            await handle_group(conversations, bot, incoming)

        bot.handle_event = AsyncMock(side_effect=handle)
        incoming = quoted_event(
            author={"id": "quoted-author", "member_openid": "quoted-openid", "username": "Alice"}
        )
        dispatch = Dispatch.model_validate(
            {"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "d": incoming.model_dump()}
        )
        try:
            adapter.dispatch_event(bot, dispatch)
            await asyncio.gather(*adapter.tasks)
            assert downloads == ["https://cdn/quoted.png"]
            content = payloads[0]["messages"][1]["content"]
            envelope = json.loads(content[0]["text"] if download_ok else content)
            assert envelope["sender"]["id"] == "current-author"
            assert envelope["text"] == "评价下"
            assert envelope["reply_to"] == {
                "reference_id": "quoted-message",
                "sender": {"id": "quoted-openid", "display_name": "Alice"},
                "text": "原消息正文[图片]",
            }
            if download_ok:
                images = [part for part in content if part["type"] == "image_url"]
                assert len(images) == 1
                assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == PNG
                assert any("被引用消息" in part.get("text", "") for part in content[1:])
            bot.send.assert_awaited_once()
        finally:
            await model.close()

    asyncio.run(run())


def test_quote_text_is_bounded_and_does_not_trigger_a_name_call():
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "fake-key"))
        model.reply = AsyncMock(return_value="好")
        conversations = Conversations(model)
        try:
            for index in range(5):
                incoming = quoted_event(content="评" * 100)
                incoming.id = str(index)
                incoming.to_me = False
                assert incoming.msg_elements
                incoming.msg_elements[1].content = "Fullish " + "原" * 2500
                await _check_reply(bot_mock(), incoming)
                assert await conversations.receive_message(collect_group_message(incoming)) is None
            model.reply.assert_not_awaited()
            group = conversations.groups["g"]
            assert len(group.turns) == 3
            total = 0
            for item in group.turns:
                source = item["message"]
                assert source and source.reply
                size = len(item["content"]) + len(source.reply.content.text)
                assert size == 2000
                assert source.reply.content.image_urls == ["https://cdn/quoted.png"]
                total += size
            assert total == 6000

            await conversations.receive("g", "call", "bob", "看看", addressed=True)
            history = model.reply.call_args.args[0]
            assert sum(len(item.get("reply_images", [])) for item in history) == 1
            # Old quotes remain text context but do not keep re-downloading media.
            for item in group.turns:
                item["created"] -= 151
            group.last_attempt -= 5
            await conversations.receive("g", "later-call", "bob", "再看看", addressed=True)
            history = model.reply.call_args.args[0]
            assert not any(item.get("reply_images") for item in history)
        finally:
            await model.close()

    asyncio.run(run())


def test_quoted_and_current_pictures_have_separate_source_labels():
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "fake-key"))
        model.download = AsyncMock(side_effect=lambda url, **kwargs: url)
        try:
            incoming = quoted_event()
            incoming.attachments = [Attachment(content_type="image/png", url="https://cdn/current")]
            await _check_reply(bot_mock(), incoming)
            message = collect_group_message(incoming)
            assert message.reply
            payload = await model._messages(
                [
                    {
                        "role": "user",
                        "content": message.content.text,
                        "sender_id": message.sender.id,
                        "reply": message.reply,
                        "reply_images": message.reply.content.image_urls,
                        "images": message.content.image_urls,
                    }
                ],
                addressed=True,
            )
            parts = payload[1]["content"]
            assert isinstance(parts, list)
            assert "被引用消息" in parts[1]["text"]
            assert parts[2]["image_url"]["url"] == "https://cdn/quoted.png"
            assert "当前消息" in parts[3]["text"]
            assert parts[4]["image_url"]["url"] == "https://cdn/current"
            envelope = json.loads(parts[0]["text"])
            assert envelope["reply_to"]["sender"] == {"id": None, "display_name": None}
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("author", "sender_id", "sender_name"),
    [(None, None, None), ({"username": "仅昵称"}, None, "仅昵称")],
)
def test_quote_preserves_missing_identity_and_attachment_metadata(author, sender_id, sender_name):
    async def run() -> None:
        incoming = quoted_event(
            author=author,
            attachments=[
                {"content_type": "image/png", "url": "https://cdn/quoted.png", "width": 20},
                {"content_type": "image/png", "filename": "missing.png"},
            ],
        )
        await _check_reply(bot_mock(), incoming)
        message = collect_group_message(incoming)
        assert message.content.text == "评价下"
        assert not message.content.image_urls
        assert message.reply is not None
        assert message.reply.sender_id == sender_id
        assert message.reply.sender_name == sender_name
        assert message.reply.content.text == "原消息正文[图片][图片]"
        assert message.reply.content.image_urls == ["https://cdn/quoted.png"]
        assert message.reply.content.parts[1].metadata["width"] == 20
        assert message.reply.content.parts[2].metadata["filename"] == "missing.png"

    asyncio.run(run())


def test_current_quote_gets_priority_over_recent_images_and_deduplicates_old_original():
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "fake-key"))
        model.reply = AsyncMock(return_value="看到了")
        conversations = Conversations(model)
        try:
            await conversations.receive(
                "g",
                "original",
                "alice",
                "原消息",
                addressed=False,
                images=["https://cdn/quoted.png"],
            )
            conversations.groups["g"].turns[0]["created"] -= 151
            await conversations.receive(
                "g",
                "recent",
                "bob",
                "其他图",
                addressed=False,
                images=[f"https://cdn/recent-{i}.png" for i in range(4)],
            )
            incoming = quoted_event()
            incoming.attachments = [
                # Keep current and quoted pictures distinguishable in the model input.
                Attachment(content_type="image/png", url=f"https://cdn/current-{i}.png")
                for i in range(4)
            ]
            await _check_reply(bot_mock(), incoming)
            await conversations.receive_message(collect_group_message(incoming))
            history = model.reply.call_args.args[0]
            assert history[-1]["reply_images"] == ["https://cdn/quoted.png"]
            assert len(history[-1]["images"]) == 3
            urls = [
                url
                for item in history
                for url in item.get("reply_images", []) + item.get("images", [])
            ]
            assert len(urls) == len(set(urls)) == 4
            assert history[0]["images"] == []
            assert history[1]["images"] == []
            # The original's copy is also skipped when it is still recent.
            conversations.groups["g"].turns[0]["created"] += 151
            conversations.groups["g"].last_attempt -= 5
            await conversations.receive("g", "next", "bob", "再看看", addressed=True)
            history = model.reply.call_args.args[0]
            urls = [
                url
                for item in history
                for url in item.get("reply_images", []) + item.get("images", [])
            ]
            assert urls.count("https://cdn/quoted.png") == 1
        finally:
            await model.close()

    asyncio.run(run())

"""Draft HTTP -> expression HTTP -> real QQ sender -> acknowledged conversation."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent

from fullish.conversation import Conversations
from fullish.model import ChatModel
from fullish.qq import handle_group
from fullish.sending.postprocess import ReplyPostprocessor
from fullish.settings import ModelSettings, ReplyPostprocessSettings


@pytest.mark.parametrize("expression_fails", [False, True])
def test_draft_to_expression_to_acknowledged_qq_messages(expression_fails):
    async def run():
        calls = []

        def respond(request):
            payload = json.loads(request.content)
            calls.append(payload["model"])
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer fake-key"
            if payload["model"] == "draft-model":
                content = "终于成功了，先歇会儿吧。"
            else:
                data = json.loads(payload["messages"][1]["content"])
                assert data["draft"] == [{"type": "text", "text": "终于成功了，先歇会儿吧。"}]
                assert data["context"][-1]["text"] == "终于部署成功了"
                if expression_fails:
                    return httpx.Response(500)
                content = json.dumps(
                    {
                        "messages": [
                            {"parts": [{"type": "text", "text": "终于过了！🎉"}]},
                            {"parts": [{"type": "text", "text": "先歇会儿吧"}]},
                        ]
                    },
                    ensure_ascii=False,
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        backend = ModelSettings("http://localhost/v1", "fake-key", model="draft-model")
        model = ChatModel(backend, transport=httpx.MockTransport(respond))
        processor = ReplyPostprocessor(
            backend,
            ReplyPostprocessSettings(model="expression-model"),
            transport=httpx.MockTransport(respond),
        )
        conversations = Conversations(model, postprocessor=processor)
        event = GroupAtMessageCreateEvent.model_validate(
            {
                "id": "incoming",
                "content": "终于部署成功了",
                "timestamp": "2026-10-23T08:00:00Z",
                "group_id": "g",
                "group_openid": "g",
                "author": {
                    "id": "u",
                    "member_openid": "u",
                    "bot": False,
                    "member_role": "member",
                },
            }
        )
        try:
            with patch.object(Bot, "post_group_messages", new=AsyncMock()) as post:
                await handle_group(conversations, object.__new__(Bot), event)
            expected = (
                ["终于成功了，先歇会儿吧。"] if expression_fails else ["终于过了！🎉", "先歇会儿吧"]
            )
            assert [call.kwargs["content"] for call in post.await_args_list] == expected
            assert [call.kwargs["msg_seq"] for call in post.await_args_list] == list(
                range(1, len(expected) + 1)
            )
            assert all(call.kwargs["msg_id"] == "incoming" for call in post.await_args_list)
            assert [
                turn["content"]
                for turn in conversations.groups["g"].turns
                if turn["role"] == "assistant"
            ] == expected
            assert calls == ["draft-model", "expression-model"]
        finally:
            await model.close()
            await processor.close()

    asyncio.run(run())

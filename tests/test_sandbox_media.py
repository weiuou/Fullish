"""Scoped binary exchange, visual tool output and existing-image QQ candidates."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from fullish.generation import GeneratedImage
from fullish.image_assets import ImageAssets
from fullish.model import ChatModel, ModelError
from fullish.sending import Image, OutgoingMessage, Text
from fullish.settings import ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime

GROUP = "g" * 32
CONTEXT = ToolContext(GROUP, "member", "message")
PNG = b"\x89PNG\r\n\x1a\nsynthetic-reference"
EDITED = b"\x89PNG\r\n\x1a\nsynthetic-sandbox-result"


def tools(backend):
    return ToolRuntime(
        ToolSettings(frozenset({GROUP}), sandbox_socket="/test.sock", sandbox_token="token-" * 8),
        sandbox_transport=httpx.MockTransport(backend),
    )


def test_sandbox_binary_roundtrip_uses_platform_identity_and_never_returns_base64_to_model():
    async def run():
        stored = {}

        def backend(request):
            payload = json.loads(request.content)
            assert payload["group_id"] == GROUP
            assert payload["sender_id"] == "member" and payload["message_id"] == "message"
            assert request.headers["Authorization"] == "Bearer " + "token-" * 8
            if request.url.path == "/files/write":
                stored[payload["path"]] = base64.b64decode(payload["data"])
                return httpx.Response(200, json={"path": payload["path"], "size": len(PNG)})
            return httpx.Response(
                200,
                json={
                    "path": payload["path"],
                    "size": len(PNG),
                    "data": base64.b64encode(stored[payload["path"]]).decode(),
                },
            )

        runtime, assets = tools(backend), ImageAssets()
        asset = assets.add(PNG)
        try:
            upload = json.loads(
                await runtime.invoke(
                    "sandbox_upload",
                    json.dumps({"image_id": asset.image_id}),
                    CONTEXT,
                    images=assets,
                )
            )
            assert upload["ok"] is True
            path = "imports/" + asset.filename
            assert stored[path] == PNG
            result = await runtime.invoke(
                "sandbox_download", json.dumps({"path": path}), CONTEXT, images=assets
            )
            downloaded = json.loads(result)
            assert downloaded["ok"] is True
            assert assets.get(downloaded["data"]["image_id"]).body == PNG
            assert base64.b64encode(PNG).decode() not in result
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("finish", ["send_image", "generate_image"])
def test_sandbox_result_is_viewed_before_send_or_reference_edit(finish):
    async def run():
        posted = []

        def backend(request):
            payload = json.loads(request.content)
            assert request.url.path == "/files/read"
            assert payload["path"] == "result.png"
            return httpx.Response(
                200,
                json={
                    "path": "result.png",
                    "size": len(PNG),
                    "data": base64.b64encode(PNG).decode(),
                },
            )

        def chat(request):
            payload = json.loads(request.content)
            posted.append(payload)
            if len(posted) == 1:
                name, args = "sandbox_download", {"path": "result.png"}
            else:
                result = json.loads(payload["messages"][-2]["content"])["data"]
                visual = payload["messages"][-1]["content"]
                assert base64.b64decode(visual[1]["image_url"]["url"].split(",", 1)[1]) == PNG
                name = finish
                args = (
                    {"image_id": result["image_id"], "caption": "处理好了"}
                    if finish == "send_image"
                    else {
                        "prompt": "保留主体，做表情包",
                        "caption": "处理好了",
                        "reference_image_ids": [result["image_id"]],
                    }
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": str(len(posted)),
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(args)},
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        generator = Mock(generate=AsyncMock(return_value=GeneratedImage(EDITED)), close=AsyncMock())
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(chat),
            tools=tools(backend),
            image_generator=generator if finish == "generate_image" else None,
        )
        try:
            result = await model.reply([], addressed=True, context=CONTEXT)
            assert result == OutgoingMessage(
                (Text("处理好了"), Image(PNG if finish == "send_image" else EDITED))
            )
            if finish == "send_image":
                generator.generate.assert_not_awaited()
            else:
                generator.generate.assert_awaited_once_with(
                    "保留主体，做表情包", reference_images=(PNG,)
                )
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("sandbox_upload", {"image_id": "img_unknown"}),
        ("sandbox_download", {"path": "x", "group_id": "another-group"}),
    ],
)
def test_unknown_assets_and_forged_group_identity_never_reach_broker(name, arguments):
    async def run():
        backend = Mock(return_value=httpx.Response(200))
        runtime = tools(backend)
        try:
            result = json.loads(
                await runtime.invoke(name, json.dumps(arguments), CONTEXT, images=ImageAssets())
            )
            assert result["ok"] is False
            backend.assert_not_called()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_image_ids_from_previous_reply_cannot_be_sent():
    async def run():
        previous = ImageAssets().add(PNG)

        def chat(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "send",
                                        "type": "function",
                                        "function": {
                                            "name": "send_image",
                                            "arguments": json.dumps(
                                                {"image_id": previous.image_id, "caption": ""}
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
            )

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(chat),
            tools=tools(lambda _: httpx.Response(500)),
        )
        try:
            with pytest.raises(ModelError, match="^image_reference_not_found$"):
                await model.reply([], addressed=True, context=CONTEXT)
        finally:
            await model.close()

    asyncio.run(run())

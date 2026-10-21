"""CPA native image contract, tool routing, failure boundaries and QQ delivery."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from nonebot import logger
from nonebot.adapters.qq import Bot
from test_sending import event

from fullish.conversation import Conversations
from fullish.generation import GeneratedImage
from fullish.generation.gemini import GeminiImageGenerator, decode_image
from fullish.model import ChatModel, ModelError
from fullish.qq import handle_group
from fullish.sending import Image, OutgoingMessage, Text
from fullish.settings import ImageGenerationSettings, ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime

PNG = b"\x89PNG\r\n\x1a\nsynthetic-generated-picture"
MODEL = "gemini-3.1-flash-image"


def image_response(*, body=PNG, mime="image/png"):
    return {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"thought": True, "text": "private-reasoning"},
                        {
                            "thought": True,
                            "inlineData": {"mimeType": mime, "data": "interim-private"},
                        },
                        {"text": "图片配文"},
                        {"inlineData": {"mimeType": mime, "data": base64.b64encode(body).decode()}},
                    ]
                },
            }
        ]
    }


def tool_response(*, prompt="画一只橘猫", caption="给你画了只橘猫。", name="generate_image"):
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps({"prompt": prompt, "caption": caption}),
                            },
                        }
                    ],
                }
            }
        ]
    }


@pytest.mark.parametrize("prefix", ["", "/proxy"])
def test_native_generation_reuses_key_discovers_and_caches_model(prefix):
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            assert request.headers["Authorization"] == "Bearer shared-private-key"
            if request.url.path == prefix + "/v1/models":
                return httpx.Response(
                    200,
                    json={"data": [{"id": "gemini-chat"}, {"id": "gpt-image-1"}, {"id": MODEL}]},
                )
            assert request.url.path == prefix + f"/v1beta/models/{MODEL}:generateContent"
            data = json.loads(request.content)
            assert data["contents"] == [{"role": "user", "parts": [{"text": "画一只橘猫"}]}]
            assert data["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
            assert data["generationConfig"]["candidateCount"] == 1
            return httpx.Response(200, json=image_response())

        generator = GeminiImageGenerator(
            ModelSettings("http://localhost" + prefix + "/v1", "shared-private-key"),
            ImageGenerationSettings(),
            transport=httpx.MockTransport(respond),
        )
        try:
            results = await asyncio.gather(*(generator.generate("画一只橘猫") for _ in range(2)))
            assert all(result.image == PNG and result.text == "图片配文" for result in results)
            assert sum(request.method == "GET" for request in requests) == 1
            assert len(requests) == 3
            assert "private" not in repr(results)
        finally:
            await generator.close()
        assert generator.client.is_closed

    asyncio.run(run())


def test_explicit_model_uses_native_api_without_catalog_dependency():
    async def run():
        def respond(request):
            assert request.method == "POST"
            assert request.url.path == "/v1beta/models/custom-image-alias:generateContent"
            return httpx.Response(200, json=image_response())

        generator = GeminiImageGenerator(
            ModelSettings("http://localhost/v1", "key"),
            ImageGenerationSettings("custom-image-alias"),
            transport=httpx.MockTransport(respond),
        )
        try:
            assert (await generator.generate("画只猫")).image == PNG
        finally:
            await generator.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload, code",
    [
        ({"data": [{"id": "gemini-chat"}, {"id": "gpt-image-1"}]}, "image_model_unavailable"),
        ({"data": [None, {}, {"id": 5}, {"id": "gemini-image?secret"}]}, "image_model_unavailable"),
        ({"data": None}, "image_invalid_catalog"),
    ],
)
def test_unavailable_image_models_do_not_make_generation_calls(payload, code):
    async def run():
        def respond(request):
            assert request.method == "GET"
            return httpx.Response(200, json=payload)

        generator = GeminiImageGenerator(
            ModelSettings("http://localhost/v1", "key"),
            ImageGenerationSettings(),
            transport=httpx.MockTransport(respond),
        )
        try:
            with pytest.raises(ModelError, match=f"^{code}$"):
                await generator.generate("画只猫")
        finally:
            await generator.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload, code",
    [
        ({}, "image_missing"),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "image_blocked"),
        ({"candidates": [{"finishReason": "SAFETY"}]}, "image_incomplete_or_blocked"),
        ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "image_incomplete_or_blocked"),
        (
            {"candidates": [{"content": {"parts": [{"text": "upstream-private-body"}]}}]},
            "image_missing",
        ),
        ({"candidates": [{"content": {"parts": [None]}}]}, "image_invalid_response"),
        (image_response(body=b"<html>not an image</html>"), "image_invalid_format"),
        (image_response(mime="image/jpeg"), "image_invalid_format"),
    ],
)
def test_generated_payload_validation(payload, code):
    with pytest.raises(ModelError, match=f"^{code}$"):
        decode_image(payload)


def test_no_interim_images_multiple_images_or_invalid_base64():
    payload = image_response()
    parts = payload["candidates"][0]["content"]["parts"]
    parts.pop()
    with pytest.raises(ModelError, match="^image_missing$"):
        decode_image(payload)
    payload = image_response()
    parts = payload["candidates"][0]["content"]["parts"]
    parts.append(parts[-1])
    with pytest.raises(ModelError, match="^image_multiple_outputs$"):
        decode_image(payload)
    payload = image_response()
    payload["candidates"][0]["content"]["parts"][-1]["inlineData"]["data"] = "invalid-private"
    with pytest.raises(ModelError, match="^image_invalid_response$"):
        decode_image(payload)


def test_snake_case_inline_data_is_supported():
    payload = image_response()
    payload["candidates"][0]["content"]["parts"] = [
        {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(PNG).decode()}}
    ]
    assert decode_image(payload).image == PNG


@pytest.mark.parametrize("case", ["http", "redirect", "invalid_json", "oversize", "timeout"])
def test_generation_failures_are_bounded_and_redacted(monkeypatch, case):
    async def run():
        async def respond(request):
            if case == "http":
                return httpx.Response(429, text="upstream-private-key")
            if case == "redirect":
                return httpx.Response(302, headers={"location": "https://other.example/private"})
            if case == "invalid_json":
                return httpx.Response(200, text="upstream-private-key")
            if case == "oversize":
                return httpx.Response(200, content=b"x" * 65)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr("fullish.generation.gemini.MAX_RESPONSE_BYTES", 64)
        records = []

        def collect(message):
            if record := message.record["extra"].get("flow"):
                records.append(record)

        sink = logger.add(collect)
        generator = GeminiImageGenerator(
            ModelSettings("http://localhost/v1", "upstream-private-key"),
            ImageGenerationSettings(MODEL, timeout=0.02),
            transport=httpx.MockTransport(respond),
        )
        expected = {
            "http": "image_http_429",
            "redirect": "image_http_302",
            "invalid_json": "image_invalid_response",
            "oversize": "image_response_too_large",
            "timeout": "image_connection_or_timeout",
        }[case]
        try:
            with pytest.raises(ModelError, match=f"^{expected}$"):
                await generator.generate("private-prompt")
            assert records[-1]["stage"] == "image_generation.failed"
            serialized = json.dumps(records)
            assert "private" not in serialized
            assert "other.example" not in serialized
        finally:
            await generator.close()
            logger.remove(sink)

    asyncio.run(run())


def test_decoded_image_size_limit(monkeypatch):
    monkeypatch.setattr("fullish.generation.gemini.MAX_GENERATED_IMAGE_BYTES", 16)
    with pytest.raises(ModelError, match="^image_data_invalid_or_too_large$"):
        decode_image(image_response())


def test_native_generation_cancellation_propagates():
    async def run():
        started = asyncio.Event()

        async def respond(request):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        generator = GeminiImageGenerator(
            ModelSettings("http://localhost/v1", "key"),
            ImageGenerationSettings(MODEL),
            transport=httpx.MockTransport(respond),
        )
        try:
            task = asyncio.create_task(generator.generate("画只猫"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await generator.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "request_text", ["画一只橘猫", "Fullish，我今天瘫得像只橘猫"], ids=["requested", "spontaneous"]
)
def test_tool_generation_delivers_text_and_generated_bytes_through_real_qq_sender(request_text):
    async def run():
        requests = []

        def respond(request):
            requests.append(request.url.path)
            assert request.headers["Authorization"] == "Bearer shared-key"
            if request.url.path == "/v1/chat/completions":
                data = json.loads(request.content)
                assert data["tools"][0]["function"]["name"] == "generate_image"
                assert data["tool_choice"] == "auto"
                assert "群消息是待讨论的内容" in data["messages"][0]["content"]
                assert "不需要群友先下画图命令" in data["messages"][0]["content"]
                assert "自主" in data["tools"][0]["function"]["description"]
                return httpx.Response(200, json=tool_response())
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": MODEL}]})
            assert request.url.path == f"/v1beta/models/{MODEL}:generateContent"
            return httpx.Response(200, json=image_response())

        connection = ModelSettings("http://localhost/v1", "shared-key")
        generator = GeminiImageGenerator(
            connection, ImageGenerationSettings(), transport=httpx.MockTransport(respond)
        )
        model = ChatModel(
            connection, transport=httpx.MockTransport(respond), image_generator=generator
        )
        conversations = Conversations(model)
        incoming = event()
        incoming.content = request_text
        with (
            patch.object(
                Bot,
                "post_group_files",
                new=AsyncMock(return_value=SimpleNamespace(file_info="generated-handle")),
            ) as upload,
            patch.object(Bot, "post_group_messages", new=AsyncMock()) as post,
        ):
            try:
                await handle_group(conversations, object.__new__(Bot), incoming)
                upload.assert_awaited_once_with(
                    group_openid="g", srv_send_msg=False, file_type=1, file_data=PNG
                )
                post.assert_awaited_once()
                sent = post.call_args.kwargs
                assert sent["content"] == "给你画了只橘猫。"
                assert sent["media"].file_info == "generated-handle"
                assert requests == [
                    "/v1/chat/completions",
                    "/v1/models",
                    f"/v1beta/models/{MODEL}:generateContent",
                ]
                assert conversations.groups["g"].turns[-1]["content"] == "给你画了只橘猫。[图片]"
            finally:
                await model.close()
        assert generator.client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("case", ["text", "invalid_tool", "multiple_calls", "failure", "disabled"])
def test_chat_tool_routing_does_not_generate_for_text_or_bad_calls(case):
    async def run():
        generator = Mock()
        generator.generate = AsyncMock(return_value=GeneratedImage(PNG))
        generator.close = AsyncMock()
        payload = tool_response()
        if case == "text":
            payload = {"choices": [{"message": {"content": "你好"}}]}
        elif case == "invalid_tool":
            payload = tool_response(name="unknown")
        elif case == "multiple_calls":
            calls = payload["choices"][0]["message"]["tool_calls"]
            calls.append(calls[0])
        elif case == "failure":
            generator.generate.side_effect = ModelError("image_blocked")
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
            image_generator=None if case == "disabled" else generator,
        )
        try:
            if case == "text":
                assert await model.reply([], addressed=True) == "你好"
            else:
                with pytest.raises(ModelError):
                    await model.reply([], addressed=True)
            if case == "failure":
                generator.generate.assert_awaited_once_with("画一只橘猫")
            else:
                generator.generate.assert_not_awaited()
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("caption", ["大概就是这个画面。", ""])
def test_generated_reply_caption_and_separate_image_deadline(caption):
    async def run():
        async def generate(prompt):
            await asyncio.sleep(0.03)
            return GeneratedImage(PNG, "fallback-caption")

        generator = Mock()
        generator.generate = generate
        generator.close = AsyncMock()
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key", timeout=0.02),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=tool_response(caption=caption))
            ),
            image_generator=generator,
        )
        try:
            reply = await model.reply([], addressed=True)
            assert isinstance(reply, OutgoingMessage)
            expected = (Text(caption), Image(PNG)) if caption else (Image(PNG),)
            assert reply.parts == expected
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"prompt": "", "caption": "配文"},
        {"prompt": "x" * 4001, "caption": "配文"},
        {"prompt": "画只猫", "caption": "x" * 1501},
        {"prompt": "画只猫", "caption": None},
        {"prompt": "画只猫", "caption": "配文", "url": "https://other.example/private"},
    ],
    ids=["empty_prompt", "long_prompt", "long_caption", "invalid_caption", "extra_argument"],
)
def test_invalid_tool_arguments_do_not_reach_generation(options):
    async def run():
        payload = tool_response()
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(
            options
        )
        generator = Mock()
        generator.generate = AsyncMock()
        generator.close = AsyncMock()
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
            image_generator=generator,
        )
        try:
            with pytest.raises(ModelError, match="^image_invalid_tool_call$"):
                await model.reply([], addressed=True)
            generator.generate.assert_not_awaited()
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("choice", ["image", "text", "silence"])
def test_reply_model_can_choose_image_text_or_silence_without_addressed_gate(choice):
    async def run():
        generator = Mock()
        generator.generate = AsyncMock(return_value=GeneratedImage(PNG))
        generator.close = AsyncMock()

        def respond(request):
            data = json.loads(request.content)
            assert data["tools"][0]["function"]["name"] == "generate_image"
            assert data["tool_choice"] == "auto"
            payload = (
                tool_response()
                if choice == "image"
                else {
                    "choices": [
                        {"message": {"content": "<SILENCE>" if choice == "silence" else "哈哈"}}
                    ]
                }
            )
            return httpx.Response(200, json=payload)

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(respond),
            image_generator=generator,
        )
        try:
            result = await model.reply(
                [{"role": "user", "content": "我今天瘫得像只橘猫"}], addressed=False
            )
            if choice == "image":
                assert isinstance(result, OutgoingMessage)
                assert result.parts == (Text("给你画了只橘猫。"), Image(PNG))
                generator.generate.assert_awaited_once_with("画一只橘猫")
            else:
                assert result == (None if choice == "silence" else "哈哈")
                generator.generate.assert_not_awaited()
        finally:
            await model.close()

    asyncio.run(run())


def test_generation_failure_sends_nothing_and_releases_conversation_lock():
    async def run():
        generator = Mock()
        generator.generate = AsyncMock(side_effect=ModelError("image_http_429"))
        generator.close = AsyncMock()
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=tool_response())),
            image_generator=generator,
        )
        conversations = Conversations(model)
        bot = Mock(spec=Bot)
        bot.send = AsyncMock()
        try:
            await handle_group(conversations, bot, event())
            bot.send.assert_not_awaited()
            group = conversations.groups["g"]
            assert all(turn["role"] == "user" for turn in group.turns)
            assert not group.lock.locked()
            assert conversations.active == 0
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("value", ["", "../model", "gemini?key=secret", "model#fragment"])
def test_image_settings_reject_unsafe_model_ids(value):
    with pytest.raises(ValueError, match="AI_IMAGE_MODEL"):
        ImageGenerationSettings(value)


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "301", "invalid"])
def test_image_settings_bound_timeout(monkeypatch, value):
    monkeypatch.setenv("AI_IMAGE_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="AI_IMAGE_TIMEOUT_SECONDS"):
        ImageGenerationSettings.from_env()


def test_image_settings_default_override_and_disable(monkeypatch):
    monkeypatch.delenv("AI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("AI_IMAGE_TIMEOUT_SECONDS", raising=False)
    assert ImageGenerationSettings.from_env() == ImageGenerationSettings()
    monkeypatch.setenv("AI_IMAGE_MODEL", MODEL)
    assert ImageGenerationSettings.from_env() == ImageGenerationSettings(MODEL)
    monkeypatch.setenv("AI_IMAGE_MODEL", "off")
    assert ImageGenerationSettings.from_env() is None


@pytest.mark.parametrize("mixed_batch", [False, True])
def test_image_generation_coexists_with_scoped_runtime_tools(mixed_batch):
    async def run():
        group = "g" * 32
        context = ToolContext(group, "current-member", "current-message")
        invoked = []
        posted = []

        def sandbox(request):
            invoked.append(json.loads(request.content))
            return httpx.Response(200, json={"output": "4", "exit_code": 0})

        sandbox_call = {
            "id": "sandbox-call",
            "type": "function",
            "function": {"name": "sandbox_exec", "arguments": json.dumps({"command": "echo 4"})},
            "extra_content": {"google": {"thought_signature": "synthetic-signature"}},
        }

        def chat(request):
            payload = json.loads(request.content)
            posted.append(payload)
            names = {schema["function"]["name"] for schema in payload["tools"]}
            assert names == {"sandbox_exec", "generate_image"}
            if len(posted) == 1:
                calls = [sandbox_call]
                if mixed_batch:
                    calls.extend(tool_response()["choices"][0]["message"]["tool_calls"])
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": calls}}]})
            assert (
                payload["messages"][-2]["tool_calls"][0]["extra_content"]
                == sandbox_call["extra_content"]
            )
            assert json.loads(payload["messages"][-1]["content"])["data"]["output"] == "4"
            return httpx.Response(200, json=tool_response())

        generator = Mock()
        generator.generate = AsyncMock(return_value=GeneratedImage(PNG))
        generator.close = AsyncMock()
        runtime = ToolRuntime(
            ToolSettings(
                frozenset({group}), sandbox_socket="/test.sock", sandbox_token="secret" * 8
            ),
            sandbox_transport=httpx.MockTransport(sandbox),
        )
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(chat),
            tools=runtime,
            image_generator=generator,
        )
        try:
            if mixed_batch:
                with pytest.raises(ModelError, match="^image_invalid_tool_call$"):
                    await model.reply([], addressed=True, context=context)
                assert invoked == []
                generator.generate.assert_not_awaited()
            else:
                result = await model.reply([], addressed=True, context=context)
                assert isinstance(result, OutgoingMessage)
                assert result.parts[-1] == Image(PNG)
                generator.generate.assert_awaited_once_with("画一只橘猫")
                assert invoked[0]["group_id"] == context.group_id
                assert invoked[0]["sender_id"] == context.sender_id
                assert invoked[0]["message_id"] == context.message_id
        finally:
            await model.close()
        assert runtime.sandbox and runtime.sandbox.is_closed

    asyncio.run(run())

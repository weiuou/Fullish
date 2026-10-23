"""Exercise optional expression planning through its actual HTTP and conversation boundary."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from fullish.collection import MessagePart, ParsedContent, QuotedMessage
from fullish.conversation import Conversations
from fullish.model import ChatModel
from fullish.sending import Audio, Image, OutgoingMessage, QQFace, ReplyPlan, Text
from fullish.sending.assets import ReplyAsset, ReplyAssets
from fullish.sending.postprocess import ReplyPostprocessor
from fullish.sending.schema import outgoing
from fullish.settings import ModelSettings, ReplyPostprocessSettings

BACKEND = ModelSettings("https://model.example/v1", "test-key", model="main-model")


def text_message(text):
    return {"parts": [{"type": "text", "text": text}]}


def asset_part(asset_id):
    return {"type": "asset", "asset_id": asset_id}


def completion(plan):
    content = plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def process(reply, handler, *, settings=None, assets=None, context=(), allow_images=True):
    processor = ReplyPostprocessor(
        BACKEND,
        settings or ReplyPostprocessSettings(),
        assets=assets,
        transport=httpx.MockTransport(handler),
    )
    try:
        return await processor.process(reply, context=context, allow_images=allow_images)
    finally:
        await processor.close()


def test_expression_rewrites_meaning_into_optional_messages_with_emoji_and_real_assets():
    image = Image(b"curated-celebration-image")
    face = QQFace("14", "😊")
    assets = ReplyAssets(
        (ReplyAsset("celebrate", "庆祝成功", image), ReplyAsset("smile", "开心的笑脸", face))
    )
    plan = {
        "messages": [
            text_message("终于过了！🎉"),
            {"parts": [asset_part("catalog_celebrate")]},
            {
                "parts": [
                    {"type": "text", "text": "折腾一下午，先歇会儿吧"},
                    asset_part("catalog_smile"),
                ]
            },
        ]
    }
    result = asyncio.run(
        process(
            "终于部署成功了，折腾了一下午确实很累，先休息一下吧。",
            lambda _: completion(plan),
            assets=assets,
        )
    )
    assert [message.text for message in result.messages] == [
        "终于过了！🎉",
        "",
        "折腾一下午，先歇会儿吧",
    ]
    assert result.messages[1].parts == (image,)
    assert result.messages[2].parts[-1] is face


@pytest.mark.parametrize("draft", ["收到", OutgoingMessage((Text("原来的配文"), Image(b"image")))])
def test_model_can_keep_original_without_any_rewrite(draft):
    result = asyncio.run(process(draft, lambda _: completion({"keep_original": True})))
    assert result == ReplyPlan((outgoing(draft),))


def test_short_answer_can_be_rewritten_as_one_message():
    result = asyncio.run(
        process("我明白了。", lambda _: completion({"messages": [text_message("懂啦 🙂")]}))
    )
    assert result == ReplyPlan((OutgoingMessage((Text("懂啦 🙂"),)),))


def test_request_uses_explicit_style_and_current_users_without_history_or_media_sources():
    captured = []
    assets = ReplyAssets(
        (
            ReplyAsset("local", "庆祝图片", Image(Path("private-local.png"))),
            ReplyAsset("remote", "鼓励图片", Image("https://private.example/img?token=secret")),
            ReplyAsset("bytes", "开心图片", Image(b"private-image-bytes")),
            ReplyAsset("smile", "微笑", QQFace("14", "😊")),
        )
    )
    draft = OutgoingMessage((Text("初稿文字"), Audio(b"private-draft-audio")))
    context = [
        {"role": "system", "content": "untrusted-old-system"},
        {
            "role": "user",
            "content": "终于成功啦",
            "sender_id": "u",
            "sender_name": "小明",
            "images": ["https://private.example/context"],
        },
        {"role": "assistant", "content": "private-old-assistant-habit"},
        {"role": "tool", "content": "private-tool-output"},
        {"role": "user", "content": "折腾得好累", "sender_id": "v"},
    ]

    def respond(request):
        assert request.url == "https://model.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        captured.append(json.loads(request.content))
        return completion({"keep_original": True})

    asyncio.run(
        process(
            draft,
            respond,
            assets=assets,
            context=context,
            settings=ReplyPostprocessSettings(model="expression-model", style="克制幽默"),
        )
    )
    request = captured[0]
    assert request["model"] == "expression-model"
    assert request["messages"][0]["role"] == "system"
    assert "克制幽默" in request["messages"][0]["content"]
    assert "tools" not in request
    payload = json.loads(request["messages"][1]["content"])
    assert payload["context"] == [
        {"sender_id": "u", "sender_name": "小明", "text": "终于成功啦"},
        {"sender_id": "v", "sender_name": "", "text": "折腾得好累"},
    ]
    assert payload["draft"] == [{"type": "text", "text": "初稿文字"}, asset_part("draft_1")]
    assert {item["id"] for item in payload["available_assets"]} == {
        "draft_1",
        "catalog_local",
        "catalog_remote",
        "catalog_bytes",
        "catalog_smile",
    }
    assert all(
        set(item) == {"id", "kind", "description", "required"}
        for item in payload["available_assets"]
    )
    serialized = json.dumps(request)
    for secret in ("private-", "private.example", "untrusted-old-system", "token=secret"):
        assert secret not in serialized


def test_context_is_bounded_to_recent_user_messages():
    captured = []

    def respond(request):
        captured.extend(
            json.loads(json.loads(request.content)["messages"][1]["content"])["context"]
        )
        return completion({"keep_original": True})

    quote = QuotedMessage(ParsedContent((MessagePart("text", "引用" * 1200),)), sender_id="quoted")
    context = [
        {"role": "user", "content": "长" * 1200, "sender_id": str(i), "reply": quote}
        for i in range(30)
    ]
    asyncio.run(process("初稿", respond, context=context))
    assert len(captured) <= 8
    assert (
        sum(len(item["text"]) + len(item.get("reply_to", {}).get("text", "")) for item in captured)
        <= 3000
    )
    assert captured[-1]["sender_id"] == "29"
    assert all(len(item["text"]) <= 1000 for item in captured)


def test_current_quote_reaches_expression_context_without_attachment_sources():
    quote = QuotedMessage(
        ParsedContent(
            (
                MessagePart("text", "部署完成了"),
                MessagePart("image", "", {"url": "https://private.example/quoted"}),
            )
        ),
        sender_id="quoted-user",
    )

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert payload["context"] == [
            {
                "sender_id": "current-user",
                "sender_name": "",
                "text": "终于好了",
                "reply_to": {"sender_id": "quoted-user", "text": "部署完成了"},
            }
        ]
        assert "private.example" not in request.content.decode()
        return completion({"keep_original": True})

    asyncio.run(
        process(
            "太好了",
            respond,
            context=[
                {
                    "role": "user",
                    "content": "终于好了",
                    "sender_id": "current-user",
                    "reply": quote,
                }
            ],
        )
    )


def test_disabled_images_are_absent_but_qq_faces_remain_available():
    assets = ReplyAssets(
        (ReplyAsset("img", "图片", Image(b"image")), ReplyAsset("face", "笑脸", QQFace("14", "😊")))
    )

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert [asset["id"] for asset in payload["available_assets"]] == ["catalog_face"]
        return completion(
            {"messages": [text_message("自然表达"), {"parts": [asset_part("catalog_img")]}]}
        )

    result = asyncio.run(process("初稿", respond, assets=assets, allow_images=False))
    assert result == ReplyPlan((outgoing("初稿"),))


@pytest.mark.parametrize("media", [Image(b"image"), Audio(b"audio"), QQFace("14", "😊")])
def test_media_only_reply_bypasses_model(media):
    transport = Mock(side_effect=AssertionError("Media-only replies should not call the model"))
    reply = OutgoingMessage((media,))
    assert asyncio.run(process(reply, transport)) == ReplyPlan((reply,))
    transport.assert_not_called()


@pytest.mark.parametrize(
    "plan",
    [
        "not json",
        '```json\n{"keep_original":true}\n```',
        [],
        None,
        {"keep_original": False},
        {"keep_original": True, "messages": []},
        {"messages": []},
        {"messages": [text_message("好")] * 4},
        {"messages": [text_message("字" * 2001)]},
        {"messages": [{"parts": []}]},
        {"messages": [text_message("   ")]},
        {"messages": [{"parts": [{"type": "text", "text": 123}]}]},
        {"messages": [{"parts": [{"type": "image", "url": "https://made-up.example/img"}]}]},
        {"messages": [{"parts": [asset_part("unknown")]}]},
        {"messages": [{"parts": [{"type": "tool_call", "name": "execute"}]}]},
        {"messages": [text_message("[CQ:image,file=made-up]")]},
        {"messages": [text_message("<emoji:14>")]},
        {"messages": [text_message("<SILENCE>")]},
        {"messages": [text_message('<qqbot-at-user id="someone" />')]},
    ],
)
def test_unusable_or_untrusted_expression_plan_falls_back_to_draft(plan):
    result = asyncio.run(process("原稿内容", lambda _: completion(plan)))
    assert result == ReplyPlan((outgoing("原稿内容"),))


@pytest.mark.parametrize("case", ["unknown", "duplicate", "missing_draft", "two_attachments"])
def test_media_validation_preserves_original_attachments(case):
    draft = OutgoingMessage((Text("原图说明"), Image(b"original-image")))
    assets = ReplyAssets((ReplyAsset("extra", "庆祝图片", Image(b"extra-image")),))
    parts = [{"type": "text", "text": "原图说明"}, asset_part("draft_1")]
    messages = [{"parts": parts}]
    if case == "unknown":
        messages.append({"parts": [asset_part("invented")]})
    elif case == "duplicate":
        messages.append({"parts": [asset_part("draft_1")]})
    elif case == "missing_draft":
        messages = [text_message("原图说明")]
    else:
        parts.append(asset_part("catalog_extra"))
    assert asyncio.run(
        process(draft, lambda _: completion({"messages": messages}), assets=assets)
    ) == ReplyPlan((draft,))


def test_original_attachment_survives_a_valid_rewrite():
    audio = Audio(b"original-voice")
    draft = OutgoingMessage((Text("这是录音，听听看。"), audio))
    plan = {"messages": [text_message("录好啦，听听看"), {"parts": [asset_part("draft_1")]}]}
    result = asyncio.run(process(draft, lambda _: completion(plan)))
    assert result.messages[1].parts == (audio,)


def test_image_budget_applies_across_the_whole_expression_plan():
    assets = ReplyAssets(tuple(ReplyAsset(str(i), "图片", Image(bytes([i]))) for i in range(3)))
    plan = {"messages": [{"parts": [asset_part("catalog_" + str(i))]} for i in range(3)]}
    assert asyncio.run(process("原稿", lambda _: completion(plan), assets=assets)) == ReplyPlan(
        (outgoing("原稿"),)
    )


@pytest.mark.parametrize(
    "draft, rewritten",
    [
        ("还要等 15 分钟", "还要等 10 分钟"),
        ("还要等一会儿", "再等 10 分钟"),
        ("还要等 15 分钟", "再等一会儿"),
        ("共 15 个，计数也是 15", "共 15 个"),
        ("参考 https://docs.example/help", "参考 https://made-up.example/help"),
        ("参考 https://docs.example/help", "看文档就行"),
        ("看文档就行", "参考 https://docs.example/help"),
        ("执行 `uv sync` 就好", "执行 `pip install` 就好"),
        ("执行 `uv sync` 就好", "同步依赖就好"),
        ("示例：\n```py\nprint('ok')\n```", "示例：\n```py\nprint('fail')\n```"),
    ],
)
def test_changed_added_or_removed_numbers_links_and_code_fall_back(draft, rewritten):
    result = asyncio.run(
        process(draft, lambda _: completion({"messages": [text_message(rewritten)]}))
    )
    assert result == ReplyPlan((outgoing(draft),))


def test_protected_content_can_move_between_messages_without_being_changed():
    draft = "等 15 分钟后执行 `uv sync`\n参考 https://docs.example/help"
    plan = {
        "messages": [
            text_message("先等 15 分钟，再执行 `uv sync`"),
            text_message("可以看看这个： https://docs.example/help"),
        ]
    }
    result = asyncio.run(process(draft, lambda _: completion(plan)))
    assert len(result.messages) == 2
    assert result.messages[0].text == "先等 15 分钟，再执行 `uv sync`"


@pytest.mark.parametrize("tool_field", ["tool_calls", "function_call"])
def test_model_tool_call_never_becomes_a_reply(tool_field):
    message = {
        "content": json.dumps({"messages": [text_message("已完成操作")]}),
        tool_field: [{"name": "execute"}] if tool_field == "tool_calls" else {"name": "execute"},
    }
    result = asyncio.run(
        process("还没执行", lambda _: httpx.Response(200, json={"choices": [{"message": message}]}))
    )
    assert result == ReplyPlan((outgoing("还没执行"),))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": [{"text": "hi"}]}}]},
    ],
)
def test_malformed_completion_envelope_falls_back(body):
    result = asyncio.run(process("原稿", lambda _: httpx.Response(200, json=body)))
    assert result == ReplyPlan((outgoing("原稿"),))


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_failures_fall_back_without_following_redirects(status):
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(status, headers={"Location": "https://other.example/model"})

    assert asyncio.run(process("原稿", respond)) == ReplyPlan((outgoing("原稿"),))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "draft, changed",
    [
        ("温度 -3°C", "温度 3°C"),
        ("比例 10%", "比例 10"),
        ("幅度 .5", "幅度 5"),
        ("系数 1e-3", "系数 1e3"),
        ("比例 10％", "比例 10%"),
    ],
)
def test_number_signs_percentages_and_exponents_are_preserved(draft, changed):
    result = asyncio.run(
        process(draft, lambda _: completion({"messages": [text_message(changed)]}))
    )
    assert result == ReplyPlan((outgoing(draft),))


def test_network_timeout_falls_back():
    def respond(request):
        raise httpx.ReadTimeout("timeout", request=request)

    assert asyncio.run(process("原稿", respond)) == ReplyPlan((outgoing("原稿"),))


def test_outer_deadline_bounds_a_stalled_provider():
    async def respond(request):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    result = asyncio.run(process("原稿", respond, settings=ReplyPostprocessSettings(timeout=0.01)))
    assert result == ReplyPlan((outgoing("原稿"),))


def test_oversized_response_is_not_accepted_even_when_its_plan_is_valid():
    body = json.dumps(
        {
            "choices": [
                {"message": {"content": json.dumps({"messages": [text_message("另一个回答")]})}}
            ],
            "padding": "x" * 70000,
        }
    )
    result = asyncio.run(process("原稿", lambda _: httpx.Response(200, content=body)))
    assert result == ReplyPlan((outgoing("原稿"),))


def test_cancellation_propagates_instead_of_sending_original_during_shutdown():
    async def run():
        started = asyncio.Event()

        async def respond(request):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        task = asyncio.create_task(process("原稿", respond))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("draft", [None, "AI 初稿"])
def test_conversation_runs_optional_processing_only_after_a_non_silent_draft(enabled, draft):
    async def run():
        order = []

        async def generate(*args, **kwargs):
            order.append("generate")
            return draft

        def respond(request):
            order.append("process")
            payload = json.loads(json.loads(request.content)["messages"][1]["content"])
            assert payload["draft"] == [{"type": "text", "text": draft}]
            assert payload["context"][-1]["text"] == "说说看"
            return completion({"messages": [text_message("表达后的回答 🙂")]})

        model = Mock(spec=ChatModel)
        model.reply = AsyncMock(side_effect=generate)
        processor = ReplyPostprocessor(
            BACKEND, ReplyPostprocessSettings(), transport=httpx.MockTransport(respond)
        )
        conversations = Conversations(model, postprocessor=processor if enabled else None)
        try:
            result = await conversations.receive("g", "m", "u", "说说看", addressed=True)
            assert order == (["generate", "process"] if enabled and draft else ["generate"])
            if enabled and draft:
                assert result == ReplyPlan((outgoing("表达后的回答 🙂"),))
            else:
                assert result == draft
            # Generating or planning a reply must not record unsent assistant messages.
            assert all(turn["role"] == "user" for turn in conversations.groups["g"].turns)
            assert conversations.active == 0
        finally:
            await processor.close()

    asyncio.run(run())


def test_settings_support_backend_default_and_explicit_runtime_configuration(monkeypatch, tmp_path):
    for key in (
        "AI_REPLY_POSTPROCESSING",
        "AI_REPLY_MODEL",
        "AI_REPLY_TIMEOUT_SECONDS",
        "AI_REPLY_MAX_MESSAGES",
        "AI_REPLY_STYLE",
        "AI_REPLY_ASSETS_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert ReplyPostprocessSettings.from_env() is None
    assert ReplyPostprocessSettings.from_env(default_enabled=True) == ReplyPostprocessSettings()
    monkeypatch.setenv("AI_REPLY_POSTPROCESSING", "true")
    assert ReplyPostprocessSettings.from_env() == ReplyPostprocessSettings()
    monkeypatch.setenv("AI_REPLY_MODEL", "expression-model")
    monkeypatch.setenv("AI_REPLY_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("AI_REPLY_MAX_MESSAGES", "2")
    monkeypatch.setenv("AI_REPLY_STYLE", "简洁自然")
    manifest = tmp_path / "assets.json"
    monkeypatch.setenv("AI_REPLY_ASSETS_FILE", str(manifest))
    assert ReplyPostprocessSettings.from_env() == ReplyPostprocessSettings(
        "expression-model", 4.5, 2, "简洁自然", manifest
    )
    monkeypatch.setenv("AI_REPLY_POSTPROCESSING", "false")
    assert ReplyPostprocessSettings.from_env() is None
    assert ReplyPostprocessSettings.from_env(default_enabled=True) is None


@pytest.mark.parametrize(
    "key, value",
    [
        ("AI_REPLY_POSTPROCESSING", "perhaps"),
        ("AI_REPLY_TIMEOUT_SECONDS", "nan"),
        ("AI_REPLY_TIMEOUT_SECONDS", "inf"),
        ("AI_REPLY_TIMEOUT_SECONDS", "0"),
        ("AI_REPLY_TIMEOUT_SECONDS", "31"),
        ("AI_REPLY_MAX_MESSAGES", "0"),
        ("AI_REPLY_MAX_MESSAGES", "6"),
        ("AI_REPLY_MAX_MESSAGES", "1.5"),
        ("AI_REPLY_STYLE", "  "),
    ],
)
def test_invalid_enabled_runtime_settings_fail_at_startup(monkeypatch, key, value):
    monkeypatch.setenv("AI_REPLY_POSTPROCESSING", "true")
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        ReplyPostprocessSettings.from_env()

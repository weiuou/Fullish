"""GIF references retain their appearance through quoted-message image editing."""

import asyncio
import base64
import json
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot.adapters.qq.bot import _check_at_me, _check_reply
from nonebot.adapters.qq.models import Dispatch
from PIL import Image
from test_reference_images import GENERATED, multipart, tool
from test_replies import bot_mock, quoted_event

from fullish.animated_images import GifError, gif_preview
from fullish.conversation import Conversations
from fullish.errors import ModelError
from fullish.generation.openai import OpenAIImageGenerator
from fullish.generation.references import prepare_reference_images
from fullish.image_assets import ImageAssetError, ImageAssets
from fullish.model import ChatModel
from fullish.qq import GroupOnlyAdapter, handle_group
from fullish.settings import ImageGenerationSettings, ModelSettings


def gif(*, animated=False):
    with Image.new("P", (2, 1), 1) as first, BytesIO() as output:
        first.putpalette([0, 0, 0, 255, 0, 0, 0, 0, 255] + [0] * (768 - 9))
        first.putpixel((0, 0), 0)
        with first.copy() as second:
            second.putpixel((1, 0), 2)
            first.save(
                output,
                format="GIF",
                transparency=0,
                save_all=animated,
                append_images=[second] if animated else [],
                duration=100,
                loop=0,
            )
        return output.getvalue()


@pytest.mark.parametrize("animated", [False, True])
def test_gif_reference_preserves_static_alpha_and_includes_each_animation_frame(animated):
    original = gif(animated=animated)
    (prepared,) = prepare_reference_images((original,))
    assert original[:6] == b"GIF89a" and prepared.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(prepared)) as frame:
        if animated:
            assert frame.mode == "RGB" and frame.size == (512, 280)
            assert frame.getpixel((224, 152)) == (255, 0, 0)
            assert frame.getpixel((480, 152)) == (0, 0, 255)
            assert frame.getpixel((0, 88)) == (255, 255, 255)
        else:
            assert frame.mode == "RGBA" and frame.size == (2, 1)
            assert frame.getpixel((0, 0)) == (0, 0, 0, 0)
            assert frame.getpixel((1, 0)) == (255, 0, 0, 255)


def test_timeline_includes_meaningful_middle_even_when_first_and_last_match():
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue", "red")]
    try:
        with BytesIO() as output:
            frames[0].save(
                output,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=[100, 200, 100],
            )
            preview = gif_preview(output.getvalue())
        assert preview.frame_indices == (0, 1, 2)
        assert preview.timestamps_ms == (0, 100, 300) and preview.duration_ms == 400
        with Image.open(BytesIO(preview.image)) as sheet:
            assert sheet.getpixel((128, 152)) == (255, 0, 0)
            assert sheet.getpixel((384, 152)) == (0, 0, 255)
            assert sheet.getpixel((640, 152)) == (255, 0, 0)
    finally:
        for frame in frames:
            frame.close()


def test_long_held_frame_does_not_hide_the_rest_of_the_timeline():
    frames = [Image.new("RGB", (8, 8), (i * 10, 0, 0)) for i in range(25)]
    try:
        with BytesIO() as output:
            frames[0].save(
                output,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=[10000] + [100] * 24,
            )
            preview = gif_preview(output.getvalue())
        assert preview.frame_count == 25
        assert len(preview.frame_indices) == 12
        assert preview.frame_indices[0] == 0 and preview.frame_indices[-1] == 24
        assert preview.frame_indices == tuple(sorted(set(preview.frame_indices)))
        assert len([i for i in preview.frame_indices if 1 <= i <= 23]) == 10
    finally:
        for frame in frames:
            frame.close()


@pytest.mark.parametrize("limit", ["MAX_GIF_FRAMES", "MAX_DECODE_PIXELS"])
def test_animation_decoder_stops_at_resource_limits(monkeypatch, limit):
    monkeypatch.setattr("fullish.animated_images." + limit, 1)
    with pytest.raises(GifError, match="gif_decode_limit"):
        gif_preview(gif(animated=True))


def test_animation_asset_keeps_original_for_sending_but_uses_timeline_for_vision_and_edit():
    original = gif(animated=True)
    asset = ImageAssets().add(original)
    assert asset.body == original and asset.filename.endswith(".gif")
    assert asset.media_type == "image/gif" and asset.animation
    assert asset.edit_body == asset.animation.image
    visual = asset.vision()
    assert "GIF 动作序列" in visual[0]["text"]
    assert base64.b64decode(visual[1]["image_url"]["url"].split(",", 1)[1]) == asset.edit_body


def test_animation_preview_counts_toward_reply_memory_limit(monkeypatch):
    original = gif(animated=True)
    preview = gif_preview(original)
    monkeypatch.setattr(
        "fullish.image_assets.MAX_SESSION_BYTES", len(original) + len(preview.image) - 1
    )
    assets = ImageAssets()
    with pytest.raises(ImageAssetError, match="image_session_limit"):
        assets.add(original)
    assert not assets.items


def test_animation_is_visible_across_its_timeline_when_image_generation_is_disabled():
    async def run():
        original = gif(animated=True)

        def chat(request):
            parts = json.loads(request.content)["messages"][1]["content"]
            assert any("GIF 动作序列" in part.get("text", "") for part in parts)
            visual = next(part for part in parts if part["type"] == "image_url")
            assert (
                base64.b64decode(visual["image_url"]["url"].split(",", 1)[1])
                == gif_preview(original).image
            )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "红色变成蓝色。"}}]}
            )

        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"), transport=httpx.MockTransport(chat)
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=original))
        )
        try:
            assert (
                await model.reply(
                    [{"role": "user", "content": "描述动作", "images": ["https://cdn/ref.gif"]}],
                    addressed=True,
                )
                == "红色变成蓝色。"
            )
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_supported_references_keep_original_bytes(format):
    with Image.new("RGB", (2, 1), "red") as image, BytesIO() as output:
        image.save(output, format=format)
        body = output.getvalue()
    assert prepare_reference_images((body,)) == (body,)


@pytest.mark.parametrize("body", [b"GIF89a", b"GIF89a\x02\0\x01\0garbage", gif()[:30]])
def test_damaged_gif_is_rejected_before_contacting_the_edit_endpoint(body):
    async def run():
        requests = []

        def reject(request):
            requests.append(request)
            return httpx.Response(500)

        generator = OpenAIImageGenerator(
            ModelSettings("http://localhost/v1", "key"),
            ImageGenerationSettings(),
            transport=httpx.MockTransport(reject),
        )
        try:
            with pytest.raises(ModelError, match="^image_invalid_reference$"):
                await generator.generate("改图", reference_images=(body,))
            assert not requests
        finally:
            await generator.close()

    asyncio.run(run())


def test_gif_canvas_limit_is_checked_before_decoding(monkeypatch):
    body = b"GIF89a" + (65535).to_bytes(2, "little") * 2 + b"small-payload"
    monkeypatch.setattr(Image, "open", lambda *a, **kw: pytest.fail("must not decode this canvas"))
    with pytest.raises(ModelError, match="^image_invalid_reference$"):
        prepare_reference_images((body,))


@pytest.mark.parametrize("limit", ["single", "total"])
def test_reference_byte_limits_apply_after_gif_conversion(monkeypatch, limit):
    with Image.new("P", (1, 1)) as image, BytesIO() as output:
        image.save(output, format="GIF")
        original = output.getvalue()
    (prepared,) = prepare_reference_images((original,))
    assert len(prepared) > len(original)
    if limit == "single":
        monkeypatch.setattr("fullish.generation.references.MAX_REFERENCE_BYTES", len(prepared) - 1)
        images = (original,)
    else:
        monkeypatch.setattr(
            "fullish.generation.references.MAX_TOTAL_REFERENCE_BYTES", 2 * len(prepared) - 1
        )
        images = (original, original)
    with pytest.raises(ModelError, match="^image_invalid_reference$"):
        prepare_reference_images(images)


@pytest.mark.parametrize("animated", [False, True])
def test_quoted_gif_reaches_static_edit_endpoint_and_unified_qq_sender(animated):
    async def run():
        original = gif(animated=animated)
        requests = []

        def chat(request):
            parts = json.loads(request.content)["messages"][1]["content"]
            assert "reply_to" in json.loads(parts[0]["text"])
            visual = next(part for part in parts if part["type"] == "image_url")
            assert visual["image_url"]["url"].startswith("data:image/png;")
            assert (
                base64.b64decode(visual["image_url"]["url"].split(",", 1)[1])
                == gif_preview(original).image
            )
            if animated:
                assert any("GIF 动作序列" in part.get("text", "") for part in parts)
            label = next(part["text"] for part in parts if "image_id=" in part.get("text", ""))
            image_id = label.split("image_id=", 1)[1].split("；", 1)[0]
            return tool(
                "generate_image",
                {"prompt": "画这个", "caption": "画好了", "reference_image_ids": [image_id]},
            )

        def edit(request):
            requests.append(request.url.path)
            assert request.url.path == "/v1/images/edits"
            assert b'filename="reference-0.png"' in request.content
            assert b"Content-Type: image/png" in request.content
            body = next(value for name, value in multipart(request) if name == "image[]")
            assert isinstance(body, bytes)
            assert body == gif_preview(original).image
            with Image.open(BytesIO(body)) as image:
                assert image.format == "PNG"
                if animated:
                    assert image.getpixel((224, 152)) == (255, 0, 0)
                    assert image.getpixel((480, 152)) == (0, 0, 255)
                else:
                    assert image.getpixel((0, 0)) == (0, 0, 0, 0)
                    assert image.getpixel((1, 0)) == (255, 0, 0, 255)
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(GENERATED).decode()}]}
            )

        connection = ModelSettings("http://localhost/v1", "key")
        generator = OpenAIImageGenerator(
            connection, ImageGenerationSettings(), transport=httpx.MockTransport(edit)
        )
        model = ChatModel(
            connection, transport=httpx.MockTransport(chat), image_generator=generator
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=original))
        )
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
            content="画这个",
            attachments=[{"content_type": "image/gif", "url": "https://cdn/reference.gif"}],
        )
        dispatch = Dispatch.model_validate(
            {"op": 0, "s": 1, "t": "GROUP_AT_MESSAGE_CREATE", "d": incoming.model_dump()}
        )
        try:
            adapter.dispatch_event(bot, dispatch)
            await asyncio.gather(*adapter.tasks)
            bot.send.assert_awaited_once()
            message = bot.send.call_args.args[1]
            assert [segment.type for segment in message] == ["text", "file_image"]
            assert message[1].data["content"] == GENERATED
            assert requests == ["/v1/images/edits"]
        finally:
            await model.close()

    asyncio.run(run())

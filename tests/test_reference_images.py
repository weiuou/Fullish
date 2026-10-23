"""Search -> real vision -> reference edit, incoming references and sandbox image replies."""

import asyncio
import base64
import gzip
import json
import socket
import zlib
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from nonebot.adapters.qq import Bot
from test_sending import event

from fullish.conversation import Conversations
from fullish.generation import GeneratedImage
from fullish.generation.openai import OpenAIImageGenerator
from fullish.image_assets import ImageAssetError, ImageAssets
from fullish.model import ChatModel, ModelError
from fullish.qq import handle_group
from fullish.sending import Image, OutgoingMessage
from fullish.settings import ImageGenerationSettings, ModelSettings, ToolSettings
from fullish.tools import ToolContext, ToolRuntime
from fullish.tools.images import PublicImages, image_candidates
from fullish.tools.web import ToolError

GROUP = "g" * 32
CONTEXT = ToolContext(GROUP, "u", "incoming")
PNG = b"\x89PNG\r\n\x1a\nsynthetic-character-reference"
GENERATED = b"\x89PNG\r\n\x1a\nsynthetic-edited-meme"


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, port, *args, **kwargs):
        address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def tool(name, arguments, number=1):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": f"call-{number}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                }
            ]
        },
    )


def multipart(request):
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    return [
        (part.get_param("name", header="content-disposition"), part.get_payload(decode=True))
        for part in message.iter_parts()
    ]


@pytest.mark.parametrize("html_fallback", [False, True])
def test_search_to_reference_edit_delivers_actual_qq_attachment(
    public_dns, tmp_path, html_fallback
):
    async def run():
        requests = []
        count = 0

        def chat(request):
            nonlocal count
            count += 1
            payload = json.loads(request.content)
            if count == 1:
                return tool("web_search", {"query": "测试角色官方形象"}, count)
            if count == 2:
                return tool("web_fetch", {"url": "https://character.example/profile"}, count)
            if count == 3:
                page = json.loads(payload["messages"][-1]["content"])["data"]
                if html_fallback:
                    assert page["images"] == [] and "web_image" in page["image_hint"]
                    assert page["url"] in page["image_hint"]
                    return tool("web_image", {"url": page["url"]}, count)
                candidates = page["images"]
                assert candidates == [
                    {"url": "https://character.example/ref.png", "alt": "角色形象"}
                ]
                return tool("web_image", {"url": candidates[0]["url"]}, count)
            if count == 4 and html_fallback:
                page = json.loads(payload["messages"][-1]["content"])["data"]
                assert page["kind"] == "page"
                return tool("web_image", {"url": page["images"][0]["url"]}, count)
            assert count == (5 if html_fallback else 4)
            result = json.loads(payload["messages"][-2]["content"])["data"]
            visual = payload["messages"][-1]
            assert visual["role"] == "user"
            assert result["image_id"] in visual["content"][0]["text"]
            assert (
                base64.b64decode(visual["content"][1]["image_url"]["url"].split(",", 1)[1]) == PNG
            )
            return tool(
                "generate_image",
                {
                    "prompt": "保留角色外形，做开心表情包",
                    "caption": "开心！",
                    "reference_image_ids": [result["image_id"]],
                },
                count,
            )

        def web(request):
            assert request.headers["Authorization"] == "Bearer search-key"
            if request.url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    json={
                        "results": [{"title": "角色", "url": "https://character.example/profile"}]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "title": "角色",
                    "content": "正文提取省略了图片" if html_fallback else "![角色形象](/ref.png)",
                },
            )

        def image(request):
            assert request.url.host == "93.184.216.34"
            assert request.headers["Host"] == "character.example"
            assert request.extensions["sni_hostname"] == "character.example"
            assert "Authorization" not in request.headers
            if request.url.path == "/profile":
                return httpx.Response(
                    200,
                    text='<img src="/ref.png" alt="角色形象">',
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(200, content=PNG)

        def edit(request):
            requests.append(request.url.path)
            assert request.url.path == "/v1/images/edits"
            assert request.headers["Authorization"] == "Bearer cpa-key"
            fields = multipart(request)
            assert ("image[]", PNG) in fields
            assert ("model", b"gpt-image-2.5") in fields
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(GENERATED).decode()}]}
            )

        from fullish.tool_audit import ToolAudit

        runtime = ToolRuntime(
            ToolSettings(frozenset({GROUP}), "search-key"),
            web_transport=httpx.MockTransport(web),
            image_transport=httpx.MockTransport(image),
            audit=ToolAudit(tmp_path / "audit.db"),
        )
        connection = ModelSettings("http://localhost/v1", "cpa-key")
        generator = OpenAIImageGenerator(
            connection, ImageGenerationSettings(), transport=httpx.MockTransport(edit)
        )
        model = ChatModel(
            connection,
            transport=httpx.MockTransport(chat),
            tools=runtime,
            image_generator=generator,
        )
        incoming = event()
        incoming.group_openid = GROUP
        incoming.group_id = GROUP
        incoming.content = "Fullish，搜一下测试角色，用它的形象制作表情包"
        with (
            patch.object(
                Bot,
                "post_group_files",
                new=AsyncMock(return_value=SimpleNamespace(file_info="meme")),
            ) as upload,
            patch.object(Bot, "post_group_messages", new=AsyncMock()) as post,
        ):
            try:
                await handle_group(Conversations(model), object.__new__(Bot), incoming)
                upload.assert_awaited_once_with(
                    group_openid=GROUP, srv_send_msg=False, file_type=1, file_data=GENERATED
                )
                assert post.call_args.kwargs["content"] == "开心！"
                assert requests == ["/v1/images/edits"]
                # Only search/fetch snapshots are retained, never image bytes or data URLs.
                audit = (tmp_path / "audit.db").read_bytes()
                assert PNG not in audit and b"data:image" not in audit
            finally:
                await model.close()

    asyncio.run(run())


def test_incoming_image_is_forwarded_to_generator_with_reference_id():
    async def run():
        def chat(request):
            payload = json.loads(request.content)
            parts = payload["messages"][1]["content"]
            image_id = parts[1]["text"].split("image_id=", 1)[1].split("；", 1)[0]
            return tool(
                "generate_image",
                {"prompt": "改成开心表情", "caption": "", "reference_image_ids": [image_id]},
            )

        generator = Mock(
            generate=AsyncMock(return_value=GeneratedImage(GENERATED)), close=AsyncMock()
        )
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            transport=httpx.MockTransport(chat),
            image_generator=generator,
        )
        await model.media.aclose()
        model.media = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=PNG))
        )
        try:
            result = await model.reply(
                [{"role": "user", "content": "改图", "images": ["https://cdn.example/ref"]}],
                addressed=True,
            )
            assert result == OutgoingMessage((Image(GENERATED),))
            generator.generate.assert_awaited_once_with("改成开心表情", reference_images=(PNG,))
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_edit_failure_never_retries_or_falls_back_to_text_generation(status):
    async def run():
        requests = []

        def respond(request):
            requests.append(request.url.path)
            return httpx.Response(status, text="private upstream error")

        generator = OpenAIImageGenerator(
            ModelSettings("http://localhost/v1", "key"),
            ImageGenerationSettings(),
            transport=httpx.MockTransport(respond),
        )
        try:
            with pytest.raises(ModelError, match=f"^image_http_{status}$"):
                await generator.generate("编辑", reference_images=(PNG,))
            assert requests == ["/v1/images/edits"]
        finally:
            await generator.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://private.example/a",
        "file:///etc/passwd",
        "https://user:password@site.example/a",
        "https://site.example:8080/a",
        "http://localhost/a",
        "https://site.example\\@private.example/a",
    ],
)
def test_public_images_reject_unsafe_targets_without_contacting_them(public_dns, url, monkeypatch):
    if url.startswith("http://127."):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        )

    async def run():
        backend = Mock(return_value=httpx.Response(200, content=PNG))
        downloader = PublicImages(transport=httpx.MockTransport(backend))
        try:
            with pytest.raises(ToolError, match="^public_image_url_required$"):
                await downloader.inspect(url, ImageAssets())
            backend.assert_not_called()
        finally:
            await downloader.close()

    asyncio.run(run())


def test_redirects_are_revalidated_and_cookies_are_not_forwarded(public_dns):
    async def run():
        seen = []

        def backend(request):
            seen.append(request)
            assert "cookie" not in request.headers and "authorization" not in request.headers
            if len(seen) == 1:
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://second.example/ref",
                        "set-cookie": "private=secret; Path=/",
                    },
                )
            return httpx.Response(302, headers={"location": "http://private.example/secret"})

        downloader = PublicImages(transport=httpx.MockTransport(backend))
        try:
            with pytest.raises(ToolError, match="^public_image_url_required$"):
                await downloader.inspect("https://first.example/ref", ImageAssets())
            assert [request.headers["host"] for request in seen] == [
                "first.example",
                "second.example",
            ]
        finally:
            await downloader.close()

    asyncio.run(run())


def test_page_candidates_are_not_treated_as_viewed_images(public_dns):
    async def run():
        downloader = PublicImages(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    text=(
                        '<meta property="og:image" content="/ref.png">'
                        '<img src="//cdn.example/other.png">'
                    ),
                    headers={"content-type": "text/html"},
                )
            )
        )
        assets = ImageAssets()
        try:
            result = await downloader.inspect("https://page.example/profile", assets)
            assert result["kind"] == "page"
            assert result["images"][0]["url"] == "https://page.example/ref.png"
            assert not assets.items and "image_id" not in result
        finally:
            await downloader.close()

    asyncio.run(run())


def test_image_assets_are_reply_local_bounded_and_do_not_expose_bytes(monkeypatch):
    first, other = ImageAssets(), ImageAssets()
    asset = first.add(PNG)
    assert PNG.decode(errors="ignore") not in repr(asset)
    assert first.add(PNG) is asset
    with pytest.raises(ImageAssetError, match="image_reference_not_found"):
        other.get(asset.image_id)
    monkeypatch.setattr("fullish.image_assets.MAX_SESSION_IMAGES", 1)
    with pytest.raises(ImageAssetError, match="image_session_limit"):
        first.add(GENERATED)


def test_markdown_and_html_candidate_extraction_is_bounded():
    assert image_candidates(
        '![角色](../ref.png) <img src="data:image/png;base64,abc">', "https://page.example/a/b"
    ) == [{"url": "https://page.example/ref.png", "alt": "角色"}]
    assert (
        len(
            image_candidates(
                "".join(f'<img src="/{i}.png">' for i in range(30)), "https://page.example"
            )
        )
        == 10
    )


@pytest.mark.parametrize("case", ["oversize", "html_spoof", "encoding", "redirect_loop"])
def test_public_reads_cannot_create_false_visual_assets(public_dns, monkeypatch, case):
    monkeypatch.setattr("fullish.tools.images.MAX_DOWNLOAD_BYTES", 64)

    async def run():
        count = 0

        def backend(request):
            nonlocal count
            count += 1
            if case == "oversize":
                return httpx.Response(200, content=PNG + b"x" * 64)
            if case == "html_spoof":
                return httpx.Response(
                    200, content=b"<html>not an image</html>", headers={"content-type": "image/png"}
                )
            if case == "encoding":
                return httpx.Response(
                    200,
                    stream=httpx.ByteStream(b"compressed"),
                    headers={"content-encoding": "br"},
                )
            return httpx.Response(302, headers={"location": "/again"})

        assets = ImageAssets()
        downloader = PublicImages(transport=httpx.MockTransport(backend))
        expected = {
            "oversize": "image_download_too_large",
            "html_spoof": "image_download_invalid_format",
            "encoding": "image_download_encoding",
            "redirect_loop": "image_redirect_limit",
        }[case]
        try:
            with pytest.raises(ToolError, match=f"^{expected}$"):
                await downloader.inspect("https://site.example/image", assets)
            assert not assets.items
            assert count == (4 if case == "redirect_loop" else 1)
        finally:
            await downloader.close()

    asyncio.run(run())


def test_unknown_reference_never_reaches_paid_generator():
    async def run():
        generator = Mock(generate=AsyncMock(), close=AsyncMock())
        model = ChatModel(
            ModelSettings("http://localhost/v1", "key"),
            image_generator=generator,
            transport=httpx.MockTransport(
                lambda _: tool(
                    "generate_image",
                    {
                        "prompt": "编辑",
                        "caption": "",
                        "reference_image_ids": ["img_other_reply"],
                    },
                )
            ),
        )
        try:
            with pytest.raises(ModelError, match="^image_reference_not_found$"):
                await model.reply([], addressed=True)
            generator.generate.assert_not_awaited()
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("case", ["page", "image", "bomb", "truncated"])
def test_compressed_public_reads_limit_decoded_size(public_dns, monkeypatch, encoding, case):
    monkeypatch.setattr("fullish.tools.images.MAX_DOWNLOAD_BYTES", 128)
    raw = PNG if case == "image" else b'<img src="/ref.png">'
    if case == "bomb":
        raw = b"x" * 10000
    encoded = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    if case == "truncated":
        encoded = encoded[:-3]

    async def run():
        downloader = PublicImages(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    stream=httpx.ByteStream(encoded),
                    headers={
                        "content-encoding": encoding,
                        "content-type": "image/png" if case == "image" else "text/html",
                    },
                )
            )
        )
        assets = ImageAssets()
        try:
            if case in {"bomb", "truncated"}:
                code = "image_download_too_large" if case == "bomb" else "image_download_encoding"
                with pytest.raises(ToolError, match=f"^{code}$"):
                    await downloader.inspect("https://site.example/page", assets)
                assert not assets.items
            else:
                result = await downloader.inspect("https://site.example/page", assets)
                assert result["kind"] == ("image" if case == "image" else "page")
                if case == "image":
                    assert assets.get(result["image_id"]).body == PNG
                else:
                    assert result["images"][0]["url"] == "https://site.example/ref.png"
        finally:
            await downloader.close()

    asyncio.run(run())

"""Credential-free public image/page reads with DNS-pinned, checked redirects."""

import asyncio
import ipaddress
import re
import socket
import zlib
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from fullish.image_assets import ImageAssets
from fullish.media import sniff_image_type
from fullish.tools.errors import ToolError

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.candidates: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        row = dict(attrs)
        if tag == "meta" and (row.get("property") or row.get("name") or "").lower() in {
            "og:image",
            "og:image:url",
            "twitter:image",
            "twitter:image:src",
        }:
            self.candidates.insert(0, (row.get("content") or "", "网页预览图"))
        elif tag == "img":
            self.candidates.append(
                (row.get("data-src") or row.get("src") or "", row.get("alt") or "")
            )


def image_candidates(content: str, page_url: str) -> list[dict[str, str]]:
    parser = _Images()
    parser.feed(content)
    candidates = parser.candidates + [
        (match[1], match[0])
        for match in re.findall(
            r"!\[([^\]]*)\]\(\s*<?([^\s<>]+?)>?(?:\s+\"[^\"]*\")?\s*\)", content
        )
    ]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, alt in candidates:
        try:
            url = urljoin(page_url, unescape(source))
            parsed = urlsplit(url)
            if (
                not source
                or parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or len(url) > 2048
                or url in seen
            ):
                continue
        except ValueError:
            continue
        seen.add(url)
        results.append({"url": url, "alt": alt[:200]})
        if len(results) == 10:
            break
    return results


async def public_address(url: str) -> tuple[httpx.URL, str]:
    try:
        if any(ord(char) < 33 or char == "\\" for char in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or host.rstrip(".").lower().endswith(("localhost", ".local", ".internal"))
        ):
            raise ValueError
        rows = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise ValueError
        # Connecting to the checked IP prevents a second DNS lookup/rebinding.
        return httpx.URL(url), str(rows[0][4][0])
    except (ValueError, OSError, httpx.InvalidURL):
        raise ToolError("public_image_url_required") from None


class PublicImages:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(
            transport=transport,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            # Pinned IPs may serve different TLS hosts; do not reuse their connections.
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=8),
            headers={
                "Accept": "image/*,text/html;q=0.8",
                "Accept-Encoding": "gzip, deflate, identity",
            },
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def read_body(self, response: httpx.Response) -> bytes:
        encoding = (
            response.headers.get("content-encoding", "identity").strip().lower() or "identity"
        )
        if encoding not in {"identity", "gzip", "deflate"}:
            raise ToolError("image_download_encoding")
        # Injected transports can return an already decoded/consumed response.
        if response.is_stream_consumed:
            if len(response.content) > MAX_DOWNLOAD_BYTES:
                raise ToolError("image_download_too_large")
            return response.content
        decoder = (
            zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            if encoding != "identity"
            else None
        )
        body = bytearray()
        received = 0
        try:
            async for chunk in response.aiter_raw(chunk_size=65536):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise ToolError("image_download_too_large")
                decoded = (
                    decoder.decompress(chunk, MAX_DOWNLOAD_BYTES - len(body) + 1)
                    if decoder
                    else chunk
                )
                if len(body) + len(decoded) > MAX_DOWNLOAD_BYTES:
                    raise ToolError("image_download_too_large")
                body.extend(decoded)
            # A complete stream needs no unbounded flush. Reject concatenated or truncated streams.
            if decoder and (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
                raise ToolError("image_download_encoding")
        except zlib.error:
            raise ToolError("image_download_encoding") from None
        return bytes(body)

    async def inspect(self, url: str, assets: ImageAssets) -> dict:
        try:
            async with asyncio.timeout(15):
                for redirects in range(4):
                    original, address = await public_address(url)
                    target = original.copy_with(host=address)
                    # Preserve Host and TLS certificate verification for the original name.
                    request = self.client.build_request(
                        "GET",
                        target,
                        headers={"Host": original.netloc.decode("ascii")},
                        extensions={"sni_hostname": original.host},
                    )
                    request.headers.pop("cookie", None)
                    response = await self.client.send(request, stream=True)
                    self.client.cookies.clear()
                    try:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirects == 3 or not response.headers.get("location"):
                                raise ToolError("image_redirect_limit")
                            url = urljoin(str(original), response.headers["location"])
                            continue
                        response.raise_for_status()
                        raw = await self.read_body(response)
                        if sniff_image_type(raw):
                            return {"kind": "image", "url": url, **assets.add(raw).metadata()}
                        if "html" in response.headers.get("content-type", "").lower():
                            return {
                                "kind": "page",
                                "url": url,
                                "images": image_candidates(
                                    raw.decode("utf-8", errors="replace"), url
                                ),
                                "note": "尚未看图；用 web_image 打开选中的图片 URL。",
                            }
                        raise ToolError("image_download_invalid_format")
                    finally:
                        await response.aclose()
        except httpx.HTTPStatusError as error:
            raise ToolError(f"image_download_http_{error.response.status_code}") from None
        except (httpx.HTTPError, TimeoutError):
            raise ToolError("image_download_unavailable") from None
        raise ToolError("image_redirect_limit")

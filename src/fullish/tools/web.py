"""DeepSeek's standalone Search/Fetch APIs, separate from the chat provider."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from fullish.tools.errors import ToolError
from fullish.tools.images import image_candidates


async def public_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or host.rstrip(".").lower().endswith(("localhost", ".local", ".internal"))
            or any(ord(char) < 33 for char in url)
        ):
            raise ValueError
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
    except (ValueError, OSError):
        raise ToolError("public_url_required") from None


class DeepSeekWeb:
    def __init__(self, key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(
            base_url="https://api.deepseek.com/v1/tools/",
            headers={"Authorization": "Bearer " + key},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, operation: str, arguments: dict) -> dict:
        try:
            # The target website is fetched by DeepSeek, never by this credentialed client.
            async with self.client.stream("POST", operation, json=arguments) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ToolError("web_response_too_large")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except httpx.HTTPStatusError as error:
            raise ToolError(f"web_http_{error.response.status_code}") from None
        except (httpx.HTTPError, ValueError):
            raise ToolError("web_unavailable") from None

    async def search(self, query: str) -> dict:
        data = await self.request("search", {"query": query, "count": 5, "need_content": False})
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ToolError("invalid_search_response")
        results = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                raise ToolError("invalid_search_response")
            results.append(
                {
                    key: str(row.get(key) or "")[:limit]
                    for key, limit in (
                        ("title", 200),
                        ("url", 2000),
                        ("summary", 1000),
                        ("date", 64),
                    )
                }
            )
        return {
            "results": results,
            "image_hint": "搜索返回网页而非图片。需要参考图时，找到相关官网页面后直接用 "
            "web_image 打开该网页列出候选图，再打开选中的图片 URL；不要反复搜索图片直链。",
        }

    async def fetch(self, url: str) -> dict:
        await public_url(url)
        data = await self.request("fetch", {"url": url})
        if not isinstance(data.get("content"), str):
            raise ToolError("invalid_fetch_response")
        content = data["content"]
        result = {
            "url": url,
            "title": str(data.get("title", ""))[:200],
            "content": content[:10000],
            "truncated": len(content) > 10000,
        }
        result["images"] = image_candidates(content[:10000], url)
        if not result["images"]:
            result["image_hint"] = (
                "正文提取可能省略图片，不代表原网页没有图。需要参考图时，下一步调用 "
                f"web_image(url={url}) 读取原网页的候选图片，不必重新搜索。"
            )
        return result

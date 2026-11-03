"""Build bounded multimodal model input from a conversation snapshot."""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

from fullish.media.assets import ImageAssetError, ImageAssets
from fullish.reply.prompts import IMAGE_GENERATION_RULES, PERSONA
from fullish.reply.schema import Turn


class InputBuilder:
    def __init__(self, download: Callable[..., Awaitable[str | None]], *, image_generation: bool):
        self.download = download
        self.image_generation = image_generation

    async def build(
        self, turns: list[Turn], addressed: bool, *, images: ImageAssets | None = None
    ) -> list[dict[str, object]]:
        rules = PERSONA
        if self.image_generation:
            rules += "\n\n" + IMAGE_GENERATION_RULES
        messages: list[dict[str, object]] = [{"role": "system", "content": rules}]
        for item in turns:
            content = item["content"]
            source = item.get("source", "chat")
            if source != "chat":
                content = "[机器人通知：" + source + "]\n" + content
            quote = item.get("reply")
            if item["role"] == "user" and ("sender_id" in item or quote):
                envelope: dict[str, object] = {
                    "sender": {
                        "id": item.get("sender_id"),
                        "display_name": item.get("sender_name"),
                    },
                    "message_id": item.get("message_id"),
                    "sent_at": item.get("sent_at"),
                    "text": content,
                }
                if quote:
                    envelope["reply_to"] = {
                        "reference_id": quote.reference_id,
                        "sender": {"id": quote.sender_id, "display_name": quote.sender_name},
                        "text": quote.content.text,
                    }
                content = json.dumps(envelope, ensure_ascii=False)
            image_parts: list[dict[str, object]] = []
            if item["role"] == "user":
                image_index = 0
                for label, urls in (
                    ("[被引用消息 reply_to 的图片]", item.get("reply_images", []) if quote else []),
                    ("[当前消息的图片]", item.get("images", [])),
                ):
                    available: list[dict[str, object]] = []
                    for url in urls:
                        data = await self.download(
                            url, source_trace_id=item.get("trace_id", ""), image_index=image_index
                        )
                        image_index += 1
                        if data:
                            if images is not None:
                                try:
                                    asset = await asyncio.to_thread(
                                        images.add, base64.b64decode(data.split(",", 1)[1])
                                    )
                                except ImageAssetError:
                                    continue
                                available.extend(asset.vision())
                            elif data.startswith("data:image/gif;"):
                                try:
                                    asset = await asyncio.to_thread(
                                        ImageAssets().add, base64.b64decode(data.split(",", 1)[1])
                                    )
                                    available.extend(asset.vision())
                                except ImageAssetError:
                                    continue
                            else:
                                available.append({"type": "image_url", "image_url": {"url": data}})
                    if available:
                        if quote:
                            image_parts.append({"type": "text", "text": label})
                        image_parts.extend(available)
            if image_parts:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": content},
                            *image_parts,
                        ],
                    }
                )
            else:
                messages.append({"role": item["role"], "content": content})
        messages.append(
            {
                "role": "user",
                "content": f"[是否在叫 Fullish：{'是' if addressed else '否'}]",
            }
        )
        return messages

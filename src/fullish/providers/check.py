"""Explicit operator smoke test; sends one tiny request, never run by health checks."""

import asyncio
import json

from fullish.errors import ModelError
from fullish.providers.chat import ChatProvider
from fullish.reply.engine import ReplyEngine
from fullish.reply.media import ImageReader
from fullish.settings import ModelSettings


async def check() -> None:
    settings = ModelSettings.from_env()
    if settings is None:
        raise SystemExit("Configure AI_BASE_URL and AI_API_KEY first")
    provider = ChatProvider(settings)
    reader = ImageReader()
    engine = ReplyEngine(provider, reader)
    try:
        if not await provider.available():
            raise SystemExit("Configured model is not available from CPA")
        response = await engine.reply(
            [{"role": "user", "content": "只回复：连接成功"}], addressed=True
        )
        print(json.dumps({"model": settings.model, "reply": response}, ensure_ascii=False))
    except ModelError as error:
        raise SystemExit(str(error)) from None
    finally:
        await reader.close()
        await provider.close()


if __name__ == "__main__":
    asyncio.run(check())

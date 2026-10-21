"""Private messages must never reach NoneBot or the model."""

import os
import subprocess
import sys
from unittest.mock import Mock, patch

from nonebot.adapters.qq import Adapter, Bot, Message, MessageSegment
from nonebot.adapters.qq.models import Dispatch

from fullish.collection.qq import describe
from fullish.qq import GroupOnlyAdapter


def test_private_dispatch_is_discarded() -> None:
    adapter = object.__new__(GroupOnlyAdapter)
    bot = Mock(spec=Bot)
    with patch.object(Adapter, "dispatch_event") as dispatch:
        for kind in ("C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
            payload = Dispatch.model_validate({"op": 0, "s": 1, "t": kind, "d": {}})
            GroupOnlyAdapter.dispatch_event(adapter, bot, payload)
        dispatch.assert_not_called()
        payload = Dispatch.model_validate({"op": 0, "s": 2, "t": "GROUP_MESSAGE_CREATE", "d": {}})
        adapter.dispatch_event(bot, payload)
        dispatch.assert_called_once_with(bot, payload)


def test_message_content_keeps_stickers_and_images() -> None:
    message = (
        MessageSegment.text("看这个 ")
        + MessageSegment.image("https://cdn.example/1.png")
        + MessageSegment.emoji("1")
        + MessageSegment.text(" 哈哈")
    )
    text, images = describe(message)
    # get_plaintext() would drop everything but "看这个 哈哈".
    assert text == "看这个 [图片][表情] 哈哈"
    assert images == ["https://cdn.example/1.png"]
    assert describe(Message(MessageSegment.image("https://cdn.example/2.png"))) == (
        "[图片]",
        ["https://cdn.example/2.png"],
    )


def test_configured_qq_driver_and_model_lifecycle() -> None:
    # Isolate NoneBot's process-global driver, and prevent live QQ traffic in tests.
    code = """
from unittest.mock import AsyncMock, patch
import httpx
import nonebot
from fastapi.testclient import TestClient
from fullish.app import create_app
from fullish.qq import GroupOnlyAdapter
with patch.object(GroupOnlyAdapter, 'startup', new_callable=AsyncMock):
    app = create_app()
    adapter = nonebot.get_adapter(GroupOnlyAdapter)
    assert adapter.qq_config.qq_bots[0].intent.to_int() == 1 << 25
    assert adapter.qq_config.qq_bots[0].use_websocket
    app.state.model.client = httpx.AsyncClient(
        base_url='http://localhost/v1/',
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'data': [{'id': 'gemini-3.8-flash-high'}]})))
    with TestClient(app) as client:
        assert client.get('/health/ready').status_code == 200
        result = client.get('/health/dependencies').json()
        assert result['model_available'] is True
        assert result['qq_enabled'] is True
        assert result['qq_connected'] is False
        assert client.get('/version').json()['mode'] == 'qq'
    assert app.state.model.client.is_closed
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "AI_BASE_URL": "http://localhost/v1",
            "AI_API_KEY": "fake-key",
            "AI_MODEL": "gemini-3.8-flash-high",
            "QQ_ENABLED": "true",
            "QQ_APP_ID": "1234",
            "QQ_APP_SECRET": "fake-secret",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

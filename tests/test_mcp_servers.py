"""Live MCP registration and real SDK calls without third-party services or credentials."""

import asyncio
import base64
import io
import json
import sys
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool
from nonebot.adapters.qq import Bot
from PIL import Image

from fullish.mcp.runtime import MCPRegistry, ServerWorker, tool_name
from fullish.mcp.store import MCPConfigError, MCPStore, ServerConfig
from fullish.media.assets import ImageAssets
from fullish.platforms.qq.menu import DeveloperMenu
from fullish.settings import ToolSettings
from fullish.tools.runtime import ToolContext, ToolRuntime
from fullish.tools.web import ToolError
from tests.helpers import make_management
from tests.test_group_control import private_payload

GROUP = "a" * 32
OTHER = "b" * 32
CONTEXT = ToolContext(GROUP, "member", "message")


def command(control, text, user="developer"):
    return control.command(user, uuid.uuid4().hex, text)


def setup_control(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_BASE_URL", "https://model.example/v1")
    monkeypatch.delenv("QQ_ALLOWED_GROUPS", raising=False)
    control = make_management(tmp_path)
    command(control, "绑定开发者 " + control.identity.pair())
    control.observe(GROUP)
    command(control, "选择 1")
    return control


def test_menu_requires_developer_and_explicit_group_grant(tmp_path, monkeypatch):
    control = setup_control(tmp_path, monkeypatch)
    assert command(control, "MCP 添加 notes https://notes.example/mcp", "attacker") is None
    assert not MCPStore(control.store.directory).configs()
    assert "已添加" in (command(control, "MCP 添加 notes https://notes.example/mcp") or "")
    assert MCPStore(control.store.directory).configs()["notes"].groups == []
    assert "未更新" in (command(control, "MCP 添加 notes https://replacement.example/mcp") or "")
    command(control, "MCP 授权 notes")
    assert MCPStore(tmp_path).configs()["notes"].groups == [GROUP]
    command(control, "MCP 限定 notes lookup,save")
    assert MCPStore(control.store.directory).configs()["notes"].tools == ["lookup", "save"]
    command(control, "MCP 停用 notes")
    assert not MCPStore(control.store.directory).configs()["notes"].enabled
    command(control, "MCP 启用 notes")
    command(control, "MCP 撤销 notes")
    assert MCPStore(control.store.directory).configs()["notes"].enabled
    assert MCPStore(control.store.directory).configs()["notes"].groups == []
    command(control, "MCP 删除 notes")
    assert not MCPStore(control.store.directory).configs()


@pytest.mark.parametrize(
    "endpoint", ["http://localhost/mcp", "https://user:pass@example.com/mcp", "env:AI_API_KEY"]
)
def test_menu_rejects_invalid_endpoints_without_echoing_them(tmp_path, monkeypatch, endpoint):
    control = setup_control(tmp_path, monkeypatch)
    reply = command(control, "MCP 添加 notes " + endpoint)
    assert reply is not None and endpoint not in reply and "未更新" in reply
    assert not MCPStore(control.store.directory).configs()


def test_menu_credentials_stay_in_server_environment(tmp_path, monkeypatch):
    control = setup_control(tmp_path, monkeypatch)
    endpoint = "https://notes.example/mcp?key=private-token"
    assert "服务器" in (command(control, "MCP 添加 notes " + endpoint) or "")
    monkeypatch.setenv("MCP_NOTES_URL", endpoint)
    assert "已添加" in (command(control, "MCP 添加 notes env:MCP_NOTES_URL") or "")
    assert endpoint == MCPStore(control.store.directory).configs()["notes"].endpoint()
    assert "private-token" not in (command(control, "MCP 列表") or "")
    assert b"private-token" not in MCPStore(control.store.directory).path.read_bytes()


def test_real_discovery_routing_results_and_reuse_without_wallet_wrappers(tmp_path):
    server = MCPServer("ordinary", log_level="ERROR")
    calls, connections = [], []

    @server.tool()
    async def lookup(query: str) -> str:
        calls.append(query)
        return "found " + query

    @server.tool()
    async def plain_json() -> dict[str, object]:
        return {"items": ["ordinary data"]}

    @server.tool()
    async def failure() -> CallToolResult:
        return CallToolResult(
            is_error=True, content=[TextContent(type="text", text="use another query")]
        )

    @asynccontextmanager
    async def connector(config):
        connections.append(config.name)
        async with Client(server, cache=None) as client:
            yield client

    async def run():
        store = MCPStore(tmp_path)
        registry = MCPRegistry(store, connector=connector)
        runtime = ToolRuntime(ToolSettings(frozenset()), mcp=registry, audit=Mock())
        try:
            await runtime.prepare(CONTEXT)
            assert runtime.schemas(CONTEXT) == []
            # Registration and grants take effect in the already running runtime.
            store.put(ServerConfig(name="notes", url="https://notes.example/mcp", groups=[GROUP]))
            await runtime.prepare(CONTEXT)
            assert len(runtime.schemas(CONTEXT)) == 3
            assert runtime.schemas(ToolContext(OTHER, "member", "message")) == []
            name = tool_name("notes", "lookup")
            invalid = json.loads(await runtime.invoke(name, '{"query":3}', CONTEXT))
            assert not invalid["ok"] and not calls
            result = json.loads(await runtime.invoke(name, '{"query":"hi"}', CONTEXT))
            assert result["ok"] and "found hi" in json.dumps(result["data"])
            result = json.loads(
                await runtime.invoke(tool_name("notes", "plain_json"), "{}", CONTEXT)
            )
            assert result["data"]["structured_content"] == {"items": ["ordinary data"]}
            result = json.loads(await runtime.invoke(tool_name("notes", "failure"), "{}", CONTEXT))
            assert not result["ok"] and "use another query" in json.dumps(result)
            assert connections == ["notes"]  # Discovery and calls share one owned connection.
            store.change("notes", "revoke", GROUP)
            assert runtime.schemas(CONTEXT) == []
            assert not json.loads(await runtime.invoke(name, '{"query":"blocked"}', CONTEXT))["ok"]
            assert calls == ["hi"]
            store.change("notes", "grant", GROUP)
            store.change("notes", "tools", ["plain_json"])
            assert not runtime.schemas(CONTEXT)  # Old schemas cannot outlive a config change.
            await runtime.prepare(CONTEXT)
            assert [s["function"]["name"] for s in runtime.schemas(CONTEXT)] == [
                tool_name("notes", "plain_json")
            ]
            store.change("notes", "delete")
            assert not runtime.schemas(CONTEXT)
        finally:
            await runtime.close()
        assert not registry.workers

    asyncio.run(run())


def test_pagination_same_named_tools_and_broken_server_are_independent(tmp_path):
    tool = Tool(name="search", input_schema={"type": "object", "additionalProperties": False})
    pages = []

    class Backend:
        async def list_tools(self, *, cursor=None):
            pages.append(cursor)
            return ListToolsResult(
                tools=[tool] if cursor else [], next_cursor=None if cursor else "page2"
            )

        async def call_tool(self, name, args):
            return CallToolResult(content=[TextContent(type="text", text="result")])

    @asynccontextmanager
    async def connector(config):
        if config.name == "broken":
            raise OSError("never expose server details or credentials")
        yield Backend()

    async def run():
        store = MCPStore(tmp_path)
        for name in ["one", "two", "broken"]:
            store.put(ServerConfig(name=name, url="https://test.example/mcp", groups=[GROUP]))
        registry = MCPRegistry(store, connector=connector)
        try:
            await registry.prepare(GROUP)
            assert {s["function"]["name"] for s in registry.schemas(GROUP)} == {
                tool_name("one", "search"),
                tool_name("two", "search"),
            }
            assert pages.count("page2") == 2
            assert "连接成功" in await registry.inspect("one")
            assert "检查失败" in await registry.inspect("broken")
        finally:
            await registry.close()

    asyncio.run(run())


def test_private_inspection_is_authorized_deduplicated_and_bypasses_model(tmp_path, monkeypatch):
    control = setup_control(tmp_path, monkeypatch)

    async def run():
        registry = Mock(inspect=AsyncMock(return_value="MCP notes：连接成功"))
        menu = DeveloperMenu(control, mcp=registry)
        bot = Mock(spec=Bot, post_c2c_messages=AsyncMock())
        await menu.handle(bot, private_payload("MCP 检查 notes", user="attacker"))
        registry.inspect.assert_not_awaited()
        payload = private_payload("MCP 检查 notes", user="developer")
        await menu.handle(bot, payload)
        await menu.handle(bot, payload)
        registry.inspect.assert_awaited_once_with("notes")
        bot.post_c2c_messages.assert_awaited_once()

    asyncio.run(run())


def test_real_stdio_discovery_and_call(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        "from mcp.server import MCPServer\n"
        "s=MCPServer('test', log_level='ERROR')\n"
        "@s.tool()\n"
        "async def greet(name: str) -> str: return 'hello '+name\n"
        "s.run(transport='stdio')\n",
        encoding="utf-8",
    )

    async def run():
        store = MCPStore(tmp_path)
        store.put(
            ServerConfig(
                name="local",
                transport="stdio",
                command=sys.executable,
                args=[str(script)],
                groups=[GROUP],
            )
        )
        registry = MCPRegistry(store)
        try:
            await registry.prepare(GROUP)
            result = await registry.invoke(tool_name("local", "greet"), '{"name":"bot"}', GROUP)
            assert "hello bot" in json.dumps(result)
        finally:
            await registry.close()

    asyncio.run(run())


def test_cancelled_queued_calls_do_not_run_and_failures_are_not_retried():
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class Backend:
        async def call_tool(self, name, args):
            calls.append(name)
            entered.set()
            await release.wait()
            raise OSError("connection lost after side effect")

    @asynccontextmanager
    async def connector(config):
        yield Backend()

    async def run():
        worker = ServerWorker(ServerConfig(name="one", url="https://test.example/mcp"), connector)
        first = asyncio.create_task(worker.request("call_tool", "write", {}))
        await entered.wait()
        second = asyncio.create_task(worker.request("call_tool", "cancelled", {}))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        release.set()
        with pytest.raises(ToolError, match="outcome_unknown"):
            await first
        await worker.close()
        assert calls == ["write"]

    asyncio.run(run())


def test_invalid_store_fails_closed(tmp_path):
    store = MCPStore(tmp_path)
    store.path.write_bytes(b"damaged")
    with pytest.raises(MCPConfigError):
        store.configs()


def test_names_are_stable_unique_and_fit_model_limits():
    original = "search.with.dots/" + "long" * 30
    assert len(tool_name("a" * 20, original)) <= 64
    assert tool_name("a", original) != tool_name("a", original + "other")
    assert tool_name("a", "search") != tool_name("b", "search")


def test_actual_http_protocol_and_server_environment_headers(tmp_path, monkeypatch):
    server = MCPServer("http-test", log_level="ERROR")

    @server.tool()
    async def lookup(query: str) -> str:
        return "found " + query

    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    requests = []

    async def instrumented(scope, receive, send):
        requests.append(dict(scope["headers"]))
        await app(scope, receive, send)

    original = httpx2.AsyncClient

    def http_client(**kwargs):
        return original(transport=httpx2.ASGITransport(app=instrumented), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", http_client)
    monkeypatch.setenv("MCP_HTTP_AUTH", "Bearer dedicated-test-token")

    async def run():
        store = MCPStore(tmp_path)
        store.put(
            ServerConfig(
                name="remote",
                url="https://localhost/mcp",
                groups=[GROUP],
                headers_env={"Authorization": "MCP_HTTP_AUTH"},
            )
        )
        registry = MCPRegistry(store)
        async with app.router.lifespan_context(app):
            try:
                await registry.prepare(GROUP)
                result = await registry.invoke(
                    tool_name("remote", "lookup"), '{"query":"bot"}', GROUP
                )
                assert "found bot" in json.dumps(result)
                assert requests and all(
                    r[b"authorization"] == b"Bearer dedicated-test-token" for r in requests
                )
            finally:
                await registry.close()

    asyncio.run(run())


def test_mcp_image_results_use_existing_image_assets(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format="PNG")
    server = MCPServer("image-test", log_level="ERROR")

    @server.tool()
    async def picture() -> CallToolResult:
        return CallToolResult(
            content=[
                ImageContent(
                    type="image",
                    mime_type="image/png",
                    data=base64.b64encode(stream.getvalue()).decode(),
                )
            ]
        )

    @asynccontextmanager
    async def connector(config):
        async with Client(server) as client:
            yield client

    async def run():
        store = MCPStore(tmp_path)
        store.put(ServerConfig(name="pictures", url="https://test.example/mcp", groups=[GROUP]))
        registry = MCPRegistry(store, connector=connector)
        images = ImageAssets()
        try:
            await registry.prepare(GROUP)
            data = await registry.invoke(
                tool_name("pictures", "picture"), "{}", GROUP, images=images
            )
            assert len(images.items) == 1
            assert data["content"][0]["image_id"] in images.items
        finally:
            await registry.close()

    asyncio.run(run())


def test_revocation_is_checked_when_queued_request_starts():
    entered, release = asyncio.Event(), asyncio.Event()
    authorized = True
    calls = []

    class Backend:
        async def call_tool(self, name, args):
            calls.append(name)
            entered.set()
            await release.wait()
            return "done"

    @asynccontextmanager
    async def connector(config):
        yield Backend()

    async def run():
        nonlocal authorized
        worker = ServerWorker(ServerConfig(name="notes", url="https://test.example/mcp"), connector)
        try:
            first = asyncio.create_task(worker.request("call_tool", "accepted", {}))
            await entered.wait()
            second = asyncio.create_task(
                worker.request("call_tool", "queued", {}, _allowed=lambda: authorized)
            )
            await asyncio.sleep(0)
            authorized = False
            release.set()
            assert await first == "done"
            with pytest.raises(ToolError, match="not_available"):
                await second
            assert calls == ["accepted"]
        finally:
            await worker.close()

    asyncio.run(run())

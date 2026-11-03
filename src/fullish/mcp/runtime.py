"""Generic MCP tools with live registration, dedicated session owners and bounded results."""

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from fullish.mcp.store import MCPConfigError, MCPStore, ServerConfig
from fullish.media.assets import MAX_ASSET_BYTES, ImageAssets
from fullish.telemetry import emit
from fullish.tools.web import ToolError


@asynccontextmanager
async def connect(config: ServerConfig):
    async with AsyncExitStack() as stack:
        if config.transport == "stdio":
            server = StdioServerParameters(
                command=config.command, args=config.args, env=config.environment(config.env)
            )
        else:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=config.environment(config.headers_env),
                    timeout=httpx2.Timeout(config.timeout),
                    trust_env=False,
                    follow_redirects=False,
                )
            )
            server = streamable_http_client(
                config.endpoint(), http_client=http, max_sse_event_size=262144
            )
        yield await stack.enter_async_context(
            Client(server, read_timeout_seconds=config.timeout, cache=None)
        )


class ServerWorker:
    """One task owns each SDK context from entry through exit, including cancellation."""

    def __init__(self, config: ServerConfig, connector=connect):
        self.config = config
        self.connector = connector
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self.task: asyncio.Task | None = None

    async def request(self, method: str, *args, _allowed=None, **kwargs):
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((future, method, args, kwargs, _allowed))
        except asyncio.QueueFull:
            raise ToolError("mcp_server_busy") from None
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        return await future

    async def run(self):
        pending = None
        try:
            while True:
                pending = await self.queue.get()
                if pending[0].cancelled():
                    continue
                try:
                    async with AsyncExitStack() as stack:
                        async with asyncio.timeout(self.config.timeout):
                            client = await stack.enter_async_context(self.connector(self.config))
                        while True:
                            future, method, args, kwargs, allowed = pending
                            if not future.cancelled():
                                if allowed is not None and not allowed():
                                    future.set_exception(ToolError("mcp_tool_not_available"))
                                else:
                                    async with asyncio.timeout(self.config.timeout):
                                        result = await getattr(client, method)(*args, **kwargs)
                                    if not future.done():
                                        future.set_result(result)
                            pending = None
                            try:
                                pending = await asyncio.wait_for(self.queue.get(), timeout=60)
                            except TimeoutError:
                                break
                except Exception:
                    if pending is not None and not pending[0].done():
                        pending[0].set_exception(
                            ToolError("mcp_server_unavailable_outcome_unknown")
                        )
                    emit("tool.mcp.unavailable", level="WARNING", server=self.config.name)
        finally:
            if pending is not None and not pending[0].done():
                pending[0].set_exception(ToolError("mcp_server_changed_or_closed"))
            while not self.queue.empty():
                future, _, _, _, _ = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(ToolError("mcp_server_changed_or_closed"))

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@dataclass
class ToolBinding:
    server: str
    original: str
    schema: dict
    validator: Draft202012Validator


def tool_name(server: str, original: str) -> str:
    name = f"mcp_{server}__{original}"
    if len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", original)[:25]
    return f"mcp_{server}__{slug}_{hashlib.sha256(original.encode()).hexdigest()[:10]}"


def validate_schema(schema):
    # Tool schemas are self-contained. Discovery must not enable network $ref resolution.
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(value, str) or not value.startswith("#")
            ):
                raise ToolError("mcp_external_schema_reference")
            validate_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            validate_schema(value)


class MCPRegistry:
    def __init__(self, store: MCPStore, *, connector=connect):
        self.store = store
        self.connector = connector
        self.configs: dict[str, ServerConfig] = {}
        self.workers: dict[str, ServerWorker] = {}
        self.bindings: dict[str, ToolBinding] = {}
        self.discovered: set[str] = set()
        self.lock = asyncio.Lock()

    async def refresh(self):
        try:
            configs = self.store.configs()
        except MCPConfigError:
            configs = {}
        for name in list(self.workers):
            if configs.get(name) != self.configs.get(name) or not configs[name].enabled:
                await self.workers.pop(name).close()
                self.discovered.discard(name)
                self.bindings = {key: b for key, b in self.bindings.items() if b.server != name}
        self.configs = configs

    async def discover(self, name: str):
        if name in self.discovered:
            return
        config = self.configs[name]
        worker = self.workers.setdefault(name, ServerWorker(config, self.connector))
        tools = []
        cursor = None
        for _ in range(10):
            result = (
                await worker.request("list_tools", cursor=cursor)
                if cursor
                else await worker.request("list_tools")
            )
            tools.extend(result.tools)
            if len(tools) > 256:
                raise ToolError("mcp_tool_limit")
            cursor = result.next_cursor
            if not cursor:
                break
        else:
            raise ToolError("mcp_tool_limit")
        bindings = {}
        for tool in tools:
            if config.tools is not None and tool.name not in config.tools:
                continue
            validate_schema(tool.input_schema)
            Draft202012Validator.check_schema(tool.input_schema)
            name_in_model = tool_name(name, tool.name)
            if name_in_model in bindings:
                raise ToolError("mcp_duplicate_tool")
            schema = {
                "type": "function",
                "function": {
                    "name": name_in_model,
                    "description": (tool.description or "")[:2000],
                    "parameters": tool.input_schema,
                },
            }
            if len(json.dumps(schema)) > 32768:
                raise ToolError("mcp_schema_too_large")
            bindings[name_in_model] = ToolBinding(
                name, tool.name, schema, Draft202012Validator(tool.input_schema)
            )
        if len(bindings) > 64:
            raise ToolError("mcp_tool_limit")
        self.bindings = {key: b for key, b in self.bindings.items() if b.server != name}
        self.bindings.update(bindings)
        self.discovered.add(name)

    def allowed(self, group: str) -> bool:
        try:
            return any(c.enabled and group in c.groups for c in self.store.configs().values())
        except MCPConfigError:
            return False

    async def prepare(self, group: str):
        async with self.lock:
            await self.refresh()

            async def load(name):
                try:
                    await self.discover(name)
                except (ToolError, SchemaError, ValueError):
                    emit("tool.mcp.unavailable", level="WARNING", server=name)

            await asyncio.gather(
                *(load(c.name) for c in self.configs.values() if c.enabled and group in c.groups)
            )

    def schemas(self, group: str) -> list[dict]:
        # Read on every invocation as well as discovery: revocations apply mid-turn.
        try:
            configs = self.store.configs()
        except MCPConfigError:
            return []
        return [
            binding.schema
            for _, binding in sorted(self.bindings.items())
            if (c := configs.get(binding.server)) is not None
            and c.enabled
            and group in c.groups
            and c == self.configs.get(binding.server)
        ][:96]

    async def invoke(
        self,
        name: str,
        raw: str,
        group: str,
        *,
        images: ImageAssets | None = None,
        authorized: Callable[[], bool] | None = None,
    ) -> dict:
        if name not in {s["function"]["name"] for s in self.schemas(group)}:
            raise ToolError("mcp_tool_not_available")
        binding = self.bindings[name]
        try:
            arguments = json.loads(raw)
            binding.validator.validate(arguments)
        except (ValueError, ValidationError):
            raise ToolError("invalid_tool_arguments") from None
        if not isinstance(arguments, dict):
            raise ToolError("invalid_tool_arguments")
        result = await self.workers[binding.server].request(
            "call_tool",
            binding.original,
            arguments,
            _allowed=lambda: (
                (authorized is None or authorized())
                and name in {s["function"]["name"] for s in self.schemas(group)}
            ),
        )
        content = []
        for item in result.content:
            if item.type == "text":
                content.append({"type": "text", "text": item.text})
            elif item.type == "resource_link":
                content.append({"type": "resource_link", "uri": str(item.uri), "name": item.name})
            elif item.type == "image" and images is not None:
                if len(item.data) > (MAX_ASSET_BYTES + 2) // 3 * 4:
                    raise ToolError("mcp_image_too_large")
                body = base64.b64decode(item.data, validate=True)
                asset = await asyncio.to_thread(images.add, body)
                content.append({"type": "image", **asset.metadata()})
            elif item.type == "resource" and hasattr(item.resource, "text"):
                content.append(
                    {"type": "resource", "uri": str(item.resource.uri), "text": item.resource.text}
                )
            else:
                content.append({"type": item.type, "omitted": True})
        data = {
            "is_error": result.is_error,
            "content": content,
            "structured_content": result.structured_content,
        }
        if len(json.dumps(data, ensure_ascii=False).encode()) > 32768:
            raise ToolError("mcp_result_too_large")
        return data

    async def inspect(self, name: str) -> str:
        async with self.lock:
            await self.refresh()
            if name not in self.configs or not self.configs[name].enabled:
                return "MCP 不存在或已停用。"
            self.discovered.discard(name)
            self.bindings = {key: b for key, b in self.bindings.items() if b.server != name}
            try:
                await self.discover(name)
            except (ToolError, SchemaError, ValueError):
                return "MCP 检查失败，请检查地址、服务器环境和连接。"
            names = [b.original for b in self.bindings.values() if b.server == name]
            return f"MCP {name}：连接成功，{len(names)} 个已开放工具。\n" + "\n".join(names)

    async def close(self):
        await asyncio.gather(*(worker.close() for worker in self.workers.values()))
        self.workers.clear()

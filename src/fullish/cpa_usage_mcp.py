"""Read-only stdio MCP for the co-located CPA; never exports management access."""

import argparse
import json
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from fullish.cpa_usage import CPAUsage, CPAUsageError, CPAUsageSettings


def create_server(
    settings: CPAUsageSettings, *, client_factory: Callable[[CPAUsageSettings], CPAUsage] = CPAUsage
):
    @asynccontextmanager
    async def lifespan(_server):
        client = client_factory(settings)
        try:
            yield {"usage": client}
        finally:
            await client.close()

    server = MCPServer("Fullish CPA Usage", lifespan=lifespan, log_level="WARNING")
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

    async def execute(ctx, method, **kwargs):
        try:
            data = await getattr(ctx.request_context.lifespan_context["usage"], method)(**kwargs)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                structured_content=data,
            )
        except CPAUsageError as error:
            return CallToolResult(
                is_error=True, content=[TextContent(type="text", text=str(error))]
            )
        except Exception:
            return CallToolResult(
                is_error=True, content=[TextContent(type="text", text="cpa_usage_unavailable")]
            )

    @server.tool(annotations=annotations)
    async def cpa_subscriptions(
        ctx: Context,
        provider: Annotated[str, Field(max_length=32)] = "all",
        offset: Annotated[int, Field(ge=0, le=500)] = 0,
        limit: Annotated[int, Field(ge=1, le=20)] = 20,
    ) -> CallToolResult:
        """列出 CPA 的 AI 订阅账号代号、提供商与状态，不返回邮箱或认证信息。"""
        return await execute(ctx, "subscriptions", provider=provider, offset=offset, limit=limit)

    @server.tool(annotations=annotations)
    async def cpa_subscription_quota(
        ctx: Context,
        provider: Annotated[str, Field(max_length=32)] = "all",
        account: Annotated[str, Field(max_length=32)] = "",
        offset: Annotated[int, Field(ge=0, le=500)] = 0,
        limit: Annotated[int, Field(ge=1, le=3)] = 3,
    ) -> CallToolResult:
        """查询 AI 订阅共享额度、已用比例和重置时间，缓存 60 秒；支持按账号代号或提供商查询。

        null/unknown 表示未查到，不能说成额度充足。该额度包含其他客户端使用，不是 Fullish 独占。
        """
        return await execute(
            ctx, "quotas", provider=provider, account=account, offset=offset, limit=limit
        )

    @server.tool(annotations=annotations)
    async def cpa_request_usage(ctx: Context) -> CallToolResult:
        """查看 CPA 提供的本地累计请求与 token 统计。统计可能未开启或随重启清零，不代表订阅额度。"""
        return await execute(ctx, "request_usage")

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register",
        action="store_true",
        help="Register the built-in server without granting groups",
    )
    args = parser.parse_args()
    if args.register:
        from fullish.mcp_servers import MCPStore, ServerConfig

        directory = os.environ.get("GROUP_CONTROL_DIR")
        if not directory:
            raise SystemExit("GROUP_CONTROL_DIR is not configured")
        environment = {
            "CPA_BASE_URL": "MCP_CPA_BASE_URL",
            "CPA_MANAGEMENT_KEY_FILE": "MCP_CPA_MANAGEMENT_KEY_FILE",
        }
        if os.environ.get("MCP_CPA_UNIX_SOCKET"):
            environment["CPA_UNIX_SOCKET"] = "MCP_CPA_UNIX_SOCKET"
        MCPStore(Path(directory)).put(
            ServerConfig(
                name="cpa_usage",
                transport="stdio",
                command=sys.executable,
                args=["-m", "fullish.cpa_usage_mcp"],
                env=environment,
                timeout=30,
            ),
            create=True,
        )
        print(
            "Registered cpa_usage; no groups granted. "
            "Use the private developer menu to authorize access."
        )
        return
    try:
        settings = CPAUsageSettings.from_env()
    except CPAUsageError:
        raise SystemExit(
            "Configure CPA_BASE_URL and the private CPA management credential before starting"
        ) from None
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()

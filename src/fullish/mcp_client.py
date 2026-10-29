"""MCP tool discovery/calls over an operator-controlled stdio connection."""

import asyncio
import json
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp_types import RequestParamsMeta

from fullish.settings import WalletSettings
from fullish.tools.web import ToolError


class StdioMCP:
    def __init__(self, server, allowed_tools: frozenset[str]):
        self.server = server
        self.allowed_tools = allowed_tools
        self.schemas: list[dict] = []
        # A signer unlock uses substantial scrypt memory. Serialize subprocesses
        # on the shared small host and avoid concurrent journal writers.
        self.lock = asyncio.Lock()

    async def discover(self) -> None:
        if self.schemas:
            return
        try:
            async with (
                self.lock,
                Client(self.server, read_timeout_seconds=30, cache=None) as client,
            ):
                result = await client.list_tools()
                if result.next_cursor:
                    raise ToolError("mcp_unexpected_tool_pagination")
                self.schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema,
                        },
                    }
                    for tool in result.tools
                    if tool.name in self.allowed_tools
                ]
                if {item["function"]["name"] for item in self.schemas} != self.allowed_tools:
                    self.schemas = []
                    raise ToolError("mcp_missing_tools")
        except ToolError:
            raise
        except Exception:
            raise ToolError("mcp_unavailable") from None

    async def call(
        self, name: str, arguments: dict, *, meta: RequestParamsMeta | None = None
    ) -> dict:
        if name not in self.allowed_tools:
            raise ToolError("mcp_tool_not_allowed")
        try:
            # Each call owns its process and cancellation scopes. The persistent
            # wallet journal, rather than the MCP connection, owns payment state.
            async with (
                self.lock,
                Client(self.server, read_timeout_seconds=30, cache=None) as client,
            ):
                result = await client.call_tool(name, arguments, meta=meta)
        except ToolError:
            raise
        except Exception:
            raise ToolError("mcp_unavailable_check_wallet_status") from None
        data = result.structured_content
        if result.is_error or not isinstance(data, dict):
            raise ToolError("mcp_tool_failed_check_wallet_status")
        if data.get("ok") is not True:
            code = data.get("error", "mcp_tool_failed")
            raise ToolError(code if isinstance(code, str) else "mcp_tool_failed")
        if not isinstance(data.get("data"), dict):
            raise ToolError("mcp_invalid_tool_result")
        return data["data"]


class WalletMCPClient:
    def __init__(self, settings: WalletSettings, *, server=None):
        self.settings = settings
        public = json.loads((settings.directory / "wallet.json").read_text(encoding="utf-8"))
        self.address = public["address"]
        self.mcp = StdioMCP(
            server
            if server is not None
            else StdioServerParameters(
                command=sys.executable,
                args=["-m", "fullish.wallet_mcp"],
                env={
                    "WALLET_ENABLED": "true",
                    "WALLET_DIR": str(settings.directory.resolve()),
                    "WALLET_ALLOWED_GROUPS": ",".join(sorted(settings.allowed_groups)),
                    "WALLET_RPC_URL": settings.rpc_url,
                },
            ),
            frozenset({"wallet_info", "wallet_transfer", "wallet_transaction"}),
        )

    async def prepare(self) -> None:
        await self.mcp.discover()

    async def close(self) -> None:
        pass  # Every stdio process is closed in its originating task.

    async def info(self, token_address: str = "") -> dict:
        return await self.mcp.call("wallet_info", {"token_address": token_address})

    async def transfer(
        self,
        event_key: str,
        to_address: str,
        amount: str,
        token_address: str = "",
        *,
        group_id: str,
    ) -> dict:
        return await self.mcp.call(
            "wallet_transfer",
            {"to_address": to_address, "amount": amount, "token_address": token_address},
            meta={"fullish/event_key": event_key, "fullish/group_id": group_id},
        )

    async def transaction(self, tx_hash: str) -> dict:
        return await self.mcp.call("wallet_transaction", {"tx_hash": tx_hash})

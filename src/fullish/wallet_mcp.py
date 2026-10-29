"""Private stdio MCP server for Fullish's BSC wallet. stdout is protocol only."""

import re
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from fullish.settings import WalletSettings
from fullish.tools.web import ToolError
from fullish.wallet import BSCWallet


def create_server(
    settings: WalletSettings, *, wallet_factory: Callable[[WalletSettings], BSCWallet] = BSCWallet
) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server):
        wallet = wallet_factory(settings)
        try:
            yield {"wallet": wallet}
        finally:
            await wallet.close()

    server = MCPServer("Fullish BSC Wallet", lifespan=lifespan, log_level="WARNING")

    async def execute(ctx: Context, method: str, **kwargs) -> dict:
        try:
            wallet = ctx.request_context.lifespan_context["wallet"]
            return {"ok": True, "data": await getattr(wallet, method)(**kwargs)}
        except ToolError as error:
            return {"ok": False, "error": str(error)}
        except sqlite3.Error:
            return {"ok": False, "error": "wallet_storage_busy_or_unavailable"}
        except Exception:
            # Never serialize exceptions from the signing process or local files.
            return {"ok": False, "error": "wallet_unavailable_check_transaction_status"}

    @server.tool()
    async def wallet_info(ctx: Context, token_address: str = "") -> dict[str, object]:
        """查看你的 Fullish BSC 钱包地址、BNB 余额和最近交易；可指定 BEP-20 合约查余额。"""
        return await execute(ctx, "info", token_address=token_address)

    @server.tool()
    async def wallet_transfer(
        ctx: Context, to_address: str, amount: str, token_address: str = ""
    ) -> dict[str, object]:
        """自主转账，无需逐笔确认。amount 是十进制字符串；必须填写 BEP-20 合约地址。BNB 仅用于 gas。

        每条消息最多一笔；重试同一请求复用同一交易。submitted 不代表到账，需查询交易状态。
        """
        meta = ctx.request_context.meta or {}
        event_key = meta.get("fullish/event_key")
        group_id = meta.get("fullish/group_id")
        if (
            not isinstance(group_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", group_id)
            or not settings.allows_group(group_id)
            or not isinstance(event_key, str)
            or not re.fullmatch(r"[0-9a-f]{64}", event_key)
        ):
            return {"ok": False, "error": "wallet_trusted_event_required"}
        return await execute(
            ctx,
            "transfer",
            event_key=event_key,
            to_address=to_address,
            amount=amount,
            token_address=token_address,
        )

    @server.tool()
    async def wallet_transaction(ctx: Context, tx_hash: str) -> dict[str, object]:
        """查询 Fullish 已签名交易。pending 等待入块；confirmed 执行成功；reverted 执行失败。"""
        return await execute(ctx, "transaction", tx_hash=tx_hash)

    return server


def main() -> None:
    settings = WalletSettings.from_env()
    if settings is None:
        raise SystemExit("Configure WALLET_ENABLED and WALLET_ALLOWED_GROUPS before starting MCP")
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()

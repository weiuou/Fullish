"""Deployment acceptance through real MCP and BSC reads; never sends funds."""

import asyncio
import json

from fullish.mcp_client import WalletMCPClient
from fullish.settings import WalletSettings


async def check() -> dict:
    settings = WalletSettings.from_env()
    if settings is None:
        raise ValueError("Wallet is not enabled")
    wallet = WalletMCPClient(settings)
    try:
        await wallet.prepare()
        info = await wallet.info()
        if info.get("address") != wallet.address or info.get("chain_id") != 56:
            raise ValueError("Wallet identity mismatch")
        return {
            "wallet": "ok",
            "chain_id": 56,
            "address": wallet.address,
            "bnb_balance": info["bnb_balance"],
            "mcp_tools": len(wallet.mcp.schemas),
        }
    finally:
        await wallet.close()


def main() -> None:
    try:
        print(json.dumps(asyncio.run(check())))
    except Exception:
        raise SystemExit(
            "Wallet acceptance failed; check configuration, files, MCP and RPC"
        ) from None


if __name__ == "__main__":
    main()

"""MCP configuration commands, reached only after developer authorization."""

import sqlite3

from fullish.management.menu import MCP_MENU
from fullish.management.schema import MCPCheck
from fullish.mcp.store import MCPStore, ServerConfig


class MCPManagement:
    def __init__(self, store: MCPStore):
        self.mcp = store

    def handle(self, db, user: str, selected: int | None, parts: list[str]) -> str:
        try:
            return self._command(db, selected, parts)
        except (ValueError, sqlite3.Error):
            return "MCP 配置未更新，请检查名称、地址、参数或服务器配置。"

    def _command(self, db, selected: int | None, parts: list[str]) -> str:
        if parts == ["列表"]:
            configs = self.mcp.configs()
            return "MCP 列表\n" + (
                "\n".join(
                    f"{c.name}：{c.transport} {'启用' if c.enabled else '停用'}，"
                    f"{len(c.groups)} 个授权群"
                    for c in configs.values()
                )
                or "尚未添加。"
            )
        if len(parts) == 3 and parts[0] == "添加":
            name, endpoint = parts[1:]
            if endpoint.startswith("env:"):
                config = ServerConfig(name=name, url_env=endpoint[4:])
            else:
                # Query credentials belong in server-side environment references.
                if "?" in endpoint:
                    return "带查询参数的地址请在服务器设置 MCP_ 专用变量，再使用 env:变量名添加。"
                config = ServerConfig(name=name, url=endpoint)
            self.mcp.put(config, create=True)
            return f"已添加 MCP {name}，尚未授权任何群。发送：MCP 检查 {name}"
        if len(parts) == 2 and parts[0] == "检查":
            return MCPCheck(parts[1])
        if len(parts) == 2 and parts[0] in {"授权", "撤销"}:
            row = db.execute("SELECT group_id FROM groups WHERE id=?", (selected,)).fetchone()
            if not row:
                return "请先发送群列表，再选择需要管理的群。"
            self.mcp.change(parts[1], "grant" if parts[0] == "授权" else "revoke", row[0])
            return f"已{parts[0]}当前群使用 MCP {parts[1]}，后续调用生效。"
        if len(parts) == 2 and parts[0] in {"启用", "停用", "删除"}:
            self.mcp.change(
                parts[1], "delete" if parts[0] == "删除" else "enabled", parts[0] == "启用"
            )
            return f"已{parts[0]} MCP {parts[1]}。"
        if len(parts) == 3 and parts[0] == "限定":
            self.mcp.change(parts[1], "tools", None if parts[2] == "*" else parts[2].split(","))
            return f"已更新 MCP {parts[1]} 的工具范围，后续调用生效。"
        return MCP_MENU

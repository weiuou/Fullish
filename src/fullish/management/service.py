"""Authenticate and dispatch commands; feature handlers own their business operations."""

import sqlite3
from collections.abc import Callable

from fullish.management.groups import GroupManagement
from fullish.management.identity import DeveloperIdentity
from fullish.management.mcp import MCPManagement
from fullish.management.menu import MENU
from fullish.management.store import ControlStore
from fullish.policies.groups import PolicyError

CommandHandler = Callable[[sqlite3.Connection, str, int | None, list[str]], str]


class ManagementService:
    def __init__(
        self,
        store: ControlStore,
        identity: DeveloperIdentity,
        groups: GroupManagement,
        mcp: MCPManagement,
    ):
        self.store = store
        self.identity = identity
        self.groups = groups
        self.handlers: dict[str, CommandHandler] = {"MCP": mcp.handle}

    def register(self, namespace: str, handler: CommandHandler) -> None:
        if not namespace or namespace in self.handlers:
            raise ValueError("Management command namespace already registered or empty")
        self.handlers[namespace] = handler

    def observe(self, group: str) -> None:
        self.store.observe(group)

    def command(self, user: str, message_id: str, text: str) -> str | None:
        if not user or len(user) > 256 or not message_id or len(message_id) > 256:
            return None
        if len(text) > (4096 if text.strip().removeprefix("/").startswith("MCP ") else 200):
            return None
        parts = text.strip().removeprefix("/").split()
        with self.store.connection() as db:
            if not self.identity.claim(db, user, message_id):
                return None
            developer = self.identity.developer(db, user)
            if len(parts) == 2 and parts[0] in {"绑定", "绑定开发者"}:
                if self.identity.bind(db, user, parts[1]):
                    return "开发者绑定成功。\n" + MENU
                return "绑定码无效或已过期。请在服务器重新生成一次性绑定码。"
            if not developer:
                return "请先使用服务器生成的一次性绑定码绑定开发者。" if parts == ["菜单"] else None
            try:
                if parts and (handler := self.handlers.get(parts[0])):
                    return handler(db, user, developer["selected"], parts[1:])
                return self.groups.handle(db, user, developer["selected"], parts)
            except PolicyError as error:
                if str(error) == "sandbox_group_limit":
                    return "最多同时为两个群开启沙箱，请先关闭另一个群的沙箱。"
                if str(error) == "sandbox_requires_group":
                    return "沙箱需要选定群后单独开启。"
                return "群配置不可用，请检查服务器配置。"

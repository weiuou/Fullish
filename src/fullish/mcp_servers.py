"""Persistent operator-managed MCP registrations, shared by the menu and runtime."""

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fullish.group_policy import GROUP_ID

SERVER_ID = re.compile(r"[a-z][a-z0-9_-]{0,19}")
ENV_NAME = re.compile(r"MCP_[A-Z0-9_]{1,100}")
MAX_SERVERS = 8


class MCPConfigError(ValueError):
    pass


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
        or any(char.isspace() for char in value)
    ):
        raise MCPConfigError("mcp_https_url_required")
    return value


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(pattern=SERVER_ID.pattern, min_length=1, max_length=20)
    transport: Literal["http", "stdio"] = "http"
    url: str = Field(default="", max_length=2048)
    url_env: str = ""
    headers_env: dict[str, str] = Field(default_factory=dict, max_length=16)
    command: str = Field(default="", max_length=1024)
    args: list[str] = Field(default_factory=list, max_length=32)
    env: dict[str, str] = Field(default_factory=dict, max_length=32)
    enabled: bool = True
    groups: list[str] = Field(default_factory=list, max_length=2000)
    tools: list[str] | None = Field(default=None, max_length=64)
    timeout: int = Field(default=20, ge=1, le=30)

    @model_validator(mode="after")
    def check(self):
        if not SERVER_ID.fullmatch(self.name) or any(
            not GROUP_ID.fullmatch(g) for g in self.groups
        ):
            raise MCPConfigError("mcp_invalid_configuration")
        references = [*self.headers_env.values(), *self.env.values()]
        if self.url_env:
            references.append(self.url_env)
        if any(not ENV_NAME.fullmatch(ref) for ref in references):
            raise MCPConfigError("mcp_dedicated_environment_required")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", h) for h in self.headers_env):
            raise MCPConfigError("mcp_invalid_header")
        if self.transport == "http":
            if bool(self.url) == bool(self.url_env) or self.command or self.args or self.env:
                raise MCPConfigError("mcp_invalid_configuration")
            if self.url:
                validate_url(self.url)
        elif not Path(self.command).is_absolute() or self.url or self.url_env or self.headers_env:
            raise MCPConfigError("mcp_absolute_executable_required")
        if any(len(arg) > 2048 for arg in self.args):
            raise MCPConfigError("mcp_invalid_configuration")
        if self.tools is not None and any(not t or len(t) > 128 for t in self.tools):
            raise MCPConfigError("mcp_invalid_configuration")
        return self

    def environment(self, references: dict[str, str]) -> dict[str, str]:
        values = {key: os.environ.get(ref, "") for key, ref in references.items()}
        if any(not value or "\r" in value or "\n" in value for value in values.values()):
            raise MCPConfigError("mcp_environment_missing_or_invalid")
        return values

    def endpoint(self) -> str:
        return validate_url(
            self.environment({"url": self.url_env})["url"] if self.url_env else self.url
        )


class MCPStore:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.path = directory / "mcp.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(self.path, timeout=3) as db:
            db.execute("CREATE TABLE IF NOT EXISTS servers(name TEXT PRIMARY KEY, config TEXT)")

    def configs(self) -> dict[str, ServerConfig]:
        try:
            with sqlite3.connect(self.path, timeout=3) as db:
                rows = db.execute("SELECT config FROM servers ORDER BY name LIMIT 9").fetchall()
            if len(rows) > MAX_SERVERS:
                raise MCPConfigError("mcp_server_limit")
            return {
                config.name: config
                for row in rows
                for config in [ServerConfig.model_validate_json(row[0])]
            }
        except (ValueError, sqlite3.Error):
            raise MCPConfigError("mcp_configuration_unavailable") from None

    def put(self, config: ServerConfig, *, create: bool = False) -> None:
        with sqlite3.connect(self.path, timeout=3) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM servers WHERE name=?", (config.name,)).fetchone()
            if create and exists:
                raise MCPConfigError("mcp_server_exists")
            if (
                not exists
                and db.execute("SELECT count(*) FROM servers").fetchone()[0] >= MAX_SERVERS
            ):
                raise MCPConfigError("mcp_server_limit")
            db.execute(
                "INSERT OR REPLACE INTO servers VALUES(?,?)",
                (config.name, config.model_dump_json()),
            )

    def change(self, name: str, operation: str, value=None) -> None:
        with sqlite3.connect(self.path, timeout=3) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config FROM servers WHERE name=?", (name,)).fetchone()
            if not row:
                raise MCPConfigError("mcp_server_not_found")
            if operation == "delete":
                db.execute("DELETE FROM servers WHERE name=?", (name,))
                return
            config = ServerConfig.model_validate_json(row[0]).model_dump()
            if operation in {"grant", "revoke"}:
                groups = set(config["groups"])
                groups.add(value) if operation == "grant" else groups.discard(value)
                config["groups"] = sorted(groups)
            elif operation in {"enabled", "tools"}:
                config[operation] = value
            else:
                raise MCPConfigError("mcp_invalid_operation")
            validated = ServerConfig.model_validate(config)
            db.execute(
                "UPDATE servers SET config=? WHERE name=?", (validated.model_dump_json(), name)
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["import", "list", "check", "delete"])
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    directory = os.environ.get("GROUP_CONTROL_DIR")
    if not directory:
        raise SystemExit("GROUP_CONTROL_DIR is not configured")
    store = MCPStore(Path(directory))
    try:
        if args.operation == "import":
            if not args.value:
                parser.error("import requires a JSON file")
            raw = Path(args.value).read_bytes()
            if len(raw) > 65536:
                raise MCPConfigError("mcp_configuration_too_large")
            config = ServerConfig.model_validate_json(raw)
            store.put(config)
            print(f"Saved MCP server: {config.name}")
        elif args.operation == "delete":
            store.change(args.value, "delete")
        elif args.operation == "check":
            import asyncio

            from fullish.mcp_runtime import MCPRegistry

            async def check():
                registry = MCPRegistry(store)
                try:
                    print(await registry.inspect(args.value or ""))
                finally:
                    await registry.close()

            asyncio.run(check())
        else:
            print(
                json.dumps(
                    [
                        {"name": c.name, "enabled": c.enabled, "groups": len(c.groups)}
                        for c in store.configs().values()
                    ]
                )
            )
    except (ValueError, OSError, sqlite3.Error):
        raise SystemExit(
            "MCP operation failed; check the name, configuration and server environment"
        ) from None


if __name__ == "__main__":
    main()

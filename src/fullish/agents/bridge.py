"""Private run-scoped plugin bridge; only Fullish can bind identity and select a target."""

import asyncio
import base64
import hmac
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from fullish.agents.rpc import DimError
from fullish.media.assets import ImageAssets
from fullish.policies.access import AccessPolicy
from fullish.sending import Image, OutgoingMessage, Reply, Text
from fullish.tools.runtime import ToolContext, ToolRuntime
from fullish.tools.web import ToolError

PLUGIN_TOOLS = ("visualize_guide", "visualize_render", "fullish_reply")


@dataclass
class RunLease:
    run_id: str
    session_id: str
    context: ToolContext
    deadline: float
    images: ImageAssets = field(default_factory=ImageAssets)
    active: bool = True
    renders: int = 0
    calls: int = 0
    selected: bool = False
    candidate: Reply | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentBridge:
    def __init__(self, access: AccessPolicy, tools: ToolRuntime | None, *, enabled: bool = False):
        self.access = access
        self.tools = tools
        self.enabled = enabled
        self.leases: dict[str, RunLease] = {}

    def bind(self, run_id: str, session_id: str, context: ToolContext, deadline: float) -> RunLease:
        if session_id in self.leases:
            raise DimError("dim_session_already_bound")
        lease = RunLease(run_id, session_id, context, deadline)
        self.leases[session_id] = lease
        return lease

    def revoke(self, session_id: str) -> None:
        if lease := self.leases.pop(session_id, None):
            lease.active = False

    def check(self, lease: RunLease, *, render: bool = False) -> None:
        if not lease.active or time.monotonic() >= lease.deadline:
            raise DimError("dim_lease_expired")
        if not self.access.group_allowed(lease.context.group_id):
            raise DimError("dim_group_revoked")
        if render and (
            not self.enabled
            or not self.tools
            or not self.tools.sandbox
            or not self.tools.base_allowed(lease.context)
            or not self.access.feature_allowed(lease.context.group_id, "sandbox")
            or not self.access.image_allowed(lease.context)
        ):
            raise DimError("dim_visualization_not_allowed")

    def tool_names(self, context: ToolContext) -> list[str]:
        lease = RunLease("", "", context, time.monotonic() + 1)
        try:
            self.check(lease, render=True)
            return list(PLUGIN_TOOLS)
        except DimError:
            return ["fullish_reply"]

    async def invoke(self, session_id: str, name: str, arguments: dict) -> dict:
        lease = self.leases.get(session_id)
        if lease is None:
            raise DimError("dim_run_not_bound")
        async with lease.lock:
            self.check(lease)
            lease.calls += 1
            if lease.calls > 12 or lease.selected:
                raise DimError("dim_tool_budget_or_finalized")
            if name == "fullish_reply":
                if set(arguments) - {"text", "artifact_id"}:
                    raise DimError("dim_invalid_candidate")
                text, artifact_id = arguments.get("text", ""), arguments.get("artifact_id", "")
                if (
                    not isinstance(text, str)
                    or len(text) > 1500
                    or not isinstance(artifact_id, str)
                ):
                    raise DimError("dim_invalid_candidate")
                if artifact_id:
                    self.check(lease, render=True)
                    asset = lease.images.get(artifact_id)
                    lease.candidate = OutgoingMessage(
                        (Text(text), Image(asset.body)) if text else (Image(asset.body),)
                    )
                else:
                    lease.candidate = text.strip() or None
                lease.selected = True
                return {"content": [{"type": "text", "text": "候选已登记，尚未投递。结束本轮。"}]}
            if name != "visualize_render":
                raise DimError("dim_tool_unavailable")
            self.check(lease, render=True)
            if lease.renders >= 2:
                raise DimError("dim_render_budget")
            if set(arguments) - {"format", "source", "width", "height"}:
                raise DimError("dim_invalid_render")
            source, format_name = arguments.get("source"), arguments.get("format")
            width, height = arguments.get("width", 1200), arguments.get("height", 800)
            if (
                not isinstance(source, str)
                or not 0 < len(source.encode()) <= 48000
                or not isinstance(format_name, str)
                or format_name not in {"svg", "mermaid", "html"}
                or type(width) is not int
                or type(height) is not int
                or not 240 <= width <= 2400
                or not 160 <= height <= 2400
                or width * height > 4_000_000
            ):
                raise DimError("dim_invalid_render")
            lease.renders += 1
            assert self.tools and self.tools.sandbox
            self.tools.charge(lease.context)
            stem = f"visualize_{lease.run_id}_{lease.renders}"
            input_path, output_path = stem + ".source", stem + ".png"
            await self.tools.file_request(
                "/files/write",
                lease.context,
                path=input_path,
                data=base64.b64encode(source.encode()).decode(),
            )
            self.check(lease, render=True)
            # Only validated enums, dimensions and host-created IDs enter the command.
            command = (
                f"/usr/local/bin/fullish-render {format_name} /workspace/{input_path} "
                f"/workspace/{output_path} {width} {height}"
            )
            response = await self.tools.sandbox.post(
                "/exec",
                json={
                    "group_id": lease.context.group_id,
                    "sender_id": lease.context.sender_id,
                    "message_id": lease.context.message_id,
                    "command": command,
                    "timeout_seconds": 30,
                },
            )
            response.raise_for_status()
            result = response.json()
            self.check(lease, render=True)
            if result.get("exit_code") != 0 or result.get("timed_out") or "error" in result:
                raise DimError("dim_render_failed")
            downloaded = json.loads(
                await self.tools.invoke(
                    "sandbox_download",
                    json.dumps({"path": output_path}),
                    lease.context,
                    images=lease.images,
                )
            )
            self.check(lease, render=True)
            if not downloaded.get("ok"):
                raise DimError("dim_artifact_download_failed")
            asset = lease.images.get(downloaded["data"]["image_id"])
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "artifact_id": asset.image_id,
                                "render": lease.renders,
                                "instruction": "查看排版；需要时修改一次，再登记最终候选。",
                            },
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "mediaType": asset.media_type,
                            "data": base64.b64encode(asset.body).decode(),
                        },
                    },
                ]
            }


def bridge_app(bridge: AgentBridge, token: str) -> FastAPI:
    """Mount only on the private plugin transport; never on Fullish's public routes."""
    if len(token) < 32:
        raise ValueError("A private bridge token (32+ characters) is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/invoke")
    async def invoke(request: Request) -> dict:
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 200_000:
                raise HTTPException(413)
        try:
            data = json.loads(body)
            if (
                not isinstance(data, dict)
                or set(data) != {"session_id", "name", "arguments"}
                or not isinstance(data["session_id"], str)
                or not isinstance(data["name"], str)
                or not isinstance(data["arguments"], dict)
            ):
                raise ValueError()
            return await bridge.invoke(data["session_id"], data["name"], data["arguments"])
        except (ValueError, DimError) as error:
            code = str(error) if isinstance(error, DimError) else "dim_invalid_request"
            return {"isError": True, "content": [{"type": "text", "text": code}]}
        except (httpx.HTTPError, TimeoutError, ToolError):
            return {"isError": True, "content": [{"type": "text", "text": "dim_tool_failed"}]}

    return app


class BridgeServer:
    def __init__(self, bridge: AgentBridge, socket: Path, token_file: Path):
        self.bridge = bridge
        self.socket = socket
        info = token_file.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 4096
            or (os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))
        ):
            raise ValueError("Dim bridge token must be a private regular file")
        self.app = bridge_app(bridge, token_file.read_text(encoding="utf-8").strip())
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task | None = None
        self.owns_socket = False

    async def start(self) -> None:
        # No public bind. The shared directory is private to bot/runtime service identities.
        self.socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.socket.exists():
            info = self.socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise DimError("dim_bridge_socket_exists")
            try:
                _, writer = await asyncio.open_unix_connection(str(self.socket))
            except ConnectionRefusedError:
                self.socket.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise DimError("dim_bridge_socket_in_use")
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                uds=str(self.socket),
                access_log=False,
                log_level="warning",
                lifespan="off",
                timeout_graceful_shutdown=5,
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        try:
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise DimError("dim_bridge_start_failed")
                    await asyncio.sleep(0.05)
            self.socket.chmod(0o600)
            self.owns_socket = True
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for session_id in list(self.bridge.leases):
            self.bridge.revoke(session_id)
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                async with asyncio.timeout(8):
                    await asyncio.shield(self.task)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.owns_socket:
            self.socket.unlink(missing_ok=True)
            self.owns_socket = False

"""Private session authorization and PNG candidates; rendering belongs to the Dim plugin."""

import asyncio
import base64
import hmac
import json
import os
import stat
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from fullish.agents.local import unified
from fullish.agents.rpc import DimError
from fullish.media.assets import MAX_ASSET_BYTES, ImageAssets
from fullish.policies.access import AccessPolicy
from fullish.sending import Image, OutgoingMessage, Reply, Text
from fullish.telemetry import elapsed_ms, emit
from fullish.tools.runtime import ToolContext

PLUGIN_TOOLS = ("visualize_guide", "visualize_render", "fullish_reply")
RENDER_FAILURES = frozenset(
    "dim_render_" + code
    for code in (
        "canvas_content_clipped",
        "external_resource_rejected",
        "chinese_font_missing",
        "renderer_dependency_failed",
        "image_too_large",
        "source_too_large",
        "cancelled",
        "timeout",
        "canvas_limit",
        "failed",
        "cleanup_failed",
    )
)


@dataclass
class RenderTicket:
    ticket: str
    format: str
    width: int
    height: int
    started: float = field(default_factory=time.monotonic)


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
    pending: RenderTicket | None = None
    probes: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgentBridge:
    def __init__(self, access: AccessPolicy, *, enabled: bool = False, inspection: bool = False):
        self.access = access
        self.enabled = enabled
        self.inspection = inspection
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
        if render and (not self.enabled or not self.access.image_allowed(lease.context)):
            raise DimError("dim_visualization_not_allowed")

    def tool_names(self, context: ToolContext) -> list[str]:
        lease = RunLease("", "", context, time.monotonic() + 1)
        result: list[str]
        try:
            self.check(lease, render=True)
            result = list(PLUGIN_TOOLS)
        except DimError:
            result = ["fullish_reply"]
        if self.inspection and self.access.group_allowed(context.group_id):
            result = ["fullish_inspect", "fullish_self_check", *result]
        return result

    @staticmethod
    def rendered(lease: RunLease, ticket: RenderTicket, code: str = "") -> None:
        emit(
            "agent.render.result",
            run_id=lease.run_id,
            render=lease.renders,
            execution="dim-plugin",
            format=ticket.format,
            width=ticket.width,
            height=ticket.height,
            outcome="failed" if code else "completed",
            error_code=code,
            elapsed_ms=elapsed_ms(ticket.started),
        )

    async def invoke(self, session_id: str, name: str, arguments: dict) -> dict:
        lease = self.leases.get(session_id)
        if lease is None:
            raise DimError("dim_run_not_bound")
        async with lease.lock:
            self.check(lease)
            if lease.selected:
                raise DimError("dim_tool_budget_or_finalized")
            if name in {
                "fullish_reply",
                "visualize_begin",
                "fullish_inspect",
                "fullish_self_check",
            }:
                lease.calls += 1
                if lease.calls > 12:
                    raise DimError("dim_tool_budget_or_finalized")
            if name in {"fullish_inspect", "fullish_self_check"}:
                if not self.inspection:
                    raise DimError("dim_inspection_not_allowed")
                if name == "fullish_self_check":
                    if (
                        set(arguments) - {"render"}
                        or type(arguments.get("render", False)) is not bool
                    ):
                        raise DimError("dim_invalid_inspection")
                    if arguments.get("render"):
                        self.check(lease, render=True)
                        if lease.probes:
                            raise DimError("dim_probe_budget")
                        lease.probes += 1
                return {
                    "protocol": 3,
                    "inspection": {
                        "revision": os.environ.get("APP_REVISION", "dev"),
                        "deployment": "unified" if unified() else "companion",
                        "fullish_uid": os.geteuid() if os.name != "nt" else None,
                        "model": os.environ.get("AI_MODEL", ""),
                        "permissions": {
                            "chat": True,
                            "image": self.access.image_allowed(lease.context),
                        },
                    },
                }
            if name == "fullish_reply":
                if lease.pending:
                    raise DimError("dim_render_in_progress")
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
            self.check(lease, render=True)
            if name == "visualize_begin":
                if lease.pending:
                    raise DimError("dim_render_in_progress")
                if lease.renders >= 2:
                    raise DimError("dim_render_budget")
                if set(arguments) != {"format", "source_bytes", "width", "height"}:
                    raise DimError("dim_invalid_render")
                format_name, size = arguments["format"], arguments["source_bytes"]
                width, height = arguments["width"], arguments["height"]
                if (
                    not isinstance(format_name, str)
                    or format_name not in {"svg", "mermaid", "html"}
                    or type(size) is not int
                    or not 0 < size <= 48000
                    or type(width) is not int
                    or type(height) is not int
                    or not 240 <= width <= 2400
                    or not 160 <= height <= 2400
                    or width * height > 4_000_000
                ):
                    raise DimError("dim_invalid_render")
                lease.renders += 1
                ticket = RenderTicket(uuid.uuid4().hex, format_name, width, height)
                lease.pending = ticket
                return {"protocol": 2, "ticket": ticket.ticket, "render": lease.renders}
            if name not in {"visualize_check", "visualize_complete", "visualize_failed"}:
                raise DimError("dim_tool_unavailable")
            ticket = lease.pending
            if ticket is None or arguments.get("ticket") != ticket.ticket:
                raise DimError("dim_render_ticket_invalid")
            if name == "visualize_check":
                if set(arguments) != {"ticket"}:
                    raise DimError("dim_invalid_render")
                return {"protocol": 2}
            if name == "visualize_failed":
                if set(arguments) != {"ticket", "error"}:
                    raise DimError("dim_invalid_render")
                raw = arguments["error"]
                code = (
                    raw if isinstance(raw, str) and raw in RENDER_FAILURES else "dim_render_failed"
                )
                lease.pending = None
                self.rendered(lease, ticket, code)
                return {"isError": True, "content": [{"type": "text", "text": code}]}
            if set(arguments) != {"ticket", "png"}:
                raise DimError("dim_invalid_render")
            # Consume a matching completion once, including malformed PNG submissions.
            lease.pending = None
            encoded = arguments["png"]
            if (
                not isinstance(encoded, str)
                or not 0 < len(encoded) <= (MAX_ASSET_BYTES + 2) // 3 * 4
            ):
                raise DimError("dim_invalid_png")
            try:
                body = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise DimError("dim_invalid_png") from None
            if (
                len(body) < 24
                or len(body) > MAX_ASSET_BYTES
                or body[:8] != b"\x89PNG\r\n\x1a\n"
                or struct.unpack(">II", body[16:24]) != (ticket.width, ticket.height)
            ):
                raise DimError("dim_invalid_png")
            asset = lease.images.add(body)
            lease.pending = None
            self.rendered(lease, ticket)
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
                    }
                ]
            }


def bridge_app(bridge: AgentBridge, token: str) -> FastAPI:
    """Authorization/candidate transport only; no execution or filesystem commands."""
    if len(token) < 32:
        raise ValueError("A private bridge token (32+ characters) is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def receive(request: Request, *, complete: bool) -> dict:
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(403)
        limit = 14 * 1024 * 1024 if complete else 200_000
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise HTTPException(413)
        try:
            data = json.loads(body)
            if (
                not isinstance(data, dict)
                or set(data) != {"session_id", "name", "arguments"}
                or not isinstance(data["session_id"], str)
                or not isinstance(data["name"], str)
                or not isinstance(data["arguments"], dict)
                or (data["name"] == "visualize_complete") != complete
            ):
                raise ValueError()
            return await bridge.invoke(data["session_id"], data["name"], data["arguments"])
        except (ValueError, DimError) as error:
            code = str(error) if isinstance(error, DimError) else "dim_invalid_request"
            return {"isError": True, "content": [{"type": "text", "text": code}]}

    @app.post("/invoke")
    async def invoke(request: Request) -> dict:
        return await receive(request, complete=False)

    @app.post("/render-complete")
    async def complete(request: Request) -> dict:
        return await receive(request, complete=True)

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
            self.socket.chmod(0o660 if unified() else 0o600)
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

"""Pinned Dim native RPC over protected local transports (never a public WebSocket)."""

import asyncio
import contextlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

DIM_VERSION = "0.1.0-alpha.20"
DIM_REVISION = "d3910d14999119c37d61d5d52139dfa29b81eab3"
PROTOCOL_VERSION = 3
MAX_FRAME = 16 * 1024 * 1024


class DimError(RuntimeError):
    """A safe code; upstream messages can contain user input and credentials."""


class DimRPC:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: Any,
        *,
        process: asyncio.subprocess.Process | None = None,
    ):
        self.reader = reader
        self.writer = writer
        self.process = process
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.sequence = 0
        self.closed = False
        self.write_lock = asyncio.Lock()
        self.read_task = asyncio.create_task(self._read())
        self.features: frozenset[str] = frozenset()

    @classmethod
    async def connect(cls, endpoint: str, *, executable: Path | None = None) -> "DimRPC":
        process = None
        if os.name == "nt":
            if not endpoint.startswith("\\\\.\\pipe\\") or executable is None:
                raise DimError("dim_private_pipe_required")
            # Use Dim's own bridge: it authenticates the pipe owner and ACL before RPC.
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "app-server",
                "--connect-pipe",
                endpoint,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_FRAME + 1,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            try:
                assert process.stderr and process.stdout and process.stdin
                async with asyncio.timeout(5):
                    ready = json.loads(await process.stderr.readline())
                if ready != {"status": "ready"}:
                    raise DimError("dim_pipe_authentication_failed")
                reader, writer = process.stdout, process.stdin
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
        else:
            path = Path(endpoint)
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise DimError("dim_private_socket_required")
            if info.st_mode & 0o077:
                raise DimError("dim_private_socket_required")
            reader, writer = await asyncio.open_unix_connection(endpoint, limit=MAX_FRAME + 1)
        rpc = cls(reader, writer, process=process)
        try:
            hello = await rpc.call(
                "hello", {"protocolVersion": PROTOCOL_VERSION, "clientKind": "other"}, timeout=5
            )
            if hello.get("protocolVersion") != PROTOCOL_VERSION:
                raise DimError("dim_protocol_mismatch")
            if hello.get("serverVersion") != DIM_VERSION:
                raise DimError("dim_version_mismatch")
            rpc.features = frozenset(hello.get("features", []))
            return rpc
        except BaseException:
            await rpc.close()
            raise

    async def _read(self) -> None:
        try:
            while line := await self.reader.readline():
                if len(line) > MAX_FRAME:
                    raise DimError("dim_frame_too_large")
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise DimError("dim_invalid_frame")
                # Product notifications are not final replies. Read results via session history
                # or the scoped final-candidate bridge, never concatenate progress text.
                if "id" not in message:
                    continue
                future = self.pending.get(message["id"])
                if future is None or future.done():
                    continue
                if "error" in message:
                    raw = message["error"].get("data", {}).get("appError", "rpc_failed")
                    code = (
                        raw
                        if isinstance(raw, str) and re.fullmatch(r"[a-z_]{1,64}", raw)
                        else "rpc_failed"
                    )
                    future.set_exception(DimError("dim_" + code))
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    raise DimError("dim_invalid_frame")
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, DimError):
            pass
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(DimError("dim_connection_lost"))

    async def call(self, method: str, params: dict, *, timeout: float = 10) -> Any:
        if self.closed:
            raise DimError("dim_connection_closed")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            frame = (
                json.dumps(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            if len(frame) > MAX_FRAME:
                raise DimError("dim_frame_too_large")
            async with asyncio.timeout(timeout):
                async with self.write_lock:
                    self.writer.write(frame)
                    await self.writer.drain()
                return await future
        except TimeoutError:
            raise DimError("dim_rpc_timeout") from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def close(self) -> None:
        self.closed = True
        self.writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            async with asyncio.timeout(3):
                await self.writer.wait_closed()
        self.read_task.cancel()
        await asyncio.gather(self.read_task, return_exceptions=True)
        if self.process:
            try:
                async with asyncio.timeout(3):
                    await self.process.wait()
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

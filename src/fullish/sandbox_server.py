"""Private Unix-socket broker for persistent rootless gVisor containers (stdlib only).

Install as a separate Unix user. Neither the bot nor model receives the Docker socket.
All Docker options are fixed here; input can only select an explicitly enabled group.
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import selectors
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

WORKER = "/opt/fullish/worker.py"
WORKSPACE_OPTIONS = "rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=700"
TEMP_OPTIONS = "rw,noexec,nosuid,nodev,size=32m,uid=10001,gid=10001,mode=700"


class SandboxError(RuntimeError):
    pass


class Sandboxes:
    def __init__(self):
        self.image = os.environ.get("SANDBOX_IMAGE", "")
        self.token = os.environ.get("SANDBOX_TOKEN", "")
        self.groups = frozenset(
            x.strip() for x in os.environ.get("TOOLS_ALLOWED_GROUPS", "").split(",") if x.strip()
        )
        self.docker_host = os.environ.get("SANDBOX_DOCKER_HOST", "")
        if (
            not re.fullmatch(r"(?:[a-z0-9./:_-]+@)?sha256:[a-f0-9]{64}", self.image)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token)
            or not 1 <= len(self.groups) <= 2
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", g) for g in self.groups)
            or not self.docker_host.startswith("unix:///run/user/")
        ):
            raise ValueError("invalid_broker_configuration")
        self.lock = threading.Lock()
        self.image_env: list[str] | None = None

    def docker(self, arguments: list[str], *, data: str | None = None, timeout: int = 10) -> str:
        # Never inherit credentials, DOCKER_CONTEXT or arbitrary CLI plugin configuration.
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ["HOME"],
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
        }
        process = None
        try:
            process = subprocess.Popen(
                ["/usr/bin/docker", "--host", self.docker_host, *arguments],
                stdin=subprocess.PIPE if data else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            assert process.stdout is not None and process.stderr is not None
            pending = memoryview(data.encode() if data else b"")
            output = bytearray()
            total = 0
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                if process.stdin:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise SandboxError("sandbox_runtime_timeout")
                    for key, _ in selector.select(min(left, 0.1)):
                        if key.data == "stdin":
                            written = os.write(key.fd, pending[:4096])
                            pending = pending[written:]
                            if not pending:
                                selector.unregister(key.fd)
                                assert process.stdin is not None
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, 8192)
                            if not chunk:
                                selector.unregister(key.fd)
                            else:
                                total += len(chunk)
                                if total > 65536:
                                    raise SandboxError("sandbox_output_too_large")
                                if key.data == "stdout":
                                    output.extend(chunk)
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode:
                raise SandboxError("sandbox_runtime_failed")
            return output.decode("utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxError("sandbox_runtime_unavailable") from None
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream:
                        stream.close()

    def name(self, group: str) -> str:
        digest = hmac.new(self.token.encode(), group.encode(), hashlib.sha256).hexdigest()[:24]
        return "fullish-sandbox-" + digest

    def check_runtime(self) -> None:
        info = json.loads(self.docker(["info", "--format", "{{json .}}"]))
        if (
            info.get("CgroupVersion") != "2"
            or info.get("CgroupDriver") != "systemd"
            or not any(
                str(item).startswith("name=rootless") for item in info.get("SecurityOptions", [])
            )
            or "runsc" not in info.get("Runtimes", {})
        ):
            raise SandboxError("rootless_runsc_with_cgroups_required")
        runtime = info["Runtimes"]["runsc"]
        if runtime.get("path") != "/opt/fullish-sandbox/sandbox-runsc.py" or runtime.get(
            "runtimeArgs"
        ) != ["--platform=systrap"]:
            raise SandboxError("verified_systemd_runtime_adapter_required")
        image = json.loads(self.docker(["image", "inspect", self.image]))[0]
        self.image_env = image["Config"].get("Env") or []

    def valid(self, container: dict) -> bool:
        host = container["HostConfig"]
        config = container["Config"]
        tmpfs = host.get("Tmpfs", {})
        return bool(
            container["State"].get("Running")
            and config.get("Image") == self.image
            and config.get("User") == "10001:10001"
            and host.get("Runtime") == "runsc"
            and host.get("NetworkMode") == "none"
            and host.get("ReadonlyRootfs")
            and not host.get("Privileged")
            and host.get("Memory") == 512 * 1024 * 1024
            and host.get("MemorySwap") == 512 * 1024 * 1024
            and host.get("NanoCpus") == 1_000_000_000
            and host.get("PidsLimit") == 64
            and host.get("CapDrop") == ["ALL"]
            and not host.get("CapAdd")
            and "no-new-privileges" in ":".join(host.get("SecurityOpt") or [])
            and not host.get("Binds")
            and not host.get("Mounts")
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and host.get("PidMode", "") in {"", "private"}
            and host.get("IpcMode") == "private"
            and host.get("UTSMode", "") in {"", "private"}
            and not host.get("VolumesFrom")
            and not host.get("PortBindings")
            and set(tmpfs) == {"/workspace", "/tmp"}
            and tmpfs["/workspace"] == WORKSPACE_OPTIONS
            and tmpfs["/tmp"] == TEMP_OPTIONS
            and config.get("Entrypoint") == ["python3", "-I", WORKER]
            and config.get("Cmd") == ["--idle"]
            and self.image_env is not None
            and (config.get("Env") or []) == self.image_env
        )

    def effective_limits(self, container: dict) -> bool:
        """Docker configuration alone does not prove rootless cgroup delegation works."""
        try:
            pid = int(container["State"]["Pid"])
            line = next(
                row
                for row in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if row.startswith("0::")
            )
            root = Path("/sys/fs/cgroup").resolve()
            directory = (root / line[3:].lstrip("/")).resolve()
            if not directory.is_relative_to(root):
                return False
            quota, period = (directory / "cpu.max").read_text().split()
            return (
                int(quota) == int(period)
                and int(period) > 0
                and int((directory / "memory.max").read_text()) == 512 * 1024 * 1024
                and int((directory / "memory.swap.max").read_text()) == 0
                and int((directory / "pids.max").read_text()) == 64
            )
        except (OSError, ValueError, KeyError, StopIteration):
            return False

    def ensure(self, group: str) -> str:
        name = self.name(group)
        try:
            rows = json.loads(self.docker(["container", "inspect", name]))
        except SandboxError:
            rows = []
        if rows and self.valid(rows[0]) and self.effective_limits(rows[0]):
            return name
        if rows:
            self.docker(["rm", "--force", name])
        self.docker(
            [
                "run",
                "--detach",
                "--name",
                name,
                "--pull=never",
                "--runtime=runsc",
                "--network=none",
                "--read-only",
                "--user=10001:10001",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=1",
                "--pids-limit=64",
                "--restart=no",
                "--log-driver=none",
                "--tmpfs=/workspace:" + WORKSPACE_OPTIONS,
                "--tmpfs=/tmp:" + TEMP_OPTIONS,
                self.image,
            ]
        )
        rows = json.loads(self.docker(["container", "inspect", name]))
        if not self.valid(rows[0]) or not self.effective_limits(rows[0]):
            self.docker(["rm", "--force", name])
            raise SandboxError("sandbox_constraints_not_enforced")
        return name

    def execute(self, data: dict) -> dict:
        if (
            not isinstance(data, dict)
            or set(data) != {"group_id", "sender_id", "message_id", "command", "timeout_seconds"}
            or data["group_id"] not in self.groups
            or not all(
                isinstance(data[key], str) and 1 <= len(data[key]) <= 256
                for key in ("sender_id", "message_id")
            )
            or not isinstance(data["command"], str)
            or not 1 <= len(data["command"]) <= 8192
            or type(data["timeout_seconds"]) is not int
            or not 1 <= data["timeout_seconds"] <= 30
        ):
            raise SandboxError("invalid_or_unauthorized_request")
        if not self.lock.acquire(blocking=False):
            raise SandboxError("sandbox_busy")
        name = ""
        broken = True
        try:
            self.check_runtime()
            name = self.ensure(data["group_id"])
            # Clear leftovers after broker restart as well as after every command.
            self.docker(["exec", name, "python3", "-I", WORKER, "--cleanup"])
            raw = self.docker(
                ["exec", "-i", name, "python3", "-I", WORKER],
                data=json.dumps(
                    {
                        "command": data["command"],
                        "timeout_seconds": data["timeout_seconds"],
                    }
                ),
                timeout=data["timeout_seconds"] + 5,
            )
            if len(raw) > 65536:
                raise SandboxError("sandbox_output_too_large")
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or set(result) != {"output", "exit_code", "timed_out", "truncated"}
                or not isinstance(result["output"], str)
                or len(result["output"]) > 12000
                or type(result["exit_code"]) is not int
                or type(result["timed_out"]) is not bool
                or type(result["truncated"]) is not bool
            ):
                raise SandboxError("invalid_sandbox_result")
            broken = False
            return result
        except (ValueError, KeyError, TypeError):
            raise SandboxError("invalid_runtime_response") from None
        finally:
            try:
                if name:
                    try:
                        if broken:
                            self.docker(["rm", "--force", name])
                        else:
                            self.docker(["exec", name, "python3", "-I", WORKER, "--cleanup"])
                    except SandboxError:
                        self.docker(["rm", "--force", name])
            finally:
                self.lock.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server: "Server"
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args) -> None:
        pass  # Do not log request bodies, commands, output or tokens.

    def respond(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        expected = "Bearer " + self.server.sandboxes.token
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self.respond(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                self.path != "/exec"
                or not 1 <= length <= 16384
                or self.headers.get("Transfer-Encoding")
            ):
                raise SandboxError("invalid_request")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            result = self.server.sandboxes.execute(data)
            self.respond(200, result)
        except SandboxError as error:
            self.respond(429 if str(error) == "sandbox_busy" else 503, {"error": str(error)})
        except (ValueError, OSError, TypeError):
            self.respond(400, {"error": "invalid_request"})


# Allow policy tests to import on Windows; the actual service is Linux-only.
UnixServerBase: Any = getattr(socketserver, "UnixStreamServer", socketserver.TCPServer)


class Server(socketserver.ThreadingMixIn, UnixServerBase):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: str, sandboxes: Sandboxes):
        self.sandboxes = sandboxes
        UnixServerBase.__init__(self, path, Handler)


def main() -> None:
    if os.name != "posix":
        raise RuntimeError("linux_required")
    sandboxes = Sandboxes()
    sandboxes.check_runtime()
    for group in sandboxes.groups:
        name = sandboxes.ensure(group)  # Reuse across commands and bot deploys.
        sandboxes.docker(["exec", name, "python3", "-I", WORKER, "--cleanup"])
    path = Path(os.environ.get("SANDBOX_SOCKET", "/run/fullish-sandbox/api.sock"))
    if path.exists():
        if not path.is_socket():
            raise ValueError("socket_path_not_socket")
        path.unlink()
    with Server(str(path), sandboxes) as server:
        path.chmod(0o660)
        print("Sandbox broker ready.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()

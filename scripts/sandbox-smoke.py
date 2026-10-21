"""Synthetic local sandbox checks; no QQ messages, chat history or model requests.

Default: verify the actual rootless/gVisor broker. --worker-image tests only the
worker in a temporary Docker container (CI); this does not attest gVisor isolation.
"""

import argparse
import http.client
import json
import shlex
import socket
import subprocess
import sys
import uuid
from pathlib import Path


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("sandbox", timeout=45)
        self.path = path

    def connect(self) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise RuntimeError("Linux Unix sockets are required")
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-image")
    args = parser.parse_args()
    name = "fullish-worker-smoke-" + uuid.uuid4().hex[:12]
    prefix = "probe_" + uuid.uuid4().hex[:12]
    settings = {}
    if not args.worker_image:
        # No token in CLI arguments or output. Run as a trusted administrator.
        settings = dict(
            line.split("=", 1)
            for line in Path("/etc/fullish-sandbox.env").read_text().splitlines()
            if "=" in line
        )

    def invoke(command: str, timeout: int = 5) -> dict:
        if args.worker_image:
            result = subprocess.run(
                ["docker", "exec", "-i", name, "python3", "-I", "/opt/fullish/worker.py"],
                input=json.dumps({"command": command, "timeout_seconds": timeout}),
                text=True,
                capture_output=True,
                timeout=timeout + 10,
                check=True,
            )
            return json.loads(result.stdout)
        client = UnixHTTPConnection(settings["SANDBOX_SOCKET"])
        try:
            client.request(
                "POST",
                "/exec",
                body=json.dumps(
                    {
                        "group_id": settings["TOOLS_ALLOWED_GROUPS"].split(",")[0],
                        "sender_id": "synthetic-smoke",
                        "message_id": prefix,
                        "command": command,
                        "timeout_seconds": timeout,
                    }
                ),
                headers={
                    "Authorization": "Bearer " + settings["SANDBOX_TOKEN"],
                    "Content-Type": "application/json",
                },
            )
            response = client.getresponse()
            if response.status != 200:
                raise RuntimeError("broker_request_failed")
            return json.loads(response.read(65537))
        finally:
            client.close()

    def python(source: str) -> str:
        return "python3 -c " + shlex.quote(source)

    def check(label: str, condition: bool) -> None:
        if not condition:
            raise RuntimeError("Sandbox check failed: " + label)
        print("PASS " + label, flush=True)

    try:
        if args.worker_image:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--network=none",
                    "--read-only",
                    "--user=10001:10001",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--memory=512m",
                    "--cpus=1",
                    "--pids-limit=64",
                    "--tmpfs=/workspace:rw,noexec,nosuid,size=128m,uid=10001,gid=10001",
                    "--tmpfs=/tmp:rw,noexec,nosuid,size=32m,uid=10001,gid=10001",
                    args.worker_image,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            print("Worker-only smoke; actual gVisor broker requires separate acceptance.")
        data = invoke(python("import math; print(math.factorial(6))"))
        check("calculation", data["exit_code"] == 0 and data["output"].strip() == "720")
        data = invoke(
            python(f"from pathlib import Path; Path('/workspace/{prefix}').write_text('kept')")
        )
        check("write group workspace", data["exit_code"] == 0)
        data = invoke(
            python(f"from pathlib import Path; print(Path('/workspace/{prefix}').read_text())")
        )
        check("reuse across tasks", data["output"].strip() == "kept")
        data = invoke(
            python(
                "import os; prefixes=('QQ_', 'AI_', 'DEEPSEEK_', 'SANDBOX_TOKEN'); "
                "print(any(k.startswith(prefixes) for k in os.environ))"
            )
        )
        check("no production credentials", data["output"].strip() == "False")
        data = invoke(
            python("from pathlib import Path; Path('/opt/fullish/forbidden').write_text('x')")
        )
        check("read-only image", data["exit_code"] != 0)
        data = invoke(
            python(
                "import socket\ntry:\n"
                " socket.create_connection(('1.1.1.1',443),timeout=1)\n"
                " print('connected')\nexcept OSError:\n print('blocked')"
            )
        )
        check("outbound blocked", data["output"].strip() == "blocked")
        data = invoke("sleep 5", timeout=1)
        check("command timeout", data["timed_out"])
        invoke("sleep 30 >/dev/null 2>&1 & echo started")
        data = invoke(
            python(
                "from pathlib import Path; paths=Path('/proc').glob('[0-9]*/comm'); "
                "print(any(b'sleep' in p.read_bytes() for p in paths))"
            )
        )
        check("background children cleaned", data["output"].strip() == "False")
        data = invoke(python("print('x'*30000)"))
        check("bounded output", data["truncated"] and len(data["output"]) <= 12000)
        invoke("rm -f /workspace/" + prefix)
    finally:
        if args.worker_image:
            subprocess.run(
                ["docker", "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not echo command output, credentials, URLs or upstream response bodies.
        print("Sandbox smoke failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None

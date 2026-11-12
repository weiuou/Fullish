"""Synthetic renderer acceptance through the real private sandbox broker; no model or QQ."""

import base64
import http.client
import json
import socket
import struct
import sys
import uuid
from pathlib import Path


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("sandbox", timeout=45)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def main() -> None:
    settings = dict(
        line.split("=", 1)
        for line in Path("/etc/fullish-sandbox.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    group = settings["TOOLS_ALLOWED_GROUPS"].split(",")[0]
    prefix = "visualize_probe_" + uuid.uuid4().hex

    def call(route: str, **payload) -> dict:
        client = UnixHTTPConnection(settings["SANDBOX_SOCKET"])
        try:
            client.request(
                "POST",
                route,
                body=json.dumps(
                    {
                        "group_id": group,
                        "sender_id": "synthetic-check",
                        "message_id": prefix,
                        **payload,
                    }
                ),
                headers={
                    "Authorization": "Bearer " + settings["SANDBOX_TOKEN"],
                    "Content-Type": "application/json",
                },
            )
            response = client.getresponse()
            if response.status != 200:
                error = json.loads(response.read(4096)).get("error", "unknown")
                if not isinstance(error, str) or not error.replace("_", "").isalpha():
                    error = "unknown"
                raise RuntimeError(f"broker_{route.rsplit('/', 1)[1]}_{response.status}_{error}")
            return json.loads(response.read(14 * 1024 * 1024 + 1))
        finally:
            client.close()

    cases = [
        (
            "svg",
            '<svg xmlns="http://www.w3.org/2000/svg" width="700" height="400">'
            '<text x="20" y="90" font-size="32">群聊 → 智能体 → 图片</text></svg>',
            True,
        ),
        ("mermaid", 'flowchart LR\n A["群消息"] --> B["Dim"] --> C["绘图"] --> D["确认"]', True),
        ("html", '<h1>离线绘图</h1><p style="font-size:28px">中文布局验收</p>', True),
        ("html", '<img src="https://example.invalid/not-allowed.png">', False),
        (
            "html",
            "<style>div{background:url(file:///etc/passwd)}</style><div>拒绝读取</div>",
            False,
        ),
        ("html", '<div style="height:2000px">画布溢出</div>', False),
    ]
    paths = []
    try:
        for index, (format_name, source, succeeds) in enumerate(cases):
            input_path, output_path = f"{prefix}_{index}.source", f"{prefix}_{index}.png"
            paths.extend([input_path, output_path])
            written = call(
                "/files/write", path=input_path, data=base64.b64encode(source.encode()).decode()
            )
            if written.get("size") != len(source.encode()):
                raise RuntimeError("probe_source_write_failed")
            try:
                result = call(
                    "/exec",
                    command=f"/usr/local/bin/fullish-render {format_name} /workspace/{input_path} "
                    f"/workspace/{output_path} 1000 650",
                    timeout_seconds=30,
                )
            except RuntimeError:
                diagnostic = call(
                    "/exec",
                    command="chromium --headless --disable-gpu --dump-dom about:blank",
                    timeout_seconds=10,
                )
                print("Synthetic Chromium diagnostic: " + diagnostic.get("output", ""), flush=True)
                raise
            if result.get("timed_out") or "error" in result:
                raise RuntimeError("probe_render_did_not_settle")
            if succeeds:
                if result.get("exit_code") != 0:
                    diagnostic = call(
                        "/exec",
                        command="chromium --headless --disable-gpu --dump-dom about:blank",
                        timeout_seconds=10,
                    )
                    # This fixed synthetic command receives no source, token or user paths.
                    print(
                        "Synthetic Chromium diagnostic: " + diagnostic.get("output", ""), flush=True
                    )
                    raise RuntimeError(f"{format_name}_render_failed")
                downloaded = call("/files/read", path=output_path)
                png = base64.b64decode(downloaded["data"], validate=True)
                if (
                    png[:8] != b"\x89PNG\r\n\x1a\n"
                    or struct.unpack(">II", png[16:24]) != (1000, 650)
                    or not 1000 < len(png) <= 10 * 1024 * 1024
                    or downloaded["size"] != len(png)
                ):
                    raise RuntimeError("probe_png_invalid")
            elif result.get("exit_code") == 0 or "data" in call("/files/read", path=output_path):
                raise RuntimeError("unsafe_source_was_accepted")
            # The worker cleans child processes after each command. Verify an actual browser
            # left no live helpers before the next render, including after rejected input.
            clean = call(
                "/exec",
                command='python3 -c "from pathlib import Path; '
                "print(any(b'chrom' in p.read_bytes() "
                "for p in Path('/proc').glob('[0-9]*/comm')))\"",
                timeout_seconds=3,
            )
            if clean.get("exit_code") != 0 or clean.get("output", "").strip() != "False":
                raise RuntimeError("browser_children_remained")
            print(
                f"PASS {format_name} case {index}: {'PNG' if succeeds else 'rejected'}, cleaned",
                flush=True,
            )
    finally:
        if paths:
            removed = call("/exec", command="rm -f -- " + " ".join(paths), timeout_seconds=3)
            if removed.get("exit_code") != 0:
                raise RuntimeError("probe_cleanup_failed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fixed codes only: upstream browser and broker errors may include source or secrets.
        code = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("Visualization acceptance failed: " + code, file=sys.stderr)
        raise SystemExit(1) from None

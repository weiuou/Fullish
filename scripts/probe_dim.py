"""Exercise the real pinned Dim binary with a local, credential-free model fixture."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fullish.agents.rpc import DIM_REVISION, DIM_VERSION, DimRPC
from fullish.agents.session import final_text

FIXTURE_TEXT = "本地 Dim 协议验收成功。"


class ModelFixture(BaseHTTPRequestHandler):
    requests: list[dict] = []
    respond: Callable[[dict], dict] = staticmethod(
        lambda _: {"role": "assistant", "content": FIXTURE_TEXT}
    )

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(request)
        message = ModelFixture.respond(request)
        finish_reason = "tool_calls" if message.get("tool_calls") else "stop"
        delta = dict(message)
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": index, **call} for index, call in enumerate(delta["tool_calls"])
            ]
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk_delta, reason in [(delta, None), ({}, finish_reason)]:
                chunk = {
                    "id": "local-probe",
                    "object": "chat.completion.chunk",
                    "model": "probe",
                    "choices": [{"index": 0, "delta": chunk_delta, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            body = json.dumps(
                {
                    "id": "local-probe",
                    "object": "chat.completion",
                    "model": "probe",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


async def probe(
    executable: Path,
    *,
    plugin: Path | None = None,
    extra_env: dict[str, str] | None = None,
    exercise: Callable[[str, Path], Awaitable[dict]] | None = None,
) -> dict:
    version = subprocess.check_output([str(executable), "--version"], text=True).strip()
    if version != "dimcode " + DIM_VERSION:
        raise RuntimeError("The probe requires dimcode " + DIM_VERSION)
    ModelFixture.requests = []
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="fullish-dim-probe-") as directory:
            root = Path(directory)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            config = root / "model.json"
            config.write_text(
                json.dumps(
                    {
                        "defaultModel": {"providerId": "fixture", "modelId": "probe"},
                        "providers": [
                            {
                                "id": "fixture",
                                "kind": "openai-chat",
                                "baseUrl": f"http://127.0.0.1:{fixture.server_port}/v1",
                                "apiKeyEnv": "FULLISH_PROBE_KEY",
                                "models": [
                                    {
                                        "id": "probe",
                                        "contextWindow": 32000,
                                        "maxOutputTokens": 4096,
                                        "config": {"maxTokens": 1024},
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            endpoint = (
                "\\\\.\\pipe\\fullish-probe-" + uuid.uuid4().hex
                if os.name == "nt"
                else str(root / "rpc.sock")
            )
            arguments = [
                str(executable),
                "app-server",
                "--home",
                str(home),
                "--model-config",
                str(config),
                "--no-ws",
                "--threads",
                "2",
                "--pipe" if os.name == "nt" else "--unix",
                endpoint,
            ]
            if plugin:
                defaults = root / "plugins.json"
                defaults.write_text(
                    json.dumps(
                        {
                            "plugins": {"fullish-visualize": str(plugin.resolve())},
                            "locked": ["Rpc", "Transport"],
                        }
                    ),
                    encoding="utf-8",
                )
                runtime = plugin / "node_modules" / "@arcships" / "rutis-runtime"
                arguments += [
                    "--defaults",
                    str(defaults),
                    "--node",
                    shutil.which("node") or "node",
                    "--js-runtime",
                    str(runtime.resolve()),
                ]
            # No production or personal provider/QQ/wallet environment reaches this process.
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC"}
            }
            environment["FULLISH_PROBE_KEY"] = "synthetic-local-fixture"
            environment["NO_PROXY"] = "127.0.0.1,localhost"
            environment.update(extra_env or {})
            with (root / "daemon.log").open("wb") as log:
                daemon = await asyncio.create_subprocess_exec(
                    *arguments,
                    env=environment,
                    stdout=log,
                    stderr=log,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
                rpc = None
                try:
                    async with asyncio.timeout(20):
                        while rpc is None:
                            if daemon.returncode is not None:
                                raise RuntimeError(
                                    "Dim daemon failed: " + (root / "daemon.log").read_text()
                                )
                            try:
                                rpc = await DimRPC.connect(endpoint, executable=executable)
                            except (OSError, RuntimeError):
                                await asyncio.sleep(0.1)
                    created = await rpc.call(
                        "session.create",
                        {
                            "cwd": str(workspace),
                            "tools": [],
                            "interactive": False,
                            "systemPrompt": "Reply briefly. No tools are allowed.",
                            "permissionPolicy": {"preset": "read-only", "allowedTools": []},
                        },
                    )
                    sid = created["sessionId"]
                    await rpc.call("session.attach", {"sessionId": sid})
                    submitted = await rpc.call(
                        "session.submit",
                        {
                            "sessionId": sid,
                            "branchId": "main",
                            "submissionId": uuid.uuid4().hex,
                            "text": "Synthetic protocol acceptance input.",
                        },
                    )
                    await rpc.call(
                        "session.idle", {"sessionId": sid, "branchId": "main"}, timeout=20
                    )
                    history = await rpc.call(
                        "session.history", {"sessionId": sid, "branchId": "main", "limit": 100}
                    )
                    serialized = json.dumps(history, ensure_ascii=False)
                    if FIXTURE_TEXT not in serialized or len(ModelFixture.requests) != 1:
                        raise RuntimeError(
                            f"Model turn failed ({len(ModelFixture.requests)} requests): "
                            + serialized
                        )
                    if ModelFixture.requests[0].get("tools"):
                        raise RuntimeError("Unexpected native tools reached the model")
                    if final_text(history["events"]) != FIXTURE_TEXT:
                        raise RuntimeError("Final-output extraction failed")
                    stopped = await rpc.call("session.stop", {"sessionId": sid, "branchId": "main"})
                    await rpc.call("session.close", {"sessionId": sid})
                    deleted = await rpc.call(
                        "session.delete", {"sessionId": sid, "requireInactive": True}
                    )
                    result = {
                        "version": DIM_VERSION,
                        "source_revision": DIM_REVISION,
                        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                        "transport": "authenticated-pipe" if os.name == "nt" else "private-unix",
                        "text_turn": True,
                        "empty_tool_allowlist": True,
                        "deleted": deleted,
                        "stop_reply": stopped,
                        "submission": submitted,
                        "history": history,
                        "model_requests": len(ModelFixture.requests),
                    }
                    if exercise:
                        result["exercise"] = await exercise(endpoint, workspace)
                    if plugin:
                        catalog = await rpc.call("plugin.list", {})
                        result["plugin_status"] = [
                            {"name": item["name"], "status": item["status"]}
                            for item in catalog
                            if item["name"] == "fullish-visualize"
                        ]
                    await rpc.call("daemon.shutdown", {}, timeout=10)
                    return result
                finally:
                    if rpc:
                        await rpc.close()
                    if daemon.returncode is None:
                        try:
                            async with asyncio.timeout(5):
                                await daemon.wait()
                        except TimeoutError:
                            daemon.kill()
                            await daemon.wait()
    finally:
        fixture.shutdown()
        fixture.server_close()
        worker.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(probe(args.executable.resolve()))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # The report contains only synthetic data; never use this fixture for real chat history.
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

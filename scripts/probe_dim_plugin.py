"""Exercise the installed visualize plugin in real Dim using only a local model fixture."""

import argparse
import asyncio
import base64
import json
import os
import re
import secrets
import shlex
import socket
import tempfile
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from probe_dim import FIXTURE_TEXT, ModelFixture, probe

from fullish.agents.backend import DimReplyModel
from fullish.agents.bridge import AgentBridge, BridgeServer, bridge_app
from fullish.agents.settings import AgentSettings
from fullish.agents.store import RunStore
from fullish.collection import CollectedMessage, MessagePart, ParsedContent, Sender
from fullish.conversation.store import ConversationStore
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies
from fullish.reply.handler import ChatHandler
from fullish.reply.media import ImageReader
from fullish.reply.service import ConversationService
from fullish.scheduling.generation import GenerationCoordinator
from fullish.sending import Image, OutgoingMessage
from fullish.sending.service import SendingService
from fullish.settings import ToolSettings
from fullish.tools.runtime import ToolContext, ToolRuntime

PLUGIN_REPLY = "插件登记的最终回复。"


async def exercise_plugin(
    executable: Path,
    plugin: Path,
    browser: Path | None = None,
    preview: Path | None = None,
    sandbox_group: str | None = None,
) -> dict:
    group = sandbox_group or "fixture-group"
    rendering = bool(browser or sandbox_group)
    access = AccessPolicy(policies=GroupPolicies.from_env() if sandbox_group else None)
    bridge = AgentBridge(access, None)
    token = secrets.token_urlsafe(32)
    private_server = None
    private_directory = None
    listener = None
    server = None
    serving = None
    if sandbox_group:
        private_directory = tempfile.TemporaryDirectory(prefix="fullish-bridge-probe-")
        private_root = Path(private_directory.name)
        token_file = private_root / "token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        private_server = BridgeServer(bridge, private_root / "bridge.sock", token_file)
        await private_server.start()
        bridge_env = {
            "FULLISH_AGENT_BRIDGE_SOCKET": str(private_root / "bridge.sock"),
            "FULLISH_AGENT_BRIDGE_TOKEN": token,
        }
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(
                bridge_app(bridge, token),
                access_log=False,
                log_level="warning",
                lifespan="off",
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Local bridge failed to start")
            await asyncio.sleep(0.05)
        bridge_env = {
            "FULLISH_AGENT_BRIDGE_URL": f"http://127.0.0.1:{port}/",
            "FULLISH_AGENT_BRIDGE_TOKEN": token,
        }

    async def run(endpoint: str, workspace: Path) -> dict:
        rendered: list[bytes] = []

        async def sandbox(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload["group_id"] != group:
                return httpx.Response(403)
            if request.url.path in {"/files/write", "/files/read"}:
                name = payload["path"]
                if not re.fullmatch(r"visualize_[a-f0-9]{32}_[12]\.(source|png)", name):
                    return httpx.Response(400)
                path = workspace / name
                if request.url.path == "/files/write":
                    body = base64.b64decode(payload["data"], validate=True)
                    with path.open("xb") as stream:
                        stream.write(body)
                    return httpx.Response(200, json={"path": name, "size": len(body)})
                body = path.read_bytes()
                return httpx.Response(
                    200,
                    json={
                        "path": name,
                        "size": len(body),
                        "data": base64.b64encode(body).decode(),
                    },
                )
            command = shlex.split(payload["command"])
            if len(command) != 6 or command[0] != "/usr/local/bin/fullish-render":
                return httpx.Response(400)
            renderer = plugin / "renderer" / "render.mjs"
            options = {
                "root": str(workspace),
                "format": command[1],
                "input": str(workspace / Path(command[2]).name),
                "output": str(workspace / Path(command[3]).name),
                "width": int(command[4]),
                "height": int(command[5]),
                "browser": str(browser),
            }
            # This is a local external-broker fixture, not a gVisor isolation test.
            program = (
                "import {render} from "
                + json.dumps(renderer.as_uri())
                + ";await render(JSON.parse(process.argv[1]));"
            )
            process = await asyncio.create_subprocess_exec(
                "node",
                "--input-type=module",
                "-e",
                program,
                json.dumps(options),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            try:
                async with asyncio.timeout(30):
                    _, error = await process.communicate()
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            if process.returncode:
                raise RuntimeError("Local browser fixture failed: " + error.decode())
            rendered.append(Path(options["output"]).read_bytes())
            return httpx.Response(200, json={"exit_code": 0, "timed_out": False, "output": ""})

        tools = None
        if browser:
            tools = ToolRuntime(
                ToolSettings(
                    frozenset({group}),
                    sandbox_socket="/fixture.sock",
                    sandbox_token="fixture" * 8,
                ),
                sandbox_transport=httpx.MockTransport(sandbox),
            )
            bridge.tools = tools
            bridge.enabled = True
        elif sandbox_group:
            tool_settings = ToolSettings.from_env()
            if not tool_settings or not tool_settings.sandbox_socket:
                raise RuntimeError("Real sandbox configuration is required")
            tools = ToolRuntime(tool_settings, policies=access.policies)
            bridge.tools = tools
            bridge.enabled = True

        if rendering and len(bridge.tool_names(ToolContext(group, "fixture-member", "one"))) != 3:
            raise RuntimeError("Test group requires explicit chat, sandbox and image grants")

        seen_pixels: list[bytes] = []

        def respond(request: dict) -> dict:
            if not request.get("tools"):
                return {"role": "assistant", "content": FIXTURE_TEXT}
            completed_calls = [
                message.get("tool_call_id", "")
                for message in request["messages"]
                if message["role"] == "tool"
            ]
            render_count = sum(call.startswith("fixture-render-") for call in completed_calls)
            if "fixture-final" in completed_calls:
                return {"role": "assistant", "content": "这个模型尾句不会替换已登记候选。"}
            arguments = {"text": PLUGIN_REPLY}
            name, call_id = "fullish_reply", "fixture-final"
            if rendering and render_count < 2:
                name, call_id = "visualize_render", f"fixture-render-{render_count + 1}"
                color = "#e0f2fe" if render_count == 0 else "#ccfbf1"
                arguments = {
                    "format": "svg",
                    "source": (
                        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="500">'
                        f'<rect width="900" height="500" fill="{color}"/>'
                        '<text x="40" y="100" font-size="32">'
                        "群聊 → 智能体 → 离线绘图 → QQ 确认</text></svg>"
                    ),
                }
            elif rendering:
                dump = json.dumps(request)
                if "data:image/png;base64," not in dump:
                    raise RuntimeError("The actual model request did not contain rendered pixels")
                artifacts = re.findall(r"img_[a-f0-9]{32}", dump)
                if not artifacts:
                    raise RuntimeError("No rendered artifact was returned to Dim")
                arguments["artifact_id"] = artifacts[-1]
                pixels = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", dump)
                seen_pixels[:] = [base64.b64decode(encoded, validate=True) for encoded in pixels]
            return {
                "role": "assistant",
                "content": "准备登记回复。",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }

        ModelFixture.respond = staticmethod(respond)
        settings = AgentSettings(
            endpoint,
            workspace,
            workspace / "runs.sqlite3",
            frozenset({group}),
            enabled=True,
            executable=executable,
        )
        reader = ImageReader()
        model = DimReplyModel(
            settings, RunStore(settings.state_path, bot_id="fixture"), reader, access, bridge
        )

        class QQFixture:
            mode: Literal["reply", "proactive"] = "reply"
            group_id = group

            def __init__(self):
                self.candidate = None
                self.acknowledged = 0

            async def allowed(self):
                return True

            def remaining(self):
                return 5

            async def prepare(self, content):
                self.candidate = content

            async def send(self, index):
                self.acknowledged += 1
                return {"id": "synthetic-qq-ack"}

        conversations = ConversationService(
            model,
            store=ConversationStore(),
            access=access,
            generation=GenerationCoordinator(min_interval=0),
        )
        handler = ChatHandler(
            conversations,
            SendingService(access, confirmed=conversations.store.sent),
            runs=model.store,
        )
        qq = QQFixture()
        try:
            await handler.handle(
                CollectedMessage(
                    group,
                    "one",
                    Sender("fixture-member"),
                    ParsedContent((MessagePart("text", "请画一张中文流程图，检查后修正配色。"),)),
                    True,
                ),
                qq,
                segment_types=["text"],
            )
            result = qq.candidate
            if rendering:
                if not isinstance(result, OutgoingMessage) or result.text != PLUGIN_REPLY:
                    raise RuntimeError("The real plugin did not return an image candidate")
                last = result.parts[-1]
                if (
                    len(seen_pixels) != 2
                    or not isinstance(last, Image)
                    or last.source != seen_pixels[-1]
                ):
                    raise RuntimeError("The final PNG did not reach Fullish intact")
                if preview:
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    preview.write_bytes(seen_pixels[-1])
            elif result != PLUGIN_REPLY:
                raise RuntimeError("The real plugin did not register the expected candidate")
            record = model.store.get(ToolContext(group, "fixture-member", "one"))
            if not record or record.delivery != "confirmed" or qq.acknowledged != 1:
                raise RuntimeError("The candidate did not reach confirmed Fullish delivery")
            if len(conversations.store.groups[group].turns) != 2:
                raise RuntimeError("Confirmed delivery did not update group history exactly once")
            requests = [request for request in ModelFixture.requests if request.get("tools")]
            if not requests or any(
                {tool["function"]["name"] for tool in request["tools"]}
                != (
                    {"visualize_render", "visualize_guide", "fullish_reply"}
                    if rendering
                    else {"fullish_reply"}
                )
                for request in requests
            ):
                raise RuntimeError("The effective native tool allowlist was not enforced")
            return {
                "native_plugin": True,
                "candidate_bridge": True,
                "tool_allowlist": True,
                "model_requests": len(requests),
                "active_leases": len(bridge.leases),
                "local_browser_renders": len(rendered),
                "model_png_inputs": len(seen_pixels),
                "sandbox_boundary": "real_broker" if sandbox_group else "simulated",
                "bridge_transport": "private_unix" if sandbox_group else "loopback_fixture",
                "gvisor_verified": False,
                "qq_boundary": "simulated",
                "confirmed_delivery_and_history": True,
            }
        finally:
            await model.close()
            await reader.close()
            if tools:
                await tools.close()

    try:
        return await probe(
            executable,
            plugin=plugin,
            extra_env=bridge_env,
            exercise=run,
        )
    finally:
        ModelFixture.respond = staticmethod(
            lambda _: {"role": "assistant", "content": FIXTURE_TEXT}
        )
        if private_server:
            await private_server.close()
        if private_directory:
            private_directory.cleanup()
        if server and serving:
            server.should_exit = True
            await serving
        if listener:
            listener.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, default=Path("plugins/visualize"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--browser", type=Path, help="Real local browser behind a simulated broker")
    parser.add_argument(
        "--sandbox-group", help="Use the configured real broker for this authorized test group"
    )
    parser.add_argument(
        "--preview", type=Path, help="Save the final synthetic PNG when using --browser"
    )
    args = parser.parse_args()
    if args.browser and args.sandbox_group:
        parser.error("Choose --browser or --sandbox-group")
    result = asyncio.run(
        exercise_plugin(
            args.executable.resolve(),
            args.plugin.resolve(),
            args.browser,
            args.preview,
            args.sandbox_group,
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "history"}, indent=2))


if __name__ == "__main__":
    main()

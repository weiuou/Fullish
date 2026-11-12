"""Real Dim/rutis direct rendering with synthetic model and QQ boundaries; no broker."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Literal

import uvicorn
from probe_dim import FIXTURE_TEXT, ModelFixture, probe

from fullish.agents.backend import DimReplyModel
from fullish.agents.bridge import AgentBridge, BridgeServer, bridge_app
from fullish.agents.check import check as deployment_check
from fullish.agents.rpc import DimRPC
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
from fullish.tools.runtime import ToolContext

GROUP = "direct-fixture-group-0001"
PLUGIN_REPLY = "插件直接绘图验收成功。"


def model_reply(request: dict) -> dict:
    if not request.get("tools"):
        return {"role": "assistant", "content": FIXTURE_TEXT}
    completed = [
        item.get("tool_call_id", "") for item in request["messages"] if item["role"] == "tool"
    ]
    if "fixture-final" in completed:
        return {"role": "assistant", "content": "模型尾句不替换登记候选。"}
    count = sum(item.startswith("fixture-render-") for item in completed)
    name, call_id, arguments = "fullish_reply", "fixture-final", {"text": PLUGIN_REPLY}
    if count < 2:
        name, call_id = "visualize_render", f"fixture-render-{count + 1}"
        color = "#e0f2fe" if count == 0 else "#ccfbf1"
        arguments = {
            "format": "svg",
            "width": 1000,
            "height": 650,
            "source": (
                '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" '
                'viewBox="0 0 1000 650">'
                f'<rect width="1000" height="650" fill="{color}"/>'
                '<text x="40" y="100" font-size="32">Dim → 插件直接绘图 → QQ 确认</text></svg>'
            ),
        }
    else:
        material = json.dumps(request)
        if "data:image/png;base64," not in material:
            raise RuntimeError("Actual native model input has no PNG")
        arguments["artifact_id"] = re.findall(r"img_[a-f0-9]{32}", material)[-1]
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


async def client_flow(
    endpoint: str,
    workspace: Path,
    state: Path,
    bridge: AgentBridge,
    access: AccessPolicy,
    executable: Path | None,
    private: bool,
) -> dict:
    settings = AgentSettings(
        endpoint,
        workspace,
        state / "runs.sqlite3",
        frozenset({GROUP}),
        enabled=True,
        executable=executable,
        inspection=os.environ.get("DIM_INSPECTION") == "true",
    )
    rpc = await DimRPC.connect(endpoint, executable=executable)
    try:
        direct = await rpc.call("visualize.probe", {}, timeout=150)
        if direct.get("protocol") != 2 or direct.get("execution") != "dim-plugin":
            raise RuntimeError("Native plugin did not own the renderer")
    finally:
        await rpc.close()
    reader = ImageReader()
    runs = RunStore(settings.state_path, bot_id="fixture")
    text_model = DimReplyModel(settings, runs, reader, access)
    model = DimReplyModel(settings, runs, reader, access, bridge)
    history = ConversationStore()
    conversations = ConversationService(
        model, store=history, access=access, generation=GenerationCoordinator(min_interval=0)
    )

    class QQFixture:
        mode: Literal["reply", "proactive"] = "reply"
        group_id = GROUP
        candidate = None
        acknowledged = 0

        async def allowed(self):
            return True

        def remaining(self):
            return 5

        async def prepare(self, content):
            self.candidate = content

        async def send(self, index):
            self.acknowledged += 1
            return {"id": "synthetic-confirmation"}

    qq = QQFixture()
    try:
        text = await text_model.reply(
            [{"role": "user", "content": "Synthetic text fixture."}],
            addressed=True,
            context=ToolContext(GROUP, "member", "text-only"),
        )
        if text != FIXTURE_TEXT:
            raise RuntimeError("Text-only native backend failed")
        if private:
            await deployment_check(preflight=True)
        handler = ChatHandler(
            conversations, SendingService(access, confirmed=history.sent), runs=runs
        )
        await handler.handle(
            CollectedMessage(
                GROUP,
                "image-one",
                Sender("member"),
                ParsedContent((MessagePart("text", "请绘制中文流程图，检查图片并修改一次配色。"),)),
                True,
            ),
            qq,
            segment_types=["text"],
        )
        candidate = qq.candidate
        if not isinstance(candidate, OutgoingMessage) or candidate.text != PLUGIN_REPLY:
            raise RuntimeError("Native renderer did not register the image candidate")
        image = candidate.parts[-1]
        if not isinstance(image, Image) or not isinstance(image.source, bytes):
            raise RuntimeError("Final PNG bytes did not reach Fullish")
        run = runs.get(ToolContext(GROUP, "member", "image-one"))
        if not run or run.delivery != "confirmed" or qq.acknowledged != 1:
            raise RuntimeError("QQ fixture did not confirm exactly once")
        if len(history.groups[GROUP].turns) != 2:
            raise RuntimeError("Confirmed reply history is incorrect")
        if private:
            await deployment_check()
        return {
            "native_plugin": True,
            "execution": "dim-plugin",
            "text_only_backend": True,
            "sandbox_calls": 0,
            "qq_boundary": "simulated",
            "confirmed_delivery_and_history": True,
            "final_png_sha256": hashlib.sha256(image.source).hexdigest(),
            "final_png_bytes": len(image.source),
            "deployment_pre_and_postflight": private,
        }
    finally:
        await text_model.close()
        await model.close()
        await reader.close()


async def run(args) -> dict:
    with tempfile.TemporaryDirectory(prefix="fullish-direct-probe-") as temporary:
        state = Path(temporary)
        policy = GroupPolicies(state / "policy.json")
        policy.write(
            {
                "version": 1,
                "defaults": {"chat": False, "image": False, "search": False, "sandbox": False},
                "groups": {GROUP: {"chat": True, "image": True}},
            }
        )
        access = AccessPolicy(policies=policy)
        bridge = AgentBridge(
            access, enabled=True, inspection=os.environ.get("DIM_INSPECTION") == "true"
        )
        server = None
        serving = None
        private_server = None
        if args.endpoint:
            if os.name == "nt":
                raise RuntimeError("External runtime mode requires Linux Unix sockets")
            private_server = BridgeServer(
                bridge,
                Path(os.environ["DIM_BRIDGE_SOCKET"]),
                Path(os.environ["DIM_BRIDGE_TOKEN_FILE"]),
            )
            await private_server.start()
            os.environ.update(
                {
                    "GROUP_POLICY_PATH": str(policy.path),
                    "DIM_VISUALIZATION": "true",
                    "DIM_ALLOWED_GROUPS": GROUP,
                    "AGENT_BACKEND": "dim",
                }
            )
            try:
                return await client_flow(
                    args.endpoint,
                    Path(os.environ["DIM_WORKSPACE_ROOT"]),
                    state,
                    bridge,
                    access,
                    None,
                    True,
                )
            finally:
                await private_server.close()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        token = secrets.token_urlsafe(32)
        server = uvicorn.Server(
            uvicorn.Config(
                bridge_app(bridge, token), access_log=False, log_level="warning", lifespan="off"
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if serving.done():
                await serving
            await asyncio.sleep(0.02)
        ModelFixture.respond = staticmethod(model_reply)

        async def exercise(endpoint, workspace):
            result = await client_flow(
                endpoint, workspace, state, bridge, access, args.executable, False
            )
            material = json.dumps(ModelFixture.requests)
            pixels = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", material)
            unique = {hashlib.sha256(base64.b64decode(value)).hexdigest() for value in pixels}
            if len(unique) != 2 or result["final_png_sha256"] not in unique:
                raise RuntimeError("The native model did not receive both actual PNGs")
            return {**result, "model_png_inputs": len(unique)}

        environment = {
            "FULLISH_AGENT_BRIDGE_URL": f"http://127.0.0.1:{listener.getsockname()[1]}/",
            "FULLISH_AGENT_BRIDGE_TOKEN": token,
        }
        if args.browser:
            environment["FULLISH_CHROMIUM"] = str(args.browser)
        try:
            return await probe(
                args.executable, plugin=args.plugin, extra_env=environment, exercise=exercise
            )
        finally:
            server.should_exit = True
            await serving
            ModelFixture.respond = staticmethod(
                lambda _: {"role": "assistant", "content": FIXTURE_TEXT}
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--plugin", type=Path, default=Path("plugins/visualize").resolve())
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if bool(args.executable) == bool(args.endpoint):
        parser.error("Choose --executable or --endpoint")
    result = asyncio.run(run(args))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result.get("exercise", result), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

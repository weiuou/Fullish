"""Read-only Dim handshake and synthetic offline rendering; no model calls or QQ messages."""

import argparse
import asyncio
import json
import secrets
import sys
import time

import httpx

from fullish.agents.bridge import AgentBridge
from fullish.agents.rpc import DimRPC
from fullish.agents.settings import AgentSettings
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies
from fullish.settings import ToolSettings
from fullish.tools.runtime import ToolContext, ToolRuntime

RENDER_CASES = (
    (
        "svg",
        '<svg xmlns="http://www.w3.org/2000/svg" width="550" height="250">'
        '<text x="24" y="90" font-size="32">离线绘图验收</text></svg>',
    ),
    ("mermaid", 'flowchart LR\n A["群消息"] --> B["离线绘图"] --> C["确认"]'),
    ("html", "<h1>离线绘图验收</h1><p>中文字体与有界布局</p>"),
)


async def check(*, preflight: bool = False) -> None:
    settings = AgentSettings.from_env()
    if not settings or not settings.enabled:
        print("Dim backend disabled; no agent dependency required.")
        return
    rpc = await DimRPC.connect(settings.endpoint, executable=settings.executable)
    try:
        if settings.visualization:
            catalog = await rpc.call("plugin.list", {})
            # Inspect status only; never expose registration configuration in output.
            if not isinstance(catalog, list) or not any(
                item.get("name") == "fullish-visualize"
                and item.get("status") == "running"
                and item.get("off") is False
                for item in catalog
            ):
                raise RuntimeError("visualize_plugin_missing")
    finally:
        await rpc.close()
    if not settings.visualization:
        print("Pinned Dim private transport passed; no model was invoked.")
        return
    tool_settings = ToolSettings.from_env()
    if not tool_settings or not tool_settings.sandbox_socket:
        raise RuntimeError("visualize_sandbox_missing")
    policies = GroupPolicies.from_env()
    if policies:
        policies.read()
    access = AccessPolicy(policies=policies)
    tools = ToolRuntime(tool_settings, policies=policies)
    bridge = AgentBridge(access, tools, enabled=True)
    checked = 0
    try:
        for group in sorted(settings.groups):
            context = ToolContext(group, "deployment-check", "check-" + secrets.token_hex(12))
            if len(bridge.tool_names(context)) != 3:
                continue
            for format_name, source in RENDER_CASES:
                run_id = secrets.token_hex(16)
                session = "deployment-" + run_id
                lease = bridge.bind(run_id, session, context, time.monotonic() + 75)
                try:
                    rendered = await bridge.invoke(
                        session,
                        "visualize_render",
                        {
                            "format": format_name,
                            "width": 900,
                            "height": 500,
                            "source": source,
                        },
                    )
                    if len(rendered.get("content", [])) != 2 or not lease.images.items:
                        raise RuntimeError("visualize_render_missing")
                    checked += 1
                finally:
                    bridge.revoke(session)
                    paths = f"visualize_{run_id}_1.source visualize_{run_id}_1.png"
                    removed = json.loads(
                        await tools.invoke(
                            "sandbox_exec",
                            json.dumps({"command": "rm -f -- " + paths, "timeout_seconds": 3}),
                            context,
                        )
                    )
                    if not removed.get("ok") or removed.get("data", {}).get("exit_code") != 0:
                        raise RuntimeError("visualize_probe_cleanup_failed")
        if not preflight:
            assert settings.bridge_socket and settings.bridge_token_file
            token = settings.bridge_token_file.read_text(encoding="utf-8").strip()
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(settings.bridge_socket)),
                base_url="http://bridge",
                timeout=5,
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/invoke",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "session_id": "unbound-deployment-probe",
                        "name": "fullish_reply",
                        "arguments": {"text": ""},
                    },
                )
                response.raise_for_status()
                if response.json() != {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "dim_run_not_bound"},
                    ],
                }:
                    raise RuntimeError("visualize_bridge_acceptance_failed")
        print(f"Pinned Dim passed; offline PNG checks: {checked}; no model or QQ calls.")
    finally:
        await tools.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(check(preflight=args.preflight))
    except Exception:
        print(
            "Dim/visualize acceptance failed; inspect private runtime, plugin and sandbox.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

"""Direct Dim rendering uses one-time authorization and reply-local PNG candidates."""

import asyncio
import base64
import json
import struct
import time
import zlib

import httpx
import pytest

from fullish.agents.bridge import AgentBridge, bridge_app
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies
from fullish.sending import Image, OutgoingMessage
from fullish.tools.runtime import ToolContext

TOKEN = "private-test-bridge-token-" * 2
GROUP = "g" * 16
CONTEXT = ToolContext(GROUP, "member", "event")
RENDER = {"format": "svg", "source_bytes": 6, "width": 240, "height": 160}


def png(width=240, height=160):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * width * 3) * height))
        + chunk(b"IEND", b"")
    )


def client(bridge):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bridge_app(bridge, TOKEN)), base_url="http://bridge"
    )


async def call(http, name, arguments, session="session"):
    response = await http.post(
        "/render-complete" if name == "visualize_complete" else "/invoke",
        headers={"Authorization": "Bearer " + TOKEN},
        json={"session_id": session, "name": name, "arguments": arguments},
    )
    assert response.status_code == 200
    return response.json()


def complete(grant, body=None):
    return {"ticket": grant["ticket"], "png": base64.b64encode(body or png()).decode()}


def test_direct_renderer_authorization_needs_no_sandbox_and_keeps_images_scoped(tmp_path):
    async def replay():
        policies = GroupPolicies(tmp_path / "policy.json")
        policies.write(
            {
                "version": 1,
                "defaults": {
                    "chat": True,
                    "image": True,
                    "search": False,
                    "sandbox": False,
                },
                "groups": {},
            }
        )
        bridge = AgentBridge(AccessPolicy(policies=policies), enabled=True)
        lease = bridge.bind("a" * 32, "session", CONTEXT, time.monotonic() + 30)
        bridge.bind(
            "b" * 32, "other", ToolContext("h" * 16, "member", "other"), time.monotonic() + 30
        )
        assert not policies.features(GROUP).sandbox
        assert bridge.tool_names(CONTEXT) == [
            "visualize_guide",
            "visualize_render",
            "fullish_reply",
        ]
        async with client(bridge) as http:
            assert (await http.post("/invoke", json={})).status_code == 403
            malformed = await call(http, "visualize_begin", {**RENDER, "format": {}})
            assert malformed["isError"] and lease.renders == 0
            grant = await call(http, "visualize_begin", RENDER)
            assert grant["protocol"] == 2 and lease.renders == 1
            assert (await call(http, "fullish_reply", {"text": "too early"}))["isError"]
            rendered = await call(http, "visualize_complete", complete(grant))
            artifact_id = json.loads(rendered["content"][0]["text"])["artifact_id"]
            assert lease.candidate is None and lease.images.get(artifact_id).body == png()
            assert (
                await call(
                    http,
                    "fullish_reply",
                    {"text": "wrong group", "artifact_id": artifact_id},
                    "other",
                )
            )["isError"]
            await call(http, "fullish_reply", {"text": "画好了", "artifact_id": artifact_id})
            assert isinstance(lease.candidate, OutgoingMessage)
            assert lease.candidate.parts[-1] == Image(png())
            assert (await call(http, "visualize_begin", RENDER))["isError"]
            bridge.revoke("session")
            late = await call(http, "visualize_complete", complete(grant))
            assert late["content"][0]["text"] == "dim_run_not_bound"

    asyncio.run(replay())


def test_direct_render_failure_can_be_revised_once_without_receiving_source_or_running_tools():
    async def replay():
        bridge = AgentBridge(AccessPolicy(), enabled=True)
        lease = bridge.bind("c" * 32, "session", CONTEXT, time.monotonic() + 30)
        async with client(bridge) as http:
            first = await call(http, "visualize_begin", RENDER)
            failure = await call(
                http,
                "visualize_failed",
                {
                    "ticket": first["ticket"],
                    "error": "dim_render_canvas_content_clipped",
                },
            )
            assert failure["isError"]
            assert failure["content"][0]["text"] == "dim_render_canvas_content_clipped"
            assert not lease.images.items and lease.pending is None
            second = await call(http, "visualize_begin", RENDER)
            assert second["render"] == 2 and second["ticket"] != first["ticket"]
            assert (await call(http, "visualize_complete", complete(first)))["isError"]
            rendered = await call(http, "visualize_complete", complete(second))
            artifact_id = json.loads(rendered["content"][0]["text"])["artifact_id"]
            assert (await call(http, "visualize_begin", RENDER))["content"][0][
                "text"
            ] == "dim_render_budget"
            await call(http, "fullish_reply", {"text": "修正完成", "artifact_id": artifact_id})
            assert isinstance(lease.candidate, OutgoingMessage)
            assert lease.candidate.parts[-1] == Image(png())

    asyncio.run(replay())


def test_png_completions_validate_dimensions_and_consume_the_ticket_once():
    async def replay():
        bridge = AgentBridge(AccessPolicy(), enabled=True)
        lease = bridge.bind("d" * 32, "session", CONTEXT, time.monotonic() + 30)
        async with client(bridge) as http:
            first = await call(http, "visualize_begin", RENDER)
            wrong = await call(http, "visualize_complete", complete(first, png(241, 160)))
            assert wrong["content"][0]["text"] == "dim_invalid_png"
            assert lease.pending is None and not lease.images.items
            assert (await call(http, "visualize_complete", complete(first)))["isError"]
            second = await call(http, "visualize_begin", RENDER)
            assert not (await call(http, "visualize_complete", complete(second))).get("isError")

    asyncio.run(replay())


def test_completion_rechecks_image_permission_and_revocation(tmp_path):
    async def replay():
        policies = GroupPolicies(tmp_path / "policy.json")
        policies.write(
            {
                "version": 1,
                "defaults": {
                    "chat": True,
                    "image": True,
                    "search": False,
                    "sandbox": False,
                },
                "groups": {},
            }
        )
        bridge = AgentBridge(AccessPolicy(policies=policies), enabled=True)
        lease = bridge.bind("e" * 32, "session", CONTEXT, time.monotonic() + 30)
        async with client(bridge) as http:
            grant = await call(http, "visualize_begin", RENDER)
            assert await call(http, "visualize_check", {"ticket": grant["ticket"]}) == {
                "protocol": 2
            }
            policies.change(GROUP, "image", False)
            assert (await call(http, "visualize_check", {"ticket": grant["ticket"]}))["isError"]
            rejected = await call(http, "visualize_complete", complete(grant))
            assert rejected["content"][0]["text"] == "dim_visualization_not_allowed"
            assert not lease.images.items and lease.candidate is None
            bridge.revoke("session")
            assert (await call(http, "visualize_complete", complete(grant)))["content"][0][
                "text"
            ] == "dim_run_not_bound"

    asyncio.run(replay())


@pytest.mark.parametrize(
    "error", ["secret-token-do-not-expose", {"url": "https://private.invalid"}]
)
def test_plugin_failure_details_are_limited_to_fixed_codes(error):
    async def replay():
        bridge = AgentBridge(AccessPolicy(), enabled=True)
        bridge.bind("f" * 32, "session", CONTEXT, time.monotonic() + 30)
        async with client(bridge) as http:
            grant = await call(http, "visualize_begin", RENDER)
            result = await call(
                http, "visualize_failed", {"ticket": grant["ticket"], "error": error}
            )
            assert result == {
                "isError": True,
                "content": [{"type": "text", "text": "dim_render_failed"}],
            }

    asyncio.run(replay())

"""Authorized render/file exchange and final-candidate selection across private bridge calls."""

import asyncio
import base64
import json
import time

import httpx
import pytest

from fullish.agents.bridge import AgentBridge, bridge_app
from fullish.agents.rpc import DimError
from fullish.policies.access import AccessPolicy
from fullish.sending import Image, OutgoingMessage
from fullish.settings import ToolSettings
from fullish.tools.runtime import ToolContext, ToolRuntime

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)
CONTEXT = ToolContext("group", "member", "event")
TOKEN = "private-test-bridge-token-" * 2


def test_bridge_keeps_artifacts_scoped_and_never_sends_qq():
    async def replay():
        files = {}
        commands = []

        def broker(request):
            payload = json.loads(request.content)
            assert payload["group_id"] == "group" and payload["sender_id"] == "member"
            if request.url.path == "/files/write":
                files[payload["path"]] = base64.b64decode(payload["data"])
                return httpx.Response(
                    200, json={"path": payload["path"], "size": len(files[payload["path"]])}
                )
            if request.url.path == "/exec":
                commands.append(payload["command"])
                return httpx.Response(200, json={"exit_code": 0, "timed_out": False})
            return httpx.Response(
                200,
                json={
                    "path": payload["path"],
                    "size": len(PNG),
                    "data": base64.b64encode(PNG).decode(),
                },
            )

        tools = ToolRuntime(
            ToolSettings(frozenset({"group"}), sandbox_socket="/fixture", sandbox_token="x" * 32),
            sandbox_transport=httpx.MockTransport(broker),
        )
        bridge = AgentBridge(AccessPolicy(), tools, enabled=True)
        first = bridge.bind("a" * 32, "session-a", CONTEXT, time.monotonic() + 30)
        second = bridge.bind("b" * 32, "session-b", CONTEXT, time.monotonic() + 30)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bridge_app(bridge, TOKEN)),
                base_url="http://bridge",
            ) as client:
                assert (await client.post("/invoke", json={})).status_code == 403
                headers = {"Authorization": "Bearer " + TOKEN}
                malformed = (
                    await client.post(
                        "/invoke",
                        headers=headers,
                        json={
                            "session_id": "session-a",
                            "name": "visualize_render",
                            "arguments": {"format": {}, "source": "<svg/>"},
                        },
                    )
                ).json()
                assert malformed["isError"] and not files and not commands
                source = '<svg><text>"; touch /tmp/not-a-command</text></svg>'
                rendered = (
                    await client.post(
                        "/invoke",
                        headers=headers,
                        json={
                            "session_id": "session-a",
                            "name": "visualize_render",
                            "arguments": {"format": "svg", "source": source},
                        },
                    )
                ).json()
                assert rendered["content"][1]["source"]["data"] == base64.b64encode(PNG).decode()
                artifact_id = json.loads(rendered["content"][0]["text"])["artifact_id"]
                assert source.encode() in files.values()
                assert "touch" not in commands[0]
                assert first.candidate is None and not first.selected
                with pytest.raises(ValueError, match="not_found"):
                    await bridge.invoke(
                        second.session_id,
                        "fullish_reply",
                        {
                            "text": "wrong session",
                            "artifact_id": artifact_id,
                        },
                    )
                await bridge.invoke(
                    first.session_id,
                    "fullish_reply",
                    {
                        "text": "画好了",
                        "artifact_id": artifact_id,
                    },
                )
                assert isinstance(first.candidate, OutgoingMessage)
                last = first.candidate.parts[-1]
                assert isinstance(last, Image) and last.source == PNG
                with pytest.raises(DimError, match="finalized"):
                    await bridge.invoke(
                        first.session_id,
                        "visualize_render",
                        {
                            "format": "svg",
                            "source": "<svg/>",
                        },
                    )
                bridge.revoke(first.session_id)
                stale = (
                    await client.post(
                        "/invoke",
                        headers=headers,
                        json={
                            "session_id": first.session_id,
                            "name": "fullish_reply",
                            "arguments": {"text": "late"},
                        },
                    )
                ).json()
                assert stale["isError"] and "not_bound" in stale["content"][0]["text"]
        finally:
            await tools.close()

    asyncio.run(replay())


def test_revoke_during_render_rejects_late_pixels_and_no_further_tools():
    async def replay():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def broker(request):
            calls.append(request.url.path)
            if request.url.path == "/files/write":
                return httpx.Response(200, json={"path": "source", "size": 6})
            started.set()
            await release.wait()
            return httpx.Response(200, json={"exit_code": 0, "timed_out": False})

        tools = ToolRuntime(
            ToolSettings(frozenset({"group"}), sandbox_socket="/fixture", sandbox_token="x" * 32),
            sandbox_transport=httpx.MockTransport(broker),
        )
        bridge = AgentBridge(AccessPolicy(), tools, enabled=True)
        lease = bridge.bind("c" * 32, "session", CONTEXT, time.monotonic() + 30)
        try:
            rendering = asyncio.create_task(
                bridge.invoke(
                    "session",
                    "visualize_render",
                    {
                        "format": "svg",
                        "source": "<svg/>",
                    },
                )
            )
            await started.wait()
            bridge.revoke("session")
            release.set()
            with pytest.raises(DimError, match="lease_expired"):
                await rendering
            assert calls == ["/files/write", "/exec"]
            assert not lease.images.items and not lease.selected
            with pytest.raises(DimError, match="not_bound"):
                await bridge.invoke("session", "fullish_reply", {"text": "late"})
        finally:
            await tools.close()

    asyncio.run(replay())


def test_failed_render_preserves_safe_reason_and_can_be_revised_to_an_image():
    async def replay():
        attempts = 0

        def broker(request):
            nonlocal attempts
            payload = json.loads(request.content)
            if request.url.path == "/files/write":
                return httpx.Response(200, json={"path": payload["path"], "size": 6})
            if request.url.path == "/exec":
                attempts += 1
                return httpx.Response(
                    200,
                    json={
                        "exit_code": 1 if attempts == 1 else 0,
                        "timed_out": False,
                        "output": '{"error":"canvas_content_clipped"}\n' if attempts == 1 else "",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "path": payload["path"],
                    "size": len(PNG),
                    "data": base64.b64encode(PNG).decode(),
                },
            )

        tools = ToolRuntime(
            ToolSettings(frozenset({"group"}), sandbox_socket="/fixture", sandbox_token="x" * 32),
            sandbox_transport=httpx.MockTransport(broker),
        )
        bridge = AgentBridge(AccessPolicy(), tools, enabled=True)
        lease = bridge.bind("d" * 32, "session", CONTEXT, time.monotonic() + 30)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bridge_app(bridge, TOKEN)),
                base_url="http://bridge",
            ) as client:

                async def render(source):
                    return (
                        await client.post(
                            "/invoke",
                            headers={"Authorization": "Bearer " + TOKEN},
                            json={
                                "session_id": "session",
                                "name": "visualize_render",
                                "arguments": {"format": "svg", "source": source},
                            },
                        )
                    ).json()

                failed = await render('<svg height="2000"/>')
                assert failed["isError"]
                assert failed["content"][0]["text"] == "dim_render_canvas_content_clipped"
                assert not lease.images.items and lease.candidate is None
                revised = await render('<svg height="600"/>')
                assert revised["content"][1]["source"]["data"] == base64.b64encode(PNG).decode()
                artifact_id = json.loads(revised["content"][0]["text"])["artifact_id"]
                await bridge.invoke(
                    "session", "fullish_reply", {"text": "画好了", "artifact_id": artifact_id}
                )
                assert isinstance(lease.candidate, OutgoingMessage)
                assert lease.candidate.parts[-1] == Image(PNG)
                assert attempts == 2 and lease.renders == 2
                assert (await render("<svg/>"))["isError"]
                assert attempts == 2
        finally:
            await tools.close()

    asyncio.run(replay())


@pytest.mark.parametrize(
    "output",
    [
        "private source and https://example.invalid/token-do-not-expose",
        '{"error":"token-do-not-expose"}',
        '{"error":{"secret":"token-do-not-expose"}}',
    ],
)
def test_unrecognized_renderer_output_never_reaches_the_plugin(output):
    async def replay():
        def broker(request):
            if request.url.path == "/files/write":
                return httpx.Response(200, json={"path": "source", "size": 6})
            return httpx.Response(200, json={"exit_code": 1, "timed_out": False, "output": output})

        tools = ToolRuntime(
            ToolSettings(frozenset({"group"}), sandbox_socket="/fixture", sandbox_token="x" * 32),
            sandbox_transport=httpx.MockTransport(broker),
        )
        bridge = AgentBridge(AccessPolicy(), tools, enabled=True)
        bridge.bind("e" * 32, "session", CONTEXT, time.monotonic() + 30)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bridge_app(bridge, TOKEN)),
                base_url="http://bridge",
            ) as client:
                response = await client.post(
                    "/invoke",
                    headers={"Authorization": "Bearer " + TOKEN},
                    json={
                        "session_id": "session",
                        "name": "visualize_render",
                        "arguments": {"format": "svg", "source": "<svg/>"},
                    },
                )
                assert response.json() == {
                    "isError": True,
                    "content": [{"type": "text", "text": "dim_render_failed"}],
                }
        finally:
            await tools.close()

    asyncio.run(replay())

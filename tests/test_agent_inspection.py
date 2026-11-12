"""Self-inspection stays within a live group grant and cannot create a reply itself."""

import asyncio
import time

import pytest

from fullish.agents.bridge import AgentBridge
from fullish.agents.rpc import DimError
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies
from fullish.tools.runtime import ToolContext


def test_inspection_works_without_image_permission_and_stops_after_revocation(tmp_path):
    async def replay():
        group = "g" * 16
        policies = GroupPolicies(tmp_path / "policy.json")
        policies.write(
            {
                "version": 1,
                "defaults": {"chat": True, "image": False, "search": False, "sandbox": False},
                "groups": {},
            }
        )
        bridge = AgentBridge(AccessPolicy(policies=policies), enabled=True, inspection=True)
        context = ToolContext(group, "member", "event")
        lease = bridge.bind("a" * 32, "session", context, time.monotonic() + 30)
        assert bridge.tool_names(context) == [
            "fullish_inspect",
            "fullish_self_check",
            "fullish_reply",
        ]
        result = await bridge.invoke("session", "fullish_self_check", {})
        assert result["inspection"]["permissions"] == {"chat": True, "image": False}
        assert lease.candidate is None and not lease.selected
        with pytest.raises(DimError, match="dim_visualization_not_allowed"):
            await bridge.invoke("session", "fullish_self_check", {"render": True})
        policies.change(group, "chat", False)
        with pytest.raises(DimError, match="dim_group_revoked"):
            await bridge.invoke("session", "fullish_inspect", {"action": "list"})
        bridge.revoke("session")
        with pytest.raises(DimError, match="dim_run_not_bound"):
            await bridge.invoke("session", "fullish_self_check", {})

    asyncio.run(replay())


def test_render_probe_is_bounded_and_rechecks_authorization_after_queueing(tmp_path):
    async def replay():
        group = "g" * 16
        policies = GroupPolicies(tmp_path / "policy.json")
        policies.write(
            {
                "version": 1,
                "defaults": {"chat": True, "image": True, "search": False, "sandbox": False},
                "groups": {},
            }
        )
        bridge = AgentBridge(AccessPolicy(policies=policies), enabled=True, inspection=True)
        bridge.bind(
            "b" * 32, "session", ToolContext(group, "member", "event"), time.monotonic() + 30
        )
        await bridge.invoke("session", "fullish_self_check", {"render": True})
        assert await bridge.invoke("session", "fullish_inspection_check", {"render": True}) == {
            "protocol": 3
        }
        with pytest.raises(DimError, match="dim_probe_budget"):
            await bridge.invoke("session", "fullish_self_check", {"render": True})
        policies.change(group, "image", False)
        with pytest.raises(DimError, match="dim_visualization_not_allowed"):
            await bridge.invoke("session", "fullish_inspection_check", {"render": True})

    asyncio.run(replay())

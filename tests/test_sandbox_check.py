"""A healthy process must not hide missing or unusable sandbox dependencies."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from fullish import sandbox_check
from fullish.settings import ToolSettings


@pytest.mark.parametrize("failure", ["disabled", "denied", "timeout", "wrong_output", "success"])
def test_deployment_gate_requires_a_real_success(monkeypatch, failure):
    settings = ToolSettings(frozenset({"synthetic-group"}), sandbox_socket="/missing/socket")
    monkeypatch.setattr(
        sandbox_check.ToolSettings, "from_env", lambda: None if failure == "disabled" else settings
    )
    data = {
        "output": "fullish-sandbox-ready",
        "exit_code": 0,
        "timed_out": False,
        "truncated": False,
    }
    if failure == "timeout":
        data["timed_out"] = True
    if failure == "wrong_output":
        data["output"] = "not executed"
    tools = Mock(
        invoke=AsyncMock(return_value=json.dumps({"ok": failure != "denied", "data": data})),
        close=AsyncMock(),
    )
    factory = Mock(return_value=tools)
    monkeypatch.setattr(sandbox_check, "ToolRuntime", factory)
    if failure in {"disabled", "success"}:
        asyncio.run(sandbox_check.check())
    else:
        with pytest.raises(RuntimeError, match="sandbox_acceptance_failed"):
            asyncio.run(sandbox_check.check())
    if failure == "disabled":
        factory.assert_not_called()
    else:
        tools.close.assert_awaited_once()
        context = tools.invoke.call_args.args[2]
        assert context.group_id == "synthetic-group"
        assert context.sender_id == "deployment-check"

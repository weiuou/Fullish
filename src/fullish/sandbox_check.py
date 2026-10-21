"""Deployment gate: prove this bot image can execute through its configured socket."""

import asyncio
import json
import sys
import uuid

from fullish.control import GroupControl
from fullish.settings import ToolSettings
from fullish.tools import ToolContext, ToolRuntime


async def check() -> None:
    settings = ToolSettings.from_env()
    if not settings or not settings.sandbox_socket:
        print("Sandbox disabled; no sandbox dependency required.")
        return
    control = GroupControl.from_env()
    policies = control.policies if control else None
    tools = ToolRuntime(settings, policies=policies)
    try:
        # Synthetic identity, no model invocation, no QQ send and no workspace writes.
        groups = policies.sandbox_groups() if policies else settings.allowed_groups
        if policies:
            policies.read()  # A damaged policy must fail deployment, not skip acceptance.
        for group in sorted(groups):
            result = json.loads(
                await tools.invoke(
                    "sandbox_exec",
                    json.dumps({"command": "printf fullish-sandbox-ready", "timeout_seconds": 3}),
                    ToolContext(group, "deployment-check", "check-" + uuid.uuid4().hex),
                )
            )
            data = result.get("data", {})
            if not (
                result.get("ok") is True
                and data.get("exit_code") == 0
                and data.get("output") == "fullish-sandbox-ready"
                and data.get("timed_out") is False
                and data.get("truncated") is False
            ):
                raise RuntimeError("sandbox_acceptance_failed")
        print("Configured sandbox passed execution and host cgroup validation.")
    finally:
        await tools.close()


def main() -> None:
    try:
        asyncio.run(check())
    except Exception:
        print(
            "Sandbox acceptance failed; check broker, socket mount and runtime limits.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

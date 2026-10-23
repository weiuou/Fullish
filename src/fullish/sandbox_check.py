"""Deployment gate: prove this bot image can execute through its configured socket."""

import asyncio
import base64
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
        # Synthetic identity and probe files; no model invocation or QQ send.
        groups = policies.sandbox_groups() if policies else settings.allowed_groups
        if policies:
            policies.read()  # A damaged policy must fail deployment, not skip acceptance.
        for group in sorted(groups):
            context = ToolContext(group, "deployment-check", "check-" + uuid.uuid4().hex)
            result = json.loads(
                await tools.invoke(
                    "sandbox_exec",
                    json.dumps({"command": "printf fullish-sandbox-ready", "timeout_seconds": 3}),
                    context,
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
            # Prove the installed broker AND pinned worker support binary exchange.
            # Use only synthetic bytes and remove the probe before accepting deployment.
            path = ".fullish-check-" + uuid.uuid4().hex + ".png"
            body = b"\x89PNG\r\n\x1a\nfullish-file-check"
            encoded = base64.b64encode(body).decode("ascii")
            try:
                uploaded = await tools.file_request(
                    "/files/write", context, path=path, data=encoded
                )
                downloaded = await tools.file_request("/files/read", context, path=path)
                if uploaded != {"path": path, "size": len(body)} or downloaded != {
                    "path": path,
                    "size": len(body),
                    "data": encoded,
                }:
                    raise RuntimeError("sandbox_acceptance_failed")
            finally:
                removed = json.loads(
                    await tools.invoke(
                        "sandbox_exec",
                        json.dumps({"command": "rm -f -- " + path, "timeout_seconds": 3}),
                        context,
                    )
                )
                if not removed.get("ok") or removed.get("data", {}).get("exit_code") != 0:
                    raise RuntimeError("sandbox_acceptance_failed")
        print("Configured sandbox passed execution, binary exchange and host cgroup validation.")
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

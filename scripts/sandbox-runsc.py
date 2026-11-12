#!/usr/bin/python3 -I
"""Rootless OCI adapter: systemd owns limits; runsc owns syscall isolation.

Installed beside a pinned complete gVisor distribution. Never use against the
production Docker daemon. No commands, OCI specs or environment values are logged.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def systemctl(environment: dict[str, str], *args: str) -> str:
    return subprocess.check_output(
        ["/usr/bin/systemctl", "--user", *args],
        env=environment,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=15,
    ).strip()


def main() -> int:
    runtime = Path(__file__).resolve().parent / "runtime" / "runsc"
    arguments = sys.argv[1:]
    operation = next((arg for arg in arguments if arg in {"create", "exec", "delete"}), "")
    identifier = next((arg for arg in arguments if re.fullmatch(r"[a-f0-9]{64}", arg)), "")
    if not operation:
        os.execv(str(runtime), [str(runtime), "--ignore-cgroups", *arguments])
    if not identifier:
        raise ValueError("container_id_required")
    mapping = Path("/proc/self/uid_map").read_text().splitlines()[0].split()
    uid = int(mapping[1]) if int(mapping[0]) == 0 else getattr(os, "getuid", lambda: -1)()
    if uid <= 0 or uid == 10001:
        raise ValueError("dedicated_rootless_user_required")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ["HOME"],
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    unit = f"fullish-sandbox-runtime-{identifier}.service"
    allowed_limits = {(512 * 1024 * 1024, 64), (1024 * 1024 * 1024, 128)}
    if operation == "create":
        bundle = next(
            (arg.split("=", 1)[1] for arg in arguments if arg.startswith("--bundle=")),
            None,
        )
        if bundle is None and "--bundle" in arguments:
            bundle = arguments[arguments.index("--bundle") + 1]
        config = json.loads(
            (Path(bundle) if bundle else Path.cwd()).joinpath("config.json").read_text()
        )
        profiles = [
            value.split("=", 1)[1]
            for value in config.get("process", {}).get("env", [])
            if value.startswith("FULLISH_SANDBOX_PROFILE=")
        ]
        if not profiles or profiles == ["standard"]:
            memory, pids = "512M", 64
            allowed_limits = {(512 * 1024 * 1024, 64)}
        elif profiles == ["visualization"]:
            memory, pids = "1G", 128
            allowed_limits = {(1024 * 1024 * 1024, 128)}
        else:
            raise ValueError("unknown_image_resource_profile")
        subprocess.run(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--property=CPUQuota=100%",
                "--property=CPUQuotaPeriodSec=100ms",
                f"--property=MemoryMax={memory}",
                "--property=MemorySwapMax=0",
                f"--property=TasksMax={pids}",
                "--property=BindsTo=docker.service",
                "/usr/bin/sleep",
                "infinity",
            ],
            env=environment,
            check=True,
            timeout=15,
        )
    if operation in {"create", "exec"}:
        relative = systemctl(environment, "show", unit, "--property=ControlGroup", "--value")
        root = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
        directory = (Path("/sys/fs/cgroup") / relative.lstrip("/")).resolve()
        if not directory.is_relative_to(root) or directory.name != unit:
            raise ValueError("invalid_runtime_cgroup")
        quota, period = (directory / "cpu.max").read_text().split()
        if not (
            int(quota) == int(period) > 0
            and (
                int((directory / "memory.max").read_text()),
                int((directory / "pids.max").read_text()),
            )
            in allowed_limits
            and int((directory / "memory.swap.max").read_text()) == 0
        ):
            raise ValueError("runtime_limits_not_enforced")
        (directory / "cgroup.procs").write_text(str(os.getpid()))
        os.execv(str(runtime), [str(runtime), "--ignore-cgroups", *arguments])
    result = subprocess.run([str(runtime), "--ignore-cgroups", *arguments], check=False)
    if systemctl(environment, "show", unit, "--property=LoadState", "--value") != "not-found":
        systemctl(environment, "stop", unit)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Fullish runtime cgroup operation failed.", file=sys.stderr)
        raise SystemExit(1) from None

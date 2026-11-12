"""The OCI adapter must refuse execution outside an enforced dedicated cgroup."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class Executed(BaseException):
    pass


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "visualization",
        "profile_mismatch",
        "unknown_profile",
        "memory",
        "swap",
        "cpu",
        "pids",
        "root",
        "bot",
    ],
)
@pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup paths and UID mapping")
def test_adapter_requires_host_limits_before_starting_runsc(tmp_path, change):
    script = Path(__file__).resolve().parents[1] / "scripts/sandbox-runsc.py"
    main = runpy.run_path(str(script))["main"]
    identifier = "a" * 64
    uid = {"root": 0, "bot": 10001}.get(change, 1002)
    mapping = tmp_path / "uid_map"
    mapping.write_text(f"0 {uid} 1\n")
    relative = f"/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/"
    unit = f"fullish-sandbox-runtime-{identifier}.service"
    directory = tmp_path / "cgroups" / relative.lstrip("/") / unit
    directory.mkdir(parents=True)
    limits = {
        "cpu.max": "100000 100000",
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "64",
    }
    if change == "visualization":
        limits.update({"memory.max": "1073741824", "pids.max": "128"})
    profile = (
        ["FULLISH_SANDBOX_PROFILE=visualization"]
        if change in {"visualization", "profile_mismatch"}
        else ["FULLISH_SANDBOX_PROFILE=unbounded"]
        if change == "unknown_profile"
        else []
    )
    (tmp_path / "config.json").write_text(json.dumps({"process": {"env": profile}}))
    key = {
        "memory": "memory.max",
        "swap": "memory.swap.max",
        "cpu": "cpu.max",
        "pids": "pids.max",
    }.get(change)
    if key:
        limits[key] = "max"
    for name, value in limits.items():
        (directory / name).write_text(value)

    def path(value):
        if value == "/proc/self/uid_map":
            return mapping
        if str(value).startswith("/sys/fs/cgroup"):
            return tmp_path / "cgroups" / str(value).removeprefix("/sys/fs/cgroup").lstrip("/")
        return Path(value)

    execute = Mock(side_effect=Executed)
    commands = SimpleNamespace(
        run=Mock(), DEVNULL=subprocess.DEVNULL, check_output=Mock(return_value=relative + unit)
    )
    main.__globals__.update(
        Path=path,
        os=SimpleNamespace(
            environ={"HOME": "/var/lib/fullish-sandbox"},
            getuid=lambda: uid,
            getpid=lambda: 12345,
            execv=execute,
        ),
        subprocess=commands,
        sys=SimpleNamespace(
            argv=[
                str(script),
                "--platform=systrap",
                "create",
                "--bundle",
                str(tmp_path),
                identifier,
            ]
        ),
    )
    if change in {"valid", "visualization"}:
        with pytest.raises(Executed):
            main()
        assert (directory / "cgroup.procs").read_text() == "12345"
        assert "--ignore-cgroups" in execute.call_args.args[1]
        arguments = commands.run.call_args.args[0]
        assert f"--property=MemoryMax={'1G' if change == 'visualization' else '512M'}" in arguments
        assert "--property=MemorySwapMax=0" in arguments
        assert f"--property=TasksMax={128 if change == 'visualization' else 64}" in arguments
    else:
        with pytest.raises(ValueError):
            main()
        execute.assert_not_called()
        assert not (directory / "cgroup.procs").exists()

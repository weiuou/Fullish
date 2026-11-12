"""Actual kernel permission checks from the Dim UID in a synthetic CI container."""

import errno
import os
from pathlib import Path

assert os.environ.get("FULLISH_UNIFIED_CI") == "1"
assert os.getuid() == 10002 and os.getgid() == 10003
assert "CapEff:\t0000000000000000" in Path("/proc/self/status").read_text()


def denied(action):
    try:
        result = action()
        if hasattr(result, "close"):
            result.close()
    except OSError as error:
        assert error.errno in {errno.EPERM, errno.EACCES, errno.EROFS}
        return
    raise AssertionError("Dim unexpectedly crossed the Fullish boundary")


source = Path("/opt/fullish-readonly/src/fullish/notifications/deployment.py")
assert "asyncio.timeout(180)" in source.read_text()
denied(lambda: source.open("r+"))
denied(lambda: Path("/var/lib/fullish-diagnostics/events.json").open("r+"))
denied(lambda: Path("/var/lib/fullish/control/boundary-sentinel").read_text())
denied(lambda: Path("/var/lib/fullish/control/injected").open("w"))
denied(lambda: Path("/run/secrets/dim-bridge-token").read_text())
fullish = []
for directory in Path("/proc").iterdir():
    if not directory.name.isdigit():
        continue
    try:
        arguments = (directory / "cmdline").read_bytes().split(b"\0")
        if len(arguments) > 1 and arguments[1] == b"/workspace/tests/unified_fixture.py":
            fullish.append(directory)
    except (FileNotFoundError, PermissionError):
        pass
assert len(fullish) == 1
denied(lambda: (fullish[0] / "environ").read_bytes())
denied(lambda: os.kill(int(fullish[0].name), 0))
denied(lambda: os.setuid(0))
own = Path("/var/lib/fullish-integrated-agent/boundary-own")
own.write_text("owned-by-dim")
assert own.read_text() == "owned-by-dim"
own.unlink()
print("Kernel UID, source/state/diagnostic/secret/proc/signal/root boundaries passed.")

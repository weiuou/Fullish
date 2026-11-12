"""Unprivileged role wrapper; its parent can request shutdown through stdin only."""

import os
import select
import signal
import stat
import subprocess
import sys
from pathlib import Path


def owned(path: str, mode: int) -> None:
    directory = Path(path)
    try:
        directory.mkdir(mode=mode)
        os.chown(directory, -1, 10003)
        directory.chmod(mode)
    except FileExistsError:
        pass
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise RuntimeError("invalid_role_directory")


role, *command = sys.argv[1:]
assert os.geteuid() == (10002 if role == "dim" else 10001)
assert os.getegid() == 10003
os.umask(0o077)
owned(os.environ["TMPDIR"], 0o700)
if role == "dim":
    owned(os.environ["DIM_HOME"], 0o700)
    owned(str(Path(os.environ["DIM_ENDPOINT"]).parent), 0o2750)
    owned(os.environ["DIM_WORKSPACE_ROOT"], 0o2770)
else:
    owned(str(Path(os.environ["DIM_BRIDGE_SOCKET"]).parent), 0o2750)
child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
stopping = False


def stop(*_args):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while child.poll() is None and not stopping:
    readable, _, _ = select.select([sys.stdin], [], [], 0.2)
    if readable:
        sys.stdin.buffer.read1(32)
        stopping = True
if child.poll() is None:
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
raise SystemExit(0 if stopping else child.returncode)

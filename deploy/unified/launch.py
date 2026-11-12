"""Fixed two-user bootstrap. It relinquishes root before supervising role pipes."""

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

assert os.geteuid() == 0, "Unified bootstrap requires the explicit root startup profile"
os.setgroups([10003])  # Bootstrap may traverse the private IPC directories, then drops this group.
command = sys.argv[1:] or ["python", "-m", "fullish"]
with_agent = command[:1] == ["--with-agent"]
if with_agent:
    command = command[1:]
preflight = command == ["python", "-m", "fullish.agents.check", "--preflight"]
serve = command == ["python", "-m", "fullish"]
start_agent = os.environ.get("AGENT_BACKEND") == "dim" and (serve or with_agent or preflight)
environment = dict(os.environ)
environment["DIM_DEPLOYMENT"] = "unified"
root = "/run/fullish-integrated"
if preflight:
    root = tempfile.mkdtemp(prefix="fullish-preflight-")
    Path(root).chmod(0o1777)  # Each child creates its own protected subdirectories.
    environment["DIM_HOME"] = root + "/home"
    environment["DIM_WORKSPACE_ROOT"] = root + "/work"
else:
    environment["DIM_HOME"] = "/var/lib/fullish-integrated-agent"
    environment["DIM_WORKSPACE_ROOT"] = "/var/lib/fullish-integrated-work"
environment["DIM_ENDPOINT"] = root + "/dim/rpc.sock"
environment["DIM_BRIDGE_SOCKET"] = root + "/fullish/bridge.sock"
environment["DIM_BRIDGE_TOKEN_FILE"] = "/run/secrets/dim-bridge-token"
roles = []


def spawn(role: str, args: list[str]) -> subprocess.Popen:
    if role == "dim":
        keys = {
            "DIM_MODEL_ID",
            "DIM_MODEL_BASE_URL",
            "DIM_DEPLOYMENT",
            "DIM_HOME",
            "DIM_ENDPOINT",
            "DIM_WORKSPACE_ROOT",
            "DIM_BRIDGE_SOCKET",
            "DIM_INSPECTION",
        }
        env = {key: value for key, value in environment.items() if key in keys}
        env["DIM_BRIDGE_TOKEN_FILE"] = "/run/secrets/dim-bridge-token-agent"
        env.update(PATH="/usr/local/bin:/usr/bin:/bin", LANG="C.UTF-8")
    else:
        env = dict(environment)
    env["TMPDIR"] = "/tmp/fullish-" + role
    env["HOME"] = env["TMPDIR"]
    process = subprocess.Popen(
        ["/app/.venv/bin/python", "/opt/fullish-integrated/role.py", role, *args],
        env=env,
        user=10002 if role == "dim" else 10001,
        group=10003,
        extra_groups=[],
        stdin=subprocess.PIPE,
    )
    roles.append(process)
    return process


try:
    if start_agent:
        agent = spawn("dim", ["/usr/local/bin/node", "/opt/fullish-agent/launch.mjs"])
        deadline = time.monotonic() + 30
        while True:
            if agent.poll() is not None:
                raise RuntimeError("unified_agent_start_failed")
            try:
                info = Path(environment["DIM_ENDPOINT"]).lstat()
                if (
                    stat.S_ISSOCK(info.st_mode)
                    and info.st_uid == 10002
                    and info.st_gid == 10003
                    and stat.S_IMODE(info.st_mode) == 0o660
                ):
                    break
            except FileNotFoundError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("unified_agent_start_timeout")
            time.sleep(0.05)
    bot = spawn("fullish", command)
finally:
    # No role can inherit the other's env or use this supervisor to regain root.
    environment.clear()
    os.environ.clear()
    os.setgroups([])
    os.setgid(65534)
    os.setuid(65534)

stopping = False


def stop(*_args):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while not stopping and all(role.poll() is None for role in roles):
    time.sleep(0.1)
result = 0 if stopping or (not serve and bot.poll() == 0) else 1
for role in roles:
    if role.stdin:
        role.stdin.close()  # The role wrapper signals only its own UID/process group.
deadline = time.monotonic() + 24
while any(role.poll() is None for role in roles) and time.monotonic() < deadline:
    time.sleep(0.1)
if any(role.poll() is None for role in roles):
    result = 1
# Container init tears down the namespace if a role cannot confirm stopping.
raise SystemExit(result)

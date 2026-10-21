"""Trusted command wrapper inside the disposable gVisor image (stdlib only)."""

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

OUTPUT_BYTES = 12000


def cleanup() -> None:
    """One command owns the entire container; reap even detached background children."""
    # Never run this cleanup against an ordinary host PID namespace.
    if Path("/proc/1/cmdline").read_bytes().split(b"\0")[:4] != [
        b"python3",
        b"-I",
        b"/opt/fullish/worker.py",
        b"--idle",
    ]:
        raise RuntimeError("sandbox_pid_namespace_required")
    for _ in range(4):
        victims = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit() and int(entry.name) not in {1, os.getpid()}:
                victims.append(int(entry.name))
        if not victims:
            return
        for pid in victims:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        time.sleep(0.03)
    # The host verifies cleanup; uncertainty causes container replacement.


def execute(command: str, timeout: int) -> dict:
    # Clean old group files without following symlinks; the entire workspace is quota-limited.
    cutoff = time.time() - 86400
    for parent, _, files in os.walk("/workspace", followlinks=False):
        for name in files:
            path = Path(parent) / name
            try:
                if path.lstat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass
    process = subprocess.Popen(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd="/workspace",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/workspace",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    kept = bytearray()
    received = 0
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(left, 0.1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        received += len(chunk)
                        kept.extend(chunk[: max(0, OUTPUT_BYTES - len(kept))])
            if not timed_out:
                try:
                    process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        cleanup()
        process.wait(timeout=2)
        process.stdout.close()
    return {
        "output": kept.decode("utf-8", errors="replace"),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "truncated": received > len(kept),
    }


def main() -> None:
    if sys.platform != "linux":
        raise RuntimeError("linux_required")
    if sys.argv[1:] == ["--idle"]:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            signal.pause()
    elif sys.argv[1:] == ["--cleanup"]:
        cleanup()
        others = [
            p.name
            for p in Path("/proc").iterdir()
            if p.name.isdigit() and int(p.name) not in {1, os.getpid()}
        ]
        if others:
            raise SystemExit(1)
    else:
        data = json.loads(sys.stdin.buffer.read(16385))
        if (
            not isinstance(data, dict)
            or set(data) != {"command", "timeout_seconds"}
            or not isinstance(data["command"], str)
            or not 1 <= len(data["command"]) <= 8192
            or type(data["timeout_seconds"]) is not int
            or not 1 <= data["timeout_seconds"] <= 30
        ):
            raise ValueError("invalid_request")
        print(json.dumps(execute(data["command"], data["timeout_seconds"]), ensure_ascii=False))


if __name__ == "__main__":
    main()

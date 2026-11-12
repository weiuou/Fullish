"""Real rootless gVisor acceptance on a disposable Linux CI host, never production."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args: str, **kwargs) -> str:
    result = subprocess.run(
        args, text=True, timeout=300, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
    )
    if result.returncode:
        print(result.stdout, flush=True)
        subprocess.run(
            ["journalctl", "-u", "fullish-sandbox.service", "-n", "35", "--no-pager"],
            check=False,
            timeout=10,
        )
        uid = subprocess.check_output(["id", "-u", "fullish-sandbox"], text=True).strip()
        docker = [
            "runuser",
            "-u",
            "fullish-sandbox",
            "--",
            "env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/var/lib/fullish-sandbox",
            "docker",
            "--host",
            f"unix:///run/user/{uid}/docker.sock",
        ]
        rows = subprocess.run(docker + ["ps", "-aq"], text=True, capture_output=True, timeout=10)
        for identifier in rows.stdout.split():
            subprocess.run(
                docker + ["inspect", "--format", "{{json .State}}", identifier],
                check=False,
                timeout=10,
            )
            # Fresh disposable CI host only: fixed commands with synthetic about:blank,
            # never an arbitrary source or token. Keep raw native startup errors that the
            # production broker correctly sanitizes, without changing any runtime limits.
            try:
                subprocess.run(
                    docker
                    + [
                        "exec",
                        "--env",
                        "HOME=/workspace",
                        identifier,
                        "/bin/sh",
                        "-c",
                        "node --version; chromium --headless --disable-gpu --dump-dom about:blank",
                    ],
                    check=False,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                print("Synthetic browser diagnostic timed out", flush=True)
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout.strip()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if (
        getattr(os, "getuid", lambda: -1)() != 0
        or os.environ.get("FULLISH_SANDBOX_CI") != "1"
        or Path("/opt/fullish").exists()
        or Path("/etc/fullish-sandbox.env").exists()
    ):
        raise SystemExit("Use a fresh disposable CI host with FULLISH_SANDBOX_CI=1.")
    image = sys.argv[1]
    run("bash", str(root / "scripts/sandbox-runtime-setup.sh"))
    uid = run("id", "-u", "fullish-sandbox")
    docker = [
        "runuser",
        "-u",
        "fullish-sandbox",
        "--",
        "env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/var/lib/fullish-sandbox",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "docker",
        "--host",
        f"unix:///run/user/{uid}/docker.sock",
    ]
    # Load the exact worker image already verified in this job. The dedicated
    # user needs neither access to the runner's checkout nor another image pull.
    worker_images = ["fullish-sandbox:ci", *sys.argv[2:3]]
    for worker_image in worker_images:
        with subprocess.Popen(["docker", "save", worker_image], stdout=subprocess.PIPE) as saved:
            assert saved.stdout is not None
            subprocess.run([*docker, "load"], stdin=saved.stdout, check=True, timeout=180)
            saved.stdout.close()
            if saved.wait(timeout=10):
                raise RuntimeError("Worker image export failed")
    pinned = run(*docker, "image", "inspect", "--format", "{{.Id}}", "fullish-sandbox:ci")
    run("bash", str(root / "scripts/server-setup.sh"), "ghcr.io/example/fullish")
    group = "synthetic-integration-group"
    setup = ["bash", str(root / "scripts/sandbox-setup.sh"), group, pinned]
    print(run(*setup), flush=True)
    environment = {**os.environ, "FULLISH_IMAGE": image}
    candidate = [
        "docker",
        "compose",
        "-p",
        "fullish",
        "-f",
        "/opt/fullish/compose.yaml",
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "bot",
        "python",
        "-m",
        "fullish.sandbox.check",
    ]
    print(run(*candidate, env=environment), flush=True)
    # Exact regression: a healthy bot image with no mounted broker socket must fail.
    missing_mount = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--env-file",
            "/opt/fullish/runtime.env",
            image,
            "python",
            "-m",
            "fullish.sandbox.check",
        ],
        capture_output=True,
        timeout=60,
    )
    assert missing_mount.returncode != 0, "Missing socket mount incorrectly passed"
    print("PASS missing socket mount blocks acceptance", flush=True)
    run("systemctl", "stop", "fullish-sandbox.service")
    unavailable = subprocess.run(candidate, env=environment, capture_output=True, timeout=60)
    assert unavailable.returncode != 0, "Stopped broker incorrectly passed"
    run("systemctl", "start", "fullish-sandbox.service")
    # Reinstallation waits for readiness and preserves credentials and allowlists.
    before = Path("/etc/fullish-sandbox.env").read_bytes()
    print(run(*setup), flush=True)
    assert Path("/etc/fullish-sandbox.env").read_bytes() == before
    print("PASS repeated setup preserves accepted configuration", flush=True)
    rows = json.loads(run(*docker, "inspect", run(*docker, "ps", "-q")))
    container = rows[0]
    pid = container["State"]["Pid"]
    path = next(
        line[3:]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    directory = Path("/sys/fs/cgroup") / path.lstrip("/")
    assert directory.name == f"fullish-sandbox-runtime-{container['Id']}.service"
    assert "gvisor" in Path(f"/proc/{pid}/exe").resolve().name
    assert int((directory / "memory.max").read_text()) == 512 * 1024 * 1024
    assert int((directory / "memory.swap.max").read_text()) == 0
    assert int((directory / "pids.max").read_text()) == 64
    quota, period = (directory / "cpu.max").read_text().split()
    assert int(quota) == int(period) > 0
    # A changed real cgroup limit must cause replacement, not silent execution.
    (directory / "pids.max").write_text("65")
    print(run(*candidate, env=environment), flush=True)
    assert run(*docker, "ps", "-q", "--no-trunc") != container["Id"]
    print("PASS host cgroup drift replaces the sandbox", flush=True)
    # A rootless daemon restart must clean old scopes and allow the broker to recover.
    run(
        "runuser",
        "-u",
        "fullish-sandbox",
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        "systemctl",
        "--user",
        "restart",
        "docker",
    )
    print(run(*candidate, env=environment), flush=True)
    print("PASS daemon restart recovers with enforced limits", flush=True)
    if len(worker_images) > 1:
        visualization = run(*docker, "image", "inspect", "--format", "{{.Id}}", worker_images[1])
        print(run("bash", str(root / "scripts/sandbox-setup.sh"), group, visualization), flush=True)
        visual_container = json.loads(run(*docker, "inspect", run(*docker, "ps", "-q")))[0]
        visual_pid = visual_container["State"]["Pid"]
        assert "gvisor" in Path(f"/proc/{visual_pid}/exe").resolve().name
        visual_path = next(
            line[3:]
            for line in Path(f"/proc/{visual_pid}/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        visual_cgroup = Path("/sys/fs/cgroup") / visual_path.lstrip("/")
        assert int((visual_cgroup / "memory.max").read_text()) == 1024 * 1024 * 1024
        assert int((visual_cgroup / "memory.swap.max").read_text()) == 0
        assert int((visual_cgroup / "pids.max").read_text()) == 128
        print("PASS visualization gVisor has bounded 1 GiB/128 PID host profile", flush=True)
        # This process reaches the real broker, worker and native browser. It does not call
        # a model or QQ and never treats a mocked browser as gVisor acceptance.
        print(
            run(
                "python3",
                str(root / "scripts/visualization-smoke.py"),
            ),
            flush=True,
        )
        if len(sys.argv) > 3:
            with tempfile.TemporaryDirectory(prefix="fullish-dim-integration-") as temporary:
                staging = Path(temporary)
                fixture = run("docker", "create", sys.argv[3])
                try:
                    for source, target in (
                        ("/opt/fullish-agent/node_modules", "modules"),
                        ("/opt/fullish-plugin", "plugin"),
                        ("/usr/local/bin/node", "node"),
                    ):
                        run("docker", "cp", f"{fixture}:{source}", str(staging / target))
                    mounts = []
                    for source, target in (
                        (str(root), "/workspace"),
                        (str(staging / "modules"), "/opt/fullish-agent/node_modules"),
                        (str(staging / "plugin"), "/opt/fullish-plugin"),
                        (str(staging / "node"), "/usr/local/bin/node"),
                        ("/run/fullish-sandbox", "/run/fullish-sandbox"),
                        ("/var/lib/fullish-control", "/var/lib/fullish/control"),
                    ):
                        mounts.extend(
                            ["--mount", f"type=bind,source={source},target={target},readonly"]
                        )
                    print(
                        run(
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            "host",
                            "--read-only",
                            "--tmpfs",
                            "/tmp:rw,noexec,nosuid,size=64m",
                            "--cap-drop",
                            "ALL",
                            "--security-opt",
                            "no-new-privileges:true",
                            "--pids-limit",
                            "128",
                            "--memory",
                            "512m",
                            "--env-file",
                            "/opt/fullish/runtime.env",
                            "--env",
                            "GROUP_CONTROL_DIR=/var/lib/fullish/control",
                            *mounts,
                            "--entrypoint",
                            "python",
                            image,
                            "/workspace/scripts/probe_dim_plugin.py",
                            "--executable",
                            "/opt/fullish-agent/node_modules/dimcode-linux-x64/bin/dimcode",
                            "--plugin",
                            "/opt/fullish-plugin",
                            "--sandbox-group",
                            group,
                        ),
                        flush=True,
                    )
                finally:
                    run("docker", "rm", fixture)


if __name__ == "__main__":
    main()

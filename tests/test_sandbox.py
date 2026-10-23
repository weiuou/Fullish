"""Fail-closed broker policy, reuse, authentication and cleanup decisions."""

import base64
import json
import os
import threading
from unittest.mock import Mock

import httpx
import pytest

from fullish.sandbox_server import (
    TEMP_OPTIONS,
    WORKER,
    WORKSPACE_OPTIONS,
    SandboxError,
    Sandboxes,
    Server,
)

GROUP = "g" * 32


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setenv("SANDBOX_TOKEN", "token-" * 8)
    monkeypatch.setenv("TOOLS_ALLOWED_GROUPS", GROUP)
    monkeypatch.setenv("SANDBOX_DOCKER_HOST", "unix:///run/user/1234/docker.sock")
    return Sandboxes()


def container(broker):
    return {
        "State": {"Running": True, "Pid": 123},
        "HostConfig": {
            "Runtime": "runsc",
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Memory": 512 * 1024 * 1024,
            "MemorySwap": 512 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {"/workspace": WORKSPACE_OPTIONS, "/tmp": TEMP_OPTIONS},
            "IpcMode": "private",
        },
        "Config": {
            "Image": broker.image,
            "User": "10001:10001",
            "Env": ["PATH=/usr/bin"],
            "Entrypoint": ["python3", "-I", WORKER],
            "Cmd": ["--idle"],
        },
    }


def request():
    return {
        "group_id": GROUP,
        "sender_id": "member",
        "message_id": "message",
        "command": "python3 -c 'print(4)'",
        "timeout_seconds": 30,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Runtime", "runc"),
        ("NetworkMode", "host"),
        ("PidMode", "host"),
        ("IpcMode", "host"),
        ("Privileged", True),
        ("ReadonlyRootfs", False),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("NanoCpus", 0),
        ("PidsLimit", 0),
        ("CapAdd", ["SYS_ADMIN"]),
        ("Binds", ["/:/host"]),
        ("Devices", [{}]),
        ("PortBindings", {"80/tcp": [{}]}),
    ],
)
def test_unsafe_existing_container_is_not_accepted(broker, key, value):
    broker.image_env = ["PATH=/usr/bin"]
    item = container(broker)
    assert broker.valid(item)
    item["HostConfig"][key] = value
    assert not broker.valid(item)


def test_container_with_injected_credentials_is_rejected(broker):
    broker.image_env = ["PATH=/usr/bin"]
    item = container(broker)
    item["Config"]["Env"].append("QQ_APP_SECRET=not-permitted")
    assert not broker.valid(item)


def test_reuse_does_not_launch_a_new_container_and_group_names_are_scoped(broker):
    broker.image_env = ["PATH=/usr/bin"]
    broker.docker = Mock(return_value=json.dumps([container(broker)]))
    broker.effective_limits = Mock(return_value=True)
    assert broker.ensure(GROUP) == broker.ensure(GROUP)
    assert all(
        call.args[0][:2] == ["container", "inspect"] for call in broker.docker.call_args_list
    )
    assert broker.name(GROUP) != broker.name("other")
    assert GROUP not in broker.name(GROUP)


@pytest.mark.parametrize(
    "info",
    [
        {"CgroupVersion": "2", "SecurityOptions": [], "Runtimes": {"runsc": {}}},
        {"CgroupVersion": "1", "SecurityOptions": ["name=rootless"], "Runtimes": {"runsc": {}}},
        {"CgroupVersion": "2", "SecurityOptions": ["name=rootless"], "Runtimes": {"runc": {}}},
    ],
)
def test_missing_isolation_prerequisites_never_fall_back_to_runc(broker, info):
    broker.docker = Mock(return_value=json.dumps(info))
    with pytest.raises(SandboxError, match="rootless_runsc_with_cgroups_required"):
        broker.check_runtime()
    assert broker.docker.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"group_id": "other"},
        {"timeout_seconds": 1000},
        {"timeout_seconds": True},
        {"image": "evil"},
        {"command": "x" * 8193},
        {"sender_id": ""},
    ],
)
def test_unauthorized_or_extended_requests_do_not_reach_docker(broker, change):
    broker.docker = Mock()
    with pytest.raises(SandboxError, match="invalid_or_unauthorized_request"):
        broker.execute({**request(), **change})
    broker.docker.assert_not_called()


def test_command_is_only_stdin_data_and_cleanup_runs_before_and_after(broker):
    broker.check_runtime = Mock()
    broker.ensure = Mock(return_value="sandbox-test")
    broker.docker = Mock(
        return_value=json.dumps(
            {"output": "4", "exit_code": 0, "timed_out": False, "truncated": False}
        )
    )
    data = request()
    data["command"] = "echo '$(do-not-execute-on-host)'; python3 -c 'print(4)'"
    assert broker.execute(data)["output"] == "4"
    calls = broker.docker.call_args_list
    assert len(calls) == 3
    assert calls[0].args[0][-1] == calls[2].args[0][-1] == "--cleanup"
    assert data["command"] not in str(calls[1].args)
    assert json.loads(calls[1].kwargs["data"])["command"] == data["command"]
    assert not broker.lock.locked()


def test_wrapper_failure_discards_container_and_releases_lock(broker):
    broker.check_runtime = Mock()
    broker.ensure = Mock(return_value="sandbox-test")
    broker.docker = Mock(side_effect=["", SandboxError("sandbox_runtime_timeout"), ""])
    with pytest.raises(SandboxError, match="sandbox_runtime_timeout"):
        broker.execute(request())
    assert broker.docker.call_args.args[0] == ["rm", "--force", "sandbox-test"]
    assert not broker.lock.locked()


def test_busy_broker_cannot_start_parallel_commands(broker):
    broker.check_runtime = Mock()
    with broker.lock:
        with pytest.raises(SandboxError, match="sandbox_busy"):
            broker.execute(request())
    broker.check_runtime.assert_not_called()


def test_unenforced_cgroup_limits_discard_container(broker):
    broker.image_env = ["PATH=/usr/bin"]
    broker.effective_limits = Mock(return_value=False)
    item = container(broker)
    broker.docker = Mock(side_effect=[json.dumps([item]), "", "", json.dumps([item]), ""])
    with pytest.raises(SandboxError, match="sandbox_constraints_not_enforced"):
        broker.ensure(GROUP)
    starts = [call.args[0] for call in broker.docker.call_args_list if call.args[0][0] == "run"]
    assert len(starts) == 1 and "--runtime=runsc" in starts[0]
    assert "--pull=never" in starts[0]


@pytest.mark.skipif(os.name != "posix", reason="Unix socket broker runs on Linux")
def test_unix_socket_requires_auth_and_rejects_oversize_body(broker, tmp_path):
    path = str(tmp_path / "broker.sock")
    broker.execute = Mock(return_value={"output": "ok"})
    with Server(path, broker) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=path), base_url="http://sandbox"
            ) as client:
                assert client.post("/exec", json=request()).status_code == 401
                assert (
                    client.post(
                        "/exec", json=request(), headers={"Authorization": "Bearer wrong"}
                    ).status_code
                    == 401
                )
                headers = {"Authorization": "Bearer " + broker.token}
                assert (
                    client.post("/exec", content=b"x" * 17000, headers=headers).status_code == 503
                )
                broker.execute.assert_not_called()
                assert client.post("/exec", json=request(), headers=headers).json() == {
                    "output": "ok"
                }
                broker.execute.assert_called_once_with(request())
                broker.transfer = Mock(return_value={"path": "ref.png", "size": 4})
                payload = {
                    key: value
                    for key, value in request().items()
                    if key not in {"command", "timeout_seconds"}
                }
                payload["path"] = "ref.png"
                assert client.post("/files/read", json=payload).status_code == 401
                assert (
                    client.post("/files/read", content=b"x" * 17000, headers=headers).status_code
                    == 503
                )
                broker.transfer.assert_not_called()
                assert client.post("/files/read", json=payload, headers=headers).json() == {
                    "path": "ref.png",
                    "size": 4,
                }
                broker.transfer.assert_called_once_with("read", payload)
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_worker_cleanup_refuses_the_host_pid_namespace(monkeypatch):
    from fullish import sandbox_worker

    monkeypatch.setattr(sandbox_worker.Path, "read_bytes", lambda _: b"host-init\0")
    kill = Mock()
    monkeypatch.setattr(sandbox_worker.os, "kill", kill)
    with pytest.raises(RuntimeError, match="sandbox_pid_namespace_required"):
        sandbox_worker.cleanup()
    kill.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"group_id": "other"},
        {"path": "../secret"},
        {"path": "/etc/passwd"},
        {"path": "x", "image": "evil"},
        {"sender_id": ""},
    ],
)
def test_file_exchange_rejects_authority_and_path_changes_before_docker(broker, change):
    broker.docker = Mock()
    data = {"group_id": GROUP, "sender_id": "member", "message_id": "message", "path": "ref.png"}
    with pytest.raises(SandboxError):
        broker.transfer("read", {**data, **change})
    broker.docker.assert_not_called()


def test_file_exchange_uses_stdin_and_bounded_output_with_existing_cleanup(broker):
    broker.check_runtime = Mock()
    broker.ensure = Mock(return_value="sandbox-test")
    encoded = base64.b64encode(b"synthetic-image-bytes").decode()
    broker.docker = Mock(return_value=json.dumps({"path": "ref.png", "size": 21, "data": encoded}))
    data = {"group_id": GROUP, "sender_id": "member", "message_id": "message", "path": "ref.png"}
    result = broker.transfer("read", data)
    assert result["data"] == encoded
    calls = broker.docker.call_args_list
    assert len(calls) == 3
    assert calls[1].args[0][-1] == "--file"
    assert json.loads(calls[1].kwargs["data"]) == {"operation": "read", "path": "ref.png"}
    assert calls[1].kwargs["output_limit"] > 10 * 1024 * 1024
    assert not broker.lock.locked()

"""Check SSH validation, encrypted credential delivery and temporary-key cleanup."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "ssh_deploy", Path(__file__).parents[1] / "scripts" / "ssh_deploy.py"
)
assert SPEC and SPEC.loader
ssh_deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ssh_deploy)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "SSH_HOST": "example.com",
        "SSH_PORT": "22",
        "SSH_DEPLOY_KEY": "private-key-placeholder",
        "SSH_KNOWN_HOSTS": "example.com ssh-ed25519 placeholder",
        "FULLISH_IMAGE": "ghcr.io/example/fullish@sha256:" + "a" * 64,
        "REGISTRY_PULL_USERNAME": "example",
        "REGISTRY_PULL_PASSWORD": "token-placeholder",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("QQ_APP_ID", raising=False)
    monkeypatch.delenv("QQ_APP_SECRET", raising=False)


def test_credentials_use_stdin_and_key_is_removed(configured, monkeypatch) -> None:
    key_paths = []

    def run(command, **kwargs):
        key = Path(command[command.index("-i") + 1])
        key_paths.append(key)
        assert key.read_text() == "private-key-placeholder\n"
        assert "StrictHostKeyChecking=yes" in command
        assert "IdentitiesOnly=yes" in command
        assert "token-placeholder" not in " ".join(command)
        assert kwargs["input"] == "example\ntoken-placeholder\n{}\n"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ssh_deploy.subprocess, "run", run)
    assert ssh_deploy.main() == 0
    assert len(key_paths) == 1 and not key_paths[0].exists()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SSH_HOST", "example.com; touch /tmp/pwned"),
        ("SSH_HOST", "-oProxyCommand=bad"),
        ("SSH_PORT", "0"),
        ("SSH_PORT", "65536"),
        ("FULLISH_IMAGE", "ghcr.io/example/fullish:latest"),
        ("REGISTRY_PULL_USERNAME", "user\nextra"),
        ("REGISTRY_PULL_PASSWORD", "token\nextra"),
        ("QQ_APP_ID", "1234"),
        ("QQ_APP_SECRET", "partial-secret"),
    ],
)
def test_reject_invalid_input_before_ssh(configured, monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)

    def run(*args, **kwargs):
        pytest.fail("Invalid configuration must never start SSH")

    monkeypatch.setattr(ssh_deploy.subprocess, "run", run)
    assert ssh_deploy.main() == 1


def test_timeout_is_not_retried(configured, monkeypatch, capsys) -> None:
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        raise subprocess.TimeoutExpired(command, 960)

    monkeypatch.setattr(ssh_deploy.subprocess, "run", run)
    assert ssh_deploy.main() == 1
    assert len(attempts) == 1
    assert "token-placeholder" not in capsys.readouterr().err


def test_qq_credentials_only_travel_in_stdin(configured, monkeypatch) -> None:
    monkeypatch.setenv("QQ_APP_ID", "1234")
    monkeypatch.setenv("QQ_APP_SECRET", "qq-secret-placeholder")

    def run(command, **kwargs):
        assert "qq-secret-placeholder" not in " ".join(command)
        payload = json.loads(kwargs["input"].splitlines()[2])
        assert payload == {"QQ_APP_ID": "1234", "QQ_APP_SECRET": "qq-secret-placeholder"}
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ssh_deploy.subprocess, "run", run)
    assert ssh_deploy.main() == 0

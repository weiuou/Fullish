"""Deploy through a pinned SSH host using a dedicated, forced-command key."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Set {name} before deploying.")
    return value


def main() -> int:
    try:
        host = required("SSH_HOST")
        port = os.environ.get("SSH_PORT", "22")
        image = required("FULLISH_IMAGE")
        username = required("REGISTRY_PULL_USERNAME")
        password = required("REGISTRY_PULL_PASSWORD")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ValueError("SSH_HOST must be an IPv4 address or hostname.")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("SSH_PORT must be between 1 and 65535.")
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)+"
            r"@sha256:[a-f0-9]{64}",
            image,
        ):
            raise ValueError("FULLISH_IMAGE must be a full immutable image digest.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", username):
            raise ValueError("Invalid registry username.")
        if "\n" in password or "\r" in password:
            raise ValueError("Registry credentials must fit on one line.")

        with tempfile.TemporaryDirectory(prefix="fullish-ssh-") as directory:
            key_path = Path(directory, "deploy_key")
            hosts_path = Path(directory, "known_hosts")
            key_path.write_text(required("SSH_DEPLOY_KEY").rstrip() + "\n", encoding="utf-8")
            key_path.chmod(0o600)
            hosts_path.write_text(required("SSH_KNOWN_HOSTS").rstrip() + "\n", encoding="utf-8")
            command = [
                "ssh",
                "-T",
                "-p",
                port,
                "-i",
                str(key_path),
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={hosts_path}",
                "-o",
                "ConnectTimeout=15",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                f"fullish-deploy@{host}",
                f"deploy {image}",
            ]
            # Credentials travel through encrypted stdin, never command arguments.
            result = subprocess.run(
                command,
                input=f"{username}\n{password}\n",
                text=True,
                timeout=960,
                check=False,
            )
            return result.returncode
    except (ValueError, subprocess.TimeoutExpired) as error:
        if isinstance(error, subprocess.TimeoutExpired):
            print("SSH observation timed out; inspect the server before retrying.", file=sys.stderr)
        else:
            print(f"Deployment configuration error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

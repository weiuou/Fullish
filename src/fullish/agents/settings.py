"""Explicit opt-in configuration for the dedicated pinned Dim runtime."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from fullish.agents.local import unified


@dataclass(frozen=True)
class AgentSettings:
    endpoint: str
    workspace: Path
    state_path: Path
    groups: frozenset[str]
    enabled: bool = False
    executable: Path | None = None
    timeout: float = 90
    visualization: bool = False
    bridge_socket: Path | None = None
    bridge_token_file: Path | None = None
    inspection: bool = False

    @classmethod
    def from_env(cls) -> Self | None:
        backend = os.environ.get("AGENT_BACKEND", "legacy").strip()
        if backend not in {"legacy", "dim"}:
            raise ValueError("AGENT_BACKEND must be legacy or dim")
        endpoint = os.environ.get("DIM_ENDPOINT", "").strip()
        if not endpoint:
            if backend == "dim":
                raise ValueError("Dim requires DIM_ENDPOINT")
            return None
        workspace = Path(os.environ.get("DIM_WORKSPACE_ROOT", ""))
        if not workspace.is_absolute():
            raise ValueError("DIM_WORKSPACE_ROOT must be an absolute shared directory")
        groups = frozenset(
            x.strip() for x in os.environ.get("DIM_ALLOWED_GROUPS", "").split(",") if x.strip()
        )
        if not groups or "*" in groups:
            raise ValueError("DIM_ALLOWED_GROUPS must explicitly name test groups")
        directory = Path(os.environ.get("GROUP_CONTROL_DIR", ".state"))
        executable = os.environ.get("DIM_EXECUTABLE", "").strip()
        if os.name == "nt":
            if not executable or not Path(executable).is_absolute():
                raise ValueError("Windows Dim requires an absolute DIM_EXECUTABLE")
            if not endpoint.startswith("\\\\.\\pipe\\"):
                raise ValueError("DIM_ENDPOINT must be a private named pipe")
        elif not Path(endpoint).is_absolute():
            raise ValueError("DIM_ENDPOINT must be an absolute Unix socket path")
        visualization = (
            os.environ.get("DIM_VISUALIZATION", os.environ.get("SANDBOX_VISUALIZATION", "false"))
            .strip()
            .lower()
        )
        if visualization not in {"true", "false"}:
            raise ValueError("DIM_VISUALIZATION must be true or false")
        inspection = os.environ.get("DIM_INSPECTION", "false").strip().lower()
        if inspection not in {"true", "false"}:
            raise ValueError("DIM_INSPECTION must be true or false")
        if inspection == "true" and not unified():
            raise ValueError("DIM_INSPECTION requires the unified deployment profile")
        bridge_socket = os.environ.get("DIM_BRIDGE_SOCKET", "").strip()
        bridge_token = os.environ.get("DIM_BRIDGE_TOKEN_FILE", "").strip()
        if (visualization == "true" or inspection == "true") and (
            os.name == "nt"
            or not Path(bridge_socket).is_absolute()
            or not Path(bridge_token).is_absolute()
        ):
            raise ValueError(
                "Visualize requires private Linux DIM_BRIDGE_SOCKET and token file paths"
            )
        return cls(
            endpoint,
            workspace,
            directory / "agent-runs.sqlite3",
            groups,
            enabled=backend == "dim",
            executable=Path(executable) if executable else None,
            visualization=visualization == "true",
            bridge_socket=Path(bridge_socket) if bridge_socket else None,
            bridge_token_file=Path(bridge_token) if bridge_token else None,
            inspection=inspection == "true",
        )

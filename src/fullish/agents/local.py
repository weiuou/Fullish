"""Fixed Unix identities for the explicitly enabled single-container deployment."""

import os
import stat
from pathlib import Path


def unified() -> bool:
    return os.environ.get("DIM_DEPLOYMENT") == "unified"


def private_rpc(info: os.stat_result) -> bool:
    if not stat.S_ISSOCK(info.st_mode):
        return False
    if unified():
        return (
            os.geteuid() == 10001
            and info.st_uid == 10002
            and info.st_gid == 10003
            and stat.S_IMODE(info.st_mode) == 0o660
        )
    return info.st_uid == os.geteuid() and not info.st_mode & 0o077


def task_workspace(root: Path, group: str, run_id: str) -> Path:
    workspace = root / group / run_id
    if not unified():
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace
    for path in [root / group, workspace]:
        try:
            path.mkdir(mode=0o770)
            path.chmod(0o2770)
        except FileExistsError:
            pass
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 10001
            or info.st_gid != 10003
            or stat.S_IMODE(info.st_mode) != 0o2770
        ):
            raise ValueError("invalid_shared_task_workspace")
    return workspace

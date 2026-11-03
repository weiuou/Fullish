"""Bounded file exchange inside /workspace; stdlib only, no host file access."""

import base64
import os
import stat
from pathlib import Path
from uuid import uuid4

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_JSON = (MAX_FILE_BYTES + 2) // 3 * 4 + 4096
ROOT = Path("/workspace")


def relative_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 512 or "\\" in path or "\x00" in path:
        raise ValueError("sandbox_file_path")
    if path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
    if (
        not path
        or path.startswith("/")
        or len(path.split("/")) > 8
        or any(part in {"", ".", ".."} or len(part) > 255 for part in path.split("/"))
        or any(ord(char) < 32 for char in path)
    ):
        raise ValueError("sandbox_file_path")
    return path


def exchange(operation: str, path: str, data: str | None = None) -> dict:
    """Open every component relative to directory FDs, never follow a symlink."""
    path = relative_path(path)
    parts = path.split("/")
    # Mandatory flags: unsupported platforms fail closed rather than follow links.
    no_follow = os.O_NOFOLLOW
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | no_follow
    directory = os.open(ROOT, directory_flags)
    try:
        for part in parts[:-1]:
            if operation == "write":
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        if operation == "read":
            fd = os.open(parts[-1], os.O_RDONLY | no_follow | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_FILE_BYTES
                ):
                    raise ValueError("sandbox_file_type_or_size")
                body = stream.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise ValueError("sandbox_file_too_large")
            return {"path": path, "size": len(body), "data": base64.b64encode(body).decode("ascii")}
        if operation != "write" or not isinstance(data, str) or len(data) > MAX_FILE_JSON - 4096:
            raise ValueError("sandbox_file_request")
        body = base64.b64decode(data, validate=True)
        if not body or len(body) > MAX_FILE_BYTES:
            raise ValueError("sandbox_file_too_large")
        # Do not replace existing files, links, or devices. Publishing via link is atomic
        # and fails if a command raced to create the destination.
        temporary = ".upload-" + uuid4().hex
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
            os.link(
                temporary,
                parts[-1],
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=directory)
        return {"path": path, "size": len(body)}
    finally:
        os.close(directory)

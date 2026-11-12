"""Bounded, credential-free deployment metadata readable by the Dim identity."""

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

FIELDS = frozenset(
    {
        "ts",
        "stage",
        "trace_id",
        "revision",
        "previous",
        "current",
        "error_type",
        "phase",
        "elapsed_ms",
        "model",
        "call",
        "prompt_chars",
        "commit_count",
        "file_count",
    }
)


def record(event: dict) -> None:
    directory = os.environ.get("FULLISH_DIAGNOSTICS_DIR")
    if not directory or not event.get("stage", "").startswith(("deployment.report.", "service.")):
        return
    temporary = None
    try:
        root = Path(directory)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            return
        path = root / "events.json"
        events = []
        if path.exists():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_size > 65536
            ):
                return
            events = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(events, list):
                return
        safe = {
            key: value[:200] if isinstance(value, str) else value
            for key, value in event.items()
            if key in FIELDS and isinstance(value, (str, int, float, bool))
        }
        body = json.dumps([*events[-39:], safe], ensure_ascii=False).encode()
        if len(body) > 65536:
            body = json.dumps([safe], ensure_ascii=False).encode()
        temporary = root / (".events-" + uuid4().hex)
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write(body)
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)

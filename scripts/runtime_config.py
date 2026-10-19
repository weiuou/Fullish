"""Validate narrowly scoped QQ credentials and update a raw Compose env file."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path


def validate(data: object) -> dict[str, str]:
    if data == {}:
        return {}
    if not isinstance(data, dict) or set(data) != {"QQ_APP_ID", "QQ_APP_SECRET"}:
        raise ValueError("Expected both QQ credentials and no other settings")
    app_id = data["QQ_APP_ID"]
    secret = data["QQ_APP_SECRET"]
    if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]{1,32}", app_id):
        raise ValueError("QQ_APP_ID must contain digits")
    if (
        not isinstance(secret, str)
        or not 1 <= len(secret) <= 512
        or secret != secret.strip()
        or any(ord(c) < 32 or ord(c) > 126 for c in secret)
    ):
        raise ValueError("QQ_APP_SECRET must be a nonempty single-line ASCII value")
    return {"QQ_APP_ID": app_id, "QQ_APP_SECRET": secret}


def update(path: Path, values: dict[str, str]) -> None:
    if not values:
        return
    values = {**values, "QQ_ENABLED": "true"}
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.partition("=")[0] not in values
    ]
    content = "\n".join([*lines, *(key + "=" + value for key, value in values.items())]) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError("QQ configuration is too large")
        values = validate(json.loads(raw))
        if sys.argv[1:] == ["validate"]:
            if values:
                print(json.dumps(values))
        elif len(sys.argv) == 3 and sys.argv[1] == "update":
            update(Path(sys.argv[2]), values)
        else:
            raise ValueError("Invalid configuration command")
        return 0
    except (ValueError, OSError):
        # JSON parse errors can contain fragments of credentials; never print them.
        print("Invalid QQ runtime configuration; no credentials were logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

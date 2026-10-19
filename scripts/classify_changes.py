"""Choose lightweight docs checks or the full delivery path from a Git diff."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


def is_documentation(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    if not parts:
        return False
    if parts[0] == "docs":
        return True
    suffix = PurePosixPath(path).suffix.lower()
    if len(parts) == 1 and suffix in {".md", ".mdx", ".markdown", ".rst"}:
        return True
    if len(parts) >= 3 and parts[:2] in {
        (".github", "issue_template"),
        (".github", "pull_request_template"),
    }:
        return True
    return len(parts) == 2 and parts[0] == ".github" and suffix == ".md"


def changed_paths(event_name: str, event: dict, head: str) -> list[str] | None:
    """None means full CI: manual request or an unavailable comparison base."""
    if event_name == "workflow_dispatch":
        return None
    if event_name == "pull_request":
        pull = event.get("pull_request", {})
        base = pull.get("base", {}).get("sha", "")
        head = pull.get("head", {}).get("sha", "")
        comparison = "..."
    elif event_name == "push":
        base = event.get("before", "")
        comparison = ".."
    else:
        return None
    if not all(re.fullmatch(r"[a-fA-F0-9]{40,64}", sha) for sha in (base, head)):
        return None
    if set(base) == {"0"}:
        return None
    try:
        # --no-renames includes both names, so moving code into docs is still code.
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}{comparison}{head}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def needs_runtime(paths: list[str] | None) -> bool:
    return paths is None or any(not is_documentation(path) for path in paths)


def deployment_is_current(build: str, head: str) -> bool:
    paths = changed_paths("push", {"before": build}, head)
    if paths is None:
        raise ValueError(
            "Cannot compare the built revision with current main; retry after checking Git history."
        )
    return not needs_runtime(paths)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--deployment-current":
        apply = deployment_is_current(sys.argv[2], sys.argv[3])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"apply={str(apply).lower()}\n")
        print(
            "Built code is current." if apply else "Newer runtime changes exist; skip this image."
        )
        return
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text(encoding="utf-8"))
    paths = changed_paths(os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_SHA"])
    runtime = needs_runtime(paths)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"runtime={str(runtime).lower()}\n")
    description = (
        "Code/configuration change or explicit manual run: full checks and delivery."
        if runtime
        else "Documentation-only change: skip container builds, artifacts, packages and deployment."
    )
    print(description)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(description + "\n")


if __name__ == "__main__":
    main()

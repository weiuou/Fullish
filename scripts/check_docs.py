"""Check local Markdown links and PNG assets without installing dependencies."""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check_document(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```\s*$", "", text)
    failures = []
    # Looking for ]( also handles the nested image links used by README badges.
    for match in re.finditer(r"\]\(([^)\n]+)\)", text):
        target = match[1].strip()
        target = target[1 : target.find(">")] if target.startswith("<") else target.split()[0]
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        relative = unquote(url.path)
        destination = (
            root / relative.lstrip("/") if relative.startswith("/") else path.parent / relative
        ).resolve()
        if not destination.is_relative_to(root) or not destination.exists():
            failures.append(f"{path.relative_to(root)}: missing local target {target}")
            continue
        if destination.suffix.lower() == ".png":
            with destination.open("rb") as asset:
                header = asset.read(24)
            if (
                len(header) != 24
                or header[:8] != b"\x89PNG\r\n\x1a\n"
                or header[12:16] != b"IHDR"
                or 0 in struct.unpack(">II", header[16:24])
            ):
                failures.append(f"{path.relative_to(root)}: invalid PNG asset {target}")
    return failures


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    documents = set(root.glob("*.md"))
    documents.update(root.glob("docs/**/*.md"))
    documents.update(root.glob(".github/**/*.md"))
    failures = [failure for path in sorted(documents) for failure in check_document(path, root)]
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Checked {len(documents)} documents: local links and PNG assets are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

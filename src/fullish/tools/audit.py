"""Bounded, private snapshots of the web results actually returned to the model."""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_SNAPSHOT_BYTES = 96_000
MAX_RECORDS = 1_000
RETENTION_SECONDS = 7 * 24 * 60 * 60
PRIVATE_KEY = re.compile(
    r"(?i)(?:token|secret|password|authorization|api[_-]?key|signature|credential|^key$|^sig$|^auth$)"
)
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def default_path() -> Path:
    return Path(os.environ.get("TOOL_AUDIT_PATH", ".state/tool-audit.sqlite3"))


def safe_url(match: re.Match[str]) -> str:
    try:
        url = urlsplit(match[0])
        host = url.netloc.rsplit("@", 1)[-1]
        query = urlencode(
            [
                (key, "[REDACTED]" if PRIVATE_KEY.search(key) else value)
                for key, value in parse_qsl(url.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((url.scheme, host, url.path, query, ""))
    except ValueError:
        return "[INVALID URL]"


def redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if PRIVATE_KEY.search(str(key)) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets) - {""}, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return URL.sub(safe_url, value)
    return value


class ToolAudit:
    def __init__(
        self,
        path: Path | None = None,
        *,
        secrets: tuple[str, ...] = (),
        max_records: int = MAX_RECORDS,
        retention_seconds: int = RETENTION_SECONDS,
    ):
        self.path = path if path is not None else default_path()
        self.secrets = secrets
        self.max_records = max_records
        self.retention_seconds = retention_seconds

    def save(self, record: dict) -> None:
        """One transaction per call, with separate connections for concurrent writers."""
        payload = record["snapshot"]
        safe = redact(payload, self.secrets)
        serialized = json.dumps(safe, ensure_ascii=False)
        redacted = safe != payload
        truncated = len(serialized.encode("utf-8")) > MAX_SNAPSHOT_BYTES
        if truncated:
            # Keep valid JSON and an explicit marker instead of silently cutting bytes.
            serialized = json.dumps(
                {"preview": serialized[:10_000], "truncated": True}, ensure_ascii=False
            )
        metadata = {key: value for key, value in record.items() if key != "snapshot"}
        metadata = {
            **metadata,
            "schema_version": 1,
            "snapshot_redacted": redacted,
            "snapshot_truncated": truncated,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create with private permissions before SQLite writes any contents.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                "id TEXT PRIMARY KEY, created REAL NOT NULL, trace_id TEXT NOT NULL, "
                "metadata TEXT NOT NULL, snapshot TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS calls_trace ON calls(trace_id, created)")
            db.execute("CREATE INDEX IF NOT EXISTS calls_created ON calls(created)")
            db.execute(
                "DELETE FROM calls WHERE created < ?", (time.time() - self.retention_seconds,)
            )
            db.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
                (
                    record["audit_id"],
                    time.time(),
                    record["trace_id"],
                    json.dumps(metadata),
                    serialized,
                ),
            )
            db.execute(
                "DELETE FROM calls WHERE id IN "
                "(SELECT id FROM calls ORDER BY created DESC, id DESC LIMIT -1 OFFSET ?)",
                (self.max_records,),
            )

    def read(
        self, *, trace_id: str | None = None, audit_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid audit limit")
        clauses, args = [], []
        if trace_id:
            clauses.append("trace_id = ?")
            args.append(trace_id)
        if audit_id:
            clauses.append("id = ?")
            args.append(audit_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Inspection never creates a missing DB or changes an existing one.
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT metadata, snapshot FROM calls" + where + " ORDER BY created DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
        return [{**json.loads(meta), "snapshot": json.loads(snapshot)} for meta, snapshot in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read private web-tool snapshots as JSON lines.")
    parser.add_argument("--path", type=Path, default=default_path())
    parser.add_argument("--trace-id")
    parser.add_argument("--audit-id")
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1..100")
    options = parser.parse_args()
    try:
        rows = ToolAudit(options.path).read(
            trace_id=options.trace_id, audit_id=options.audit_id, limit=options.limit
        )
    except (OSError, sqlite3.Error, ValueError):
        print("Tool audit unavailable; check its path, permissions and storage.", file=sys.stderr)
        return 1
    for row in reversed(rows):
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

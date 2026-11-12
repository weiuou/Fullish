"""Durable event claims and uncertain execution/delivery state, without chat payloads."""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fullish.agents.rpc import DimError
from fullish.tools.runtime import ToolContext


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    event_key: str
    session_id: str
    state: str
    delivery: str


class RunStore:
    def __init__(self, path: Path, *, bot_id: str):
        self.path = path
        self.bot_id = bot_id
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    event_key TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                    delivery TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def event_key(self, context: ToolContext) -> str:
        identity = ["qq", self.bot_id, context.group_id, context.sender_id, context.message_id]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _run(row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            *(row[key] for key in ("run_id", "event_key", "session_id", "state", "delivery"))
        )

    def get(self, context: ToolContext) -> AgentRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE event_key=?", (self.event_key(context),)
            ).fetchone()
        return self._run(row) if row else None

    def claim(self, context: ToolContext) -> AgentRun | None:
        now = time.time()
        run_id = uuid.uuid4().hex
        key = self.event_key(context)
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO agent_runs(event_key,run_id,state,created,updated) "
                "VALUES(?,?,'creating',?,?)",
                (key, run_id, now, now),
            ).rowcount
        return AgentRun(run_id, key, "", "creating", "") if inserted else None

    def update(self, run_id: str, *, state: str, session_id: str | None = None) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE agent_runs SET state=?,session_id=COALESCE(?,session_id),updated=? "
                "WHERE run_id=?",
                (state, session_id, time.time(), run_id),
            ).rowcount
            if changed != 1:
                raise DimError("dim_run_missing")

    def delivery(self, run_id: str, state: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE agent_runs SET delivery=?,updated=? WHERE run_id=?",
                (state, time.time(), run_id),
            ).rowcount
            if changed != 1:
                raise DimError("dim_run_missing")

    def recover(self) -> int:
        """Only at exclusive application startup; never resubmit interrupted work."""
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE agent_runs SET state='unknown',updated=? "
                "WHERE state IN ('creating','created','submitting','running')",
                (time.time(),),
            ).rowcount
            connection.execute(
                "UPDATE agent_runs SET delivery='unknown',updated=? WHERE delivery='sending'",
                (time.time(),),
            )
        return changed

    def prune(self, *, now: float | None = None, days: int = 7) -> int:
        """Unresolved executions or sends never expire as successful tombstones."""
        cutoff = (time.time() if now is None else now) - days * 86400
        with self._connect() as connection:
            return connection.execute(
                "DELETE FROM agent_runs WHERE updated<? "
                "AND state IN ('completed','failed','cancelled') "
                "AND delivery IN ('','confirmed','failed','blocked')",
                (cutoff,),
            ).rowcount

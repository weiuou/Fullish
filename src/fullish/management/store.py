"""Developer/group metadata storage and the existing policy-file bootstrap."""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fullish.policies.groups import GROUP_ID, GroupPolicies, PolicyError, legacy_policy


class ControlStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.path = directory / "control.sqlite3"
        fresh = not self.path.exists()
        self.policies = GroupPolicies(directory / "policy.json")
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS developers(user_id TEXT PRIMARY KEY, selected INTEGER);
                CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT UNIQUE NOT NULL, label TEXT NOT NULL DEFAULT '',
                    seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing(id INTEGER PRIMARY KEY CHECK(id=1),
                    digest TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS received(user_id TEXT, message_id TEXT, created REAL,
                    PRIMARY KEY(user_id,message_id));
                CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_ref TEXT, group_ref TEXT, feature TEXT, enabled INTEGER, created REAL);
            """)
            if not self.policies.path.exists():
                if not fresh:
                    raise PolicyError("group_policy_unavailable")
                self.policies.write(legacy_policy())
            for group in self.policies.read()["groups"]:
                db.execute("INSERT OR IGNORE INTO groups(group_id,seen) VALUES(?,?)", (group, 0))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def observe(self, group: str) -> None:
        if not GROUP_ID.fullmatch(group):
            return
        with self.connection() as db:
            db.execute(
                """INSERT INTO groups(group_id,seen) VALUES(?,?)
                ON CONFLICT(group_id) DO UPDATE SET seen=excluded.seen""",
                (group, time.time()),
            )

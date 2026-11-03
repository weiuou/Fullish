"""One-use pairing, authenticated developer lookup and command replay protection."""

import hashlib
import hmac
import secrets
import sqlite3
import time

from fullish.management.store import ControlStore


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class DeveloperIdentity:
    def __init__(self, store: ControlStore):
        self.store = store

    def pair(self) -> str:
        code = secrets.token_urlsafe(24)
        with self.store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO pairing VALUES(1,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), time.time() + 600),
            )
        return code

    def claim(self, db: sqlite3.Connection, user: str, message_id: str) -> bool:
        if db.execute(
            "SELECT 1 FROM received WHERE user_id=? AND message_id=?", (user, message_id)
        ).fetchone():
            return False
        db.execute("INSERT INTO received VALUES(?,?,?)", (user, message_id, time.time()))
        db.execute(
            "DELETE FROM received WHERE rowid NOT IN "
            "(SELECT rowid FROM received ORDER BY created DESC LIMIT 2000)"
        )
        return True

    def developer(self, db: sqlite3.Connection, user: str) -> sqlite3.Row | None:
        return db.execute("SELECT * FROM developers WHERE user_id=?", (user,)).fetchone()

    def bind(self, db: sqlite3.Connection, user: str, code: str) -> bool:
        row = db.execute("SELECT * FROM pairing WHERE id=1").fetchone()
        if (
            not row
            or row["expires"] <= time.time()
            or not hmac.compare_digest(row["digest"], hashlib.sha256(code.encode()).hexdigest())
        ):
            return False
        db.execute("DELETE FROM pairing WHERE id=1")
        db.execute("INSERT OR IGNORE INTO developers VALUES(?,NULL)", (user,))
        return True

    def developers(self) -> list[str]:
        with self.store.connection() as db:
            return [row["user_id"] for row in db.execute("SELECT user_id FROM developers")]

    def revoke(self, user_ref: str) -> None:
        with self.store.connection() as db:
            for row in db.execute("SELECT user_id FROM developers").fetchall():
                if fingerprint(row["user_id"]) == user_ref:
                    db.execute("DELETE FROM developers WHERE user_id=?", (row["user_id"],))

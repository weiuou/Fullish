"""Developer identity, one-use pairing and deterministic private management menu."""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fullish.group_policy import GROUP_ID, GroupPolicies, PolicyError, legacy_policy

NAMES = {"聊天": "chat", "搜索": "search", "生图": "image", "沙箱": "sandbox"}
MENU = (
    "开发者菜单\n群列表：查看群编号\n选择 1：管理指定群\n状态：查看当前群\n"
    "开启 搜索 / 关闭 生图：切换功能\n默认 开启 搜索：设置新群默认功能\n"
    "命名 我的群：给当前群加备注\n菜单：返回这里"
)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class GroupControl:
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

    def pair(self) -> str:
        code = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO pairing VALUES(1,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), time.time() + 600),
            )
        return code

    def command(self, user: str, message_id: str, text: str) -> str | None:
        if not user or len(user) > 256 or not message_id or len(message_id) > 256:
            return None
        if len(text) > 200:
            return None
        parts = text.strip().removeprefix("/").split()
        with self.connection() as db:
            if db.execute(
                "SELECT 1 FROM received WHERE user_id=? AND message_id=?", (user, message_id)
            ).fetchone():
                return None
            db.execute("INSERT INTO received VALUES(?,?,?)", (user, message_id, time.time()))
            db.execute(
                "DELETE FROM received WHERE rowid NOT IN "
                "(SELECT rowid FROM received ORDER BY created DESC LIMIT 2000)"
            )
            developer = db.execute("SELECT * FROM developers WHERE user_id=?", (user,)).fetchone()
            if len(parts) == 2 and parts[0] in {"绑定", "绑定开发者"}:
                row = db.execute("SELECT * FROM pairing WHERE id=1").fetchone()
                if (
                    row
                    and row["expires"] > time.time()
                    and hmac.compare_digest(
                        row["digest"], hashlib.sha256(parts[1].encode()).hexdigest()
                    )
                ):
                    db.execute("DELETE FROM pairing WHERE id=1")
                    db.execute("INSERT OR IGNORE INTO developers VALUES(?,NULL)", (user,))
                    return "开发者绑定成功。\n" + MENU
                return "绑定码无效或已过期。请在服务器重新生成一次性绑定码。"
            if not developer:
                return "请先使用服务器生成的一次性绑定码绑定开发者。" if parts == ["菜单"] else None
            try:
                return self._command(db, user, developer["selected"], parts)
            except PolicyError as error:
                if str(error) == "sandbox_group_limit":
                    return "最多同时为两个群开启沙箱，请先关闭另一个群的沙箱。"
                if str(error) == "sandbox_requires_group":
                    return "沙箱需要选定群后单独开启。"
                return "群配置不可用，请检查服务器配置。"

    def _command(self, db, user: str, selected: int | None, parts: list[str]) -> str:
        if not parts or parts in [["菜单"], ["帮助"], ["返回"]]:
            return MENU
        if parts[0] == "群列表":
            page = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 1
            page = max(1, min(page, 200))
            rows = db.execute(
                "SELECT * FROM groups ORDER BY id LIMIT 10 OFFSET ?", ((page - 1) * 10,)
            ).fetchall()
            if not rows:
                return "还没有记录到群，请先在新群 @Fullish，再发送群列表。"
            return (
                "群列表（编号固定）\n"
                + "\n".join(self._status(row) for row in rows)
                + f"\n发送：选择 {rows[0]['id']}\n下一页：群列表 {page + 1}"
            )
        if len(parts) == 2 and parts[0] == "选择" and parts[1].isdigit():
            row = db.execute("SELECT * FROM groups WHERE id=?", (int(parts[1]),)).fetchone()
            if not row:
                return "群编号不存在，请发送群列表。"
            db.execute("UPDATE developers SET selected=? WHERE user_id=?", (row["id"], user))
            return self._status(row) + "\n发送：开启 搜索 / 关闭 生图 / 开启 沙箱 / 关闭 聊天"
        default = parts[0] == "默认"
        operation = parts[1:] if default else parts
        row = (
            db.execute("SELECT * FROM groups WHERE id=?", (selected,)).fetchone()
            if selected
            else None
        )
        if not default and not row:
            return "请先发送群列表，再发送：选择 群编号。"
        if (
            len(operation) == 2
            and operation[0] in {"开启", "关闭", "开", "关"}
            and operation[1] in NAMES
        ):
            feature = NAMES[operation[1]]
            enabled = operation[0] in {"开启", "开"}
            configured = {
                "chat": bool(os.environ.get("AI_BASE_URL")),
                "search": bool(os.environ.get("DEEPSEEK_API_KEY")),
                "image": os.environ.get("AI_IMAGE_MODEL", "").lower() != "off",
                "sandbox": bool(
                    os.environ.get("SANDBOX_SOCKET") and os.environ.get("SANDBOX_TOKEN")
                ),
            }
            if enabled and not configured[feature]:
                return f"{operation[1]}服务尚未配置，暂时无法开启。"
            group = row["group_id"] if row and not default else None
            self.policies.change(group, feature, enabled)
            db.execute(
                "INSERT INTO changes(actor_ref,group_ref,feature,enabled,created) "
                "VALUES(?,?,?,?,?)",
                (
                    fingerprint(user),
                    fingerprint(group) if group else "defaults",
                    feature,
                    enabled,
                    time.time(),
                ),
            )
            db.execute(
                "DELETE FROM changes WHERE id NOT IN "
                "(SELECT id FROM changes ORDER BY id DESC LIMIT 2000)"
            )
            return f"已{operation[0]}{operation[1]}，后续消息和调用生效。\n" + (
                self._status(row) if row else "新群默认配置已更新；已有群的单独设置优先生效。"
            )
        if not default and parts[0] == "命名" and len(parts) >= 2:
            label = " ".join(parts[1:])[:60]
            db.execute("UPDATE groups SET label=? WHERE id=?", (label, selected))
            return "群备注已保存。"
        if not default and parts == ["状态"]:
            return self._status(row)
        return MENU

    def _status(self, row) -> str:
        self.policies.read()
        features = self.policies.features(row["group_id"])
        flags = " ".join(
            name + ("开" if features.allows(key) else "关") for name, key in NAMES.items()
        )
        label = row["label"] or row["group_id"][:8] + "…"
        return f"{row['id']}. {label}：{flags}"

    @classmethod
    def from_env(cls) -> "GroupControl | None":
        directory = os.environ.get("GROUP_CONTROL_DIR")
        return cls(Path(directory)) if directory else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pair", "developers", "revoke"])
    parser.add_argument("--user-ref")
    args = parser.parse_args()
    control = GroupControl.from_env()
    if not control:
        raise SystemExit("GROUP_CONTROL_DIR is not configured")
    if args.command == "pair":
        print("私聊机器人发送（10 分钟有效，仅使用一次）：\n绑定开发者 " + control.pair())
    else:
        with control.connection() as db:
            rows = db.execute("SELECT user_id FROM developers").fetchall()
            if args.command == "revoke":
                if not args.user_ref:
                    raise SystemExit("--user-ref is required")
                for row in rows:
                    if fingerprint(row["user_id"]) == args.user_ref:
                        db.execute("DELETE FROM developers WHERE user_id=?", (row["user_id"],))
            else:
                print(json.dumps([fingerprint(row["user_id"]) for row in rows]))


if __name__ == "__main__":
    main()

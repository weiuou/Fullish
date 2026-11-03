"""Group selection, labels and feature configuration management."""

import os
import time

from fullish.management.identity import fingerprint
from fullish.management.menu import MENU, NAMES
from fullish.policies.groups import GroupPolicies


class GroupManagement:
    def __init__(self, policies: GroupPolicies):
        self.policies = policies

    def handle(self, db, user: str, selected: int | None, parts: list[str]) -> str:
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

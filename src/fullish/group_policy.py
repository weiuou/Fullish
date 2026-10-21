"""Shared, hot-reloaded group permissions. Standard library only for the broker."""

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

FEATURES = ("chat", "search", "image", "sandbox")
GROUP_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Features:
    chat: bool = False
    search: bool = False
    image: bool = False
    sandbox: bool = False

    def allows(self, name: str) -> bool:
        return self.chat and name in FEATURES and bool(getattr(self, name))


class GroupPolicies:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def validate(data: object) -> dict:
        if not isinstance(data, dict) or set(data) != {"version", "defaults", "groups"}:
            raise PolicyError("invalid_group_policy")
        defaults, groups = data["defaults"], data["groups"]
        if data["version"] != 1 or not isinstance(defaults, dict) or set(defaults) != set(FEATURES):
            raise PolicyError("invalid_group_policy")
        if not isinstance(groups, dict) or len(groups) > 2000:
            raise PolicyError("invalid_group_policy")
        for row in [defaults, *groups.values()]:
            if not isinstance(row, dict) or set(row) - set(FEATURES):
                raise PolicyError("invalid_group_policy")
            if any(type(value) is not bool for value in row.values()):
                raise PolicyError("invalid_group_policy")
        if defaults["sandbox"] or any(
            not isinstance(group, str) or not GROUP_ID.fullmatch(group) for group in groups
        ):
            raise PolicyError("invalid_group_policy")
        if sum(row.get("sandbox", False) for row in groups.values()) > 2:
            raise PolicyError("sandbox_group_limit")
        return data

    def read(self) -> dict:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise PolicyError("invalid_group_policy")
            return self.validate(json.loads(raw))
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, PolicyError):
                raise
            raise PolicyError("group_policy_unavailable") from None

    def features(self, group: str) -> Features:
        try:
            data = self.read()
            return Features(**{**data["defaults"], **data["groups"].get(group, {})})
        except PolicyError:
            return Features()  # A missing or damaged configured policy never grants access.

    def sandbox_groups(self) -> frozenset[str]:
        try:
            data = self.read()
            return frozenset(
                group
                for group, overrides in data["groups"].items()
                if Features(**{**data["defaults"], **overrides}).allows("sandbox")
            )
        except PolicyError:
            return frozenset()

    def write(self, data: dict) -> None:
        self.validate(data)
        raw = json.dumps(data, ensure_ascii=True, sort_keys=True) + "\n"
        if len(raw.encode()) > 1024 * 1024:
            raise PolicyError("invalid_group_policy")
        fd, temporary = tempfile.mkstemp(prefix=".policy-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def change(self, group: str | None, feature: str, enabled: bool) -> None:
        # Caller holds the control database writer transaction across this update.
        if feature not in FEATURES or type(enabled) is not bool:
            raise PolicyError("invalid_group_policy")
        if group is not None and not GROUP_ID.fullmatch(group):
            raise PolicyError("invalid_group_policy")
        if group is None and feature == "sandbox":
            raise PolicyError("sandbox_requires_group")
        data = self.read()
        target = data["defaults"] if group is None else data["groups"].setdefault(group, {})
        target[feature] = enabled
        self.write(data)

    @classmethod
    def from_env(cls) -> "GroupPolicies | None":
        if path := os.environ.get("GROUP_POLICY_PATH"):
            return cls(Path(path))
        if directory := os.environ.get("GROUP_CONTROL_DIR"):
            return cls(Path(directory) / "policy.json")
        return None


def legacy_policy() -> dict:
    """Import existing access once. Later env edits cannot overwrite menu choices."""
    qq = {x.strip() for x in os.environ.get("QQ_ALLOWED_GROUPS", "").split(",") if x.strip()}
    tools = {x.strip() for x in os.environ.get("TOOLS_ALLOWED_GROUPS", "").split(",") if x.strip()}
    image = (
        bool(os.environ.get("AI_BASE_URL"))
        and os.environ.get("AI_IMAGE_MODEL", "").lower() != "off"
    )
    defaults = Features(chat=not qq, image=image and not qq)
    groups = {}
    for group in qq | tools:
        chat = not qq or group in qq
        groups[group] = asdict(
            Features(
                chat=chat,
                search=bool(os.environ.get("DEEPSEEK_API_KEY")) and group in tools,
                image=image and chat,
                sandbox=bool(os.environ.get("SANDBOX_SOCKET")) and group in tools,
            )
        )
    return {"version": 1, "defaults": asdict(defaults), "groups": groups}

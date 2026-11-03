"""One interpretation of current group policy, reused at execution and delivery boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fullish.policies.groups import GroupPolicies
from fullish.sending import Reply, ReplyPlan
from fullish.sending.schema import reply_messages

if TYPE_CHECKING:
    from fullish.tools.runtime import ToolContext


class AccessPolicy:
    def __init__(
        self,
        *,
        policies: GroupPolicies | None = None,
        allowed_groups: frozenset[str] = frozenset(),
        notification_targets: dict[str, frozenset[str]] | None = None,
    ):
        self.policies = policies
        self.allowed_groups = allowed_groups
        self.notification_targets = notification_targets or {}

    def group_allowed(self, group_id: str) -> bool:
        if self.policies:
            return self.policies.features(group_id).chat
        return not self.allowed_groups or group_id in self.allowed_groups

    def feature_allowed(self, group_id: str, feature: str, *, legacy: bool = True) -> bool:
        if self.policies:
            return self.policies.features(group_id).allows(feature)
        return legacy

    def image_allowed(self, context: ToolContext | None) -> bool:
        return not self.policies or bool(
            context and self.feature_allowed(context.group_id, "image")
        )

    def reply_allowed(self, group_id: str, reply: Reply | ReplyPlan) -> bool:
        return self.group_allowed(group_id) and (
            not any(part.kind == "image" for item in reply_messages(reply) for part in item.parts)
            or self.feature_allowed(group_id, "image")
        )

    def delivery_allowed(self, group_id: str, reply: Reply | ReplyPlan, *, source: str) -> bool:
        if source == "chat":
            return self.reply_allowed(group_id, reply)
        # Existing deployment configuration is an explicit, independent text notification grant.
        return group_id in self.notification_targets.get(source, frozenset()) and (
            not any(part.kind == "image" for item in reply_messages(reply) for part in item.parts)
            or self.feature_allowed(group_id, "image", legacy=False)
        )

    def interaction_allowed(self, context: ToolContext | None, *, legacy: bool = True) -> bool:
        return bool(
            context
            and context.sender_id
            and context.message_id
            and self.feature_allowed(context.group_id, "chat", legacy=legacy)
        )

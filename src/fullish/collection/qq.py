"""Translate QQ events into normalized records without model calls or downloads."""

from collections.abc import Iterator, Mapping

from nonebot.adapters.qq import Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.models.qq import GroupMentionUser

from fullish.collection.parsers import parse_segments
from fullish.collection.schema import CollectedMessage, Sender


def describe(message: Message) -> tuple[str, list[str]]:
    content = parse_segments((segment.type, segment.data) for segment in message)
    return content.text, content.image_urls


def collect_group_message(event: GroupMessageCreateEvent) -> CollectedMessage:
    attachments = {item.url: item for item in event.attachments or () if item.url}
    mentions = {
        item.id: item for item in event.mentions or () if isinstance(item, GroupMentionUser)
    }

    def segments() -> Iterator[tuple[str, Mapping[str, object]]]:
        has_everyone = False
        for segment in event.get_message():
            has_everyone |= segment.type in {"mention_everyone", "group_mention_everyone"}
            data: dict[str, object] = dict(segment.data)
            if segment.type in {"image", "audio", "video", "file"}:
                url = data.get("url")
                if isinstance(url, str) and (attachment := attachments.get(url)):
                    data.update(attachment.model_dump(exclude_none=True))
            elif segment.type == "mention_user":
                target = data.get("user_id")
                if isinstance(target, str) and (mention := mentions.get(target)):
                    data["member_openid"] = mention.member_openid
            yield segment.type, data
        # The adapter removes @everyone markup from text; preserve its event metadata.
        if not has_everyone and any(item.scope == "all" for item in event.mentions or ()):
            yield "mention_everyone", {}
        for attachment in event.attachments or ():
            if not attachment.url:
                kind = attachment.content_type.split("/", 1)[0]
                yield (
                    kind if kind in {"image", "audio", "video"} else "file",
                    attachment.model_dump(exclude_none=True),
                )

    return CollectedMessage(
        group_id=event.group_openid,
        message_id=event.id,
        sender=Sender(event.author.member_openid, event.author.username or None),
        content=parse_segments(segments()),
        addressed=event.is_tome(),
        sent_at=event.timestamp,
    )

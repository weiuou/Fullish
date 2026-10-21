"""Translate QQ events into normalized records without model calls or downloads."""

from collections.abc import Iterator, Mapping

from nonebot.adapters.qq import Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.models.qq import Attachment, GroupMentionUser

from fullish.collection.parsers import parse_segments
from fullish.collection.schema import CollectedMessage, ParsedContent, QuotedMessage, Sender


def describe(message: Message) -> tuple[str, list[str]]:
    content = parse_segments((segment.type, segment.data) for segment in message)
    return content.text, content.image_urls


def _content(
    message: Message,
    attachments: list[Attachment] | None,
    mentions: list[GroupMentionUser] | None = None,
    *,
    everyone: bool = False,
) -> ParsedContent:
    by_url = {item.url: item for item in attachments or () if item.url}
    by_id = {item.id: item for item in mentions or ()}

    def segments() -> Iterator[tuple[str, Mapping[str, object]]]:
        has_everyone = False
        for segment in message:
            has_everyone |= segment.type in {"mention_everyone", "group_mention_everyone"}
            data: dict[str, object] = dict(segment.data)
            if segment.type in {"image", "audio", "video", "file"}:
                url = data.get("url")
                if isinstance(url, str) and (attachment := by_url.get(url)):
                    data.update(attachment.model_dump(exclude_none=True))
            elif segment.type == "mention_user":
                target = data.get("user_id")
                if isinstance(target, str) and (mention := by_id.get(target)):
                    data["member_openid"] = mention.member_openid
            yield segment.type, data
        # The adapter removes @everyone markup from text; preserve its event metadata.
        if not has_everyone and everyone:
            yield "mention_everyone", {}
        for attachment in attachments or ():
            if not attachment.url:
                kind = attachment.content_type.split("/", 1)[0]
                yield (
                    kind if kind in {"image", "audio", "video"} else "file",
                    attachment.model_dump(exclude_none=True),
                )

    return parse_segments(segments())


def collect_group_message(event: GroupMessageCreateEvent) -> CollectedMessage:
    reply = None
    if source := event.reply:
        author = source.author
        reply = QuotedMessage(
            content=_content(Message.from_qq_message(source), source.attachments),
            reference_id=source.msg_idx,
            sender_id=(author.member_openid or author.user_openid or author.id) if author else None,
            sender_name=author.username if author else None,
        )
    return CollectedMessage(
        group_id=event.group_openid,
        message_id=event.id,
        sender=Sender(event.author.member_openid, event.author.username or None),
        content=_content(
            event.get_message(),
            event.attachments,
            [item for item in event.mentions or () if isinstance(item, GroupMentionUser)],
            everyone=any(item.scope == "all" for item in event.mentions or ()),
        ),
        addressed=event.is_tome(),
        sent_at=event.timestamp,
        reply=reply,
    )

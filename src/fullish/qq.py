"""QQ official WebSocket transport. Private events stop before message dispatch."""

from nonebot import logger, on_message
from nonebot.adapters.qq import Adapter, Bot, Message
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.models import Dispatch

from fullish.conversation import Conversations
from fullish.model import ModelError


class GroupOnlyAdapter(Adapter):
    def dispatch_event(self, bot: Bot, payload: Dispatch) -> None:
        if payload.type in {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            return
        super().dispatch_event(bot, payload)


def describe(message: Message) -> tuple[str, list[str]]:
    """Flatten a message into context text plus the image URLs it carries."""
    parts: list[str] = []
    images: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(segment.data["text"])
        elif segment.type == "emoji":
            parts.append("[表情]")
        elif segment.type == "mention_user":
            name = segment.data.get("username")
            parts.append(f"@{name}" if name else "@某人")
        elif segment.type == "image":
            parts.append("[图片]")
            if url := segment.data.get("url"):
                images.append(url)
    return "".join(parts).strip(), images


def install_group_handler(conversations: Conversations) -> None:
    async def is_group(event: GroupMessageCreateEvent) -> bool:
        return True

    matcher = on_message(rule=is_group, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageCreateEvent) -> None:
        try:
            text, images = describe(event.get_message())
            reply = await conversations.receive(
                event.group_openid,
                event.id,
                event.author.member_openid,
                text,
                addressed=event.is_tome(),
                images=images,
            )
            if reply:
                await bot.send(event, reply)
                conversations.sent(event.group_openid, reply)
        except ModelError as error:
            logger.warning("AI request failed: {}", str(error))
        except Exception as error:
            # No message payloads, API response bodies or credentials in our logs.
            logger.warning("QQ reply failed: {}", type(error).__name__)

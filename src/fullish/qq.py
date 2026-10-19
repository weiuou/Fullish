"""QQ official WebSocket transport. Private events stop before message dispatch."""

from nonebot import logger, on_message
from nonebot.adapters.qq import Adapter, Bot
from nonebot.adapters.qq.event import GroupMessageCreateEvent
from nonebot.adapters.qq.models import Dispatch

from fullish.conversation import Conversations
from fullish.model import ModelError


class GroupOnlyAdapter(Adapter):
    def dispatch_event(self, bot: Bot, payload: Dispatch) -> None:
        if payload.type in {"C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}:
            return
        super().dispatch_event(bot, payload)


def install_group_handler(conversations: Conversations) -> None:
    async def is_group(event: GroupMessageCreateEvent) -> bool:
        return True

    matcher = on_message(rule=is_group, priority=10, block=True)

    @matcher.handle()
    async def handle(bot: Bot, event: GroupMessageCreateEvent) -> None:
        try:
            text = await conversations.receive(
                event.group_openid,
                event.id,
                event.author.member_openid,
                event.get_plaintext(),
                addressed=event.is_tome(),
            )
            if text:
                await bot.send(event, text)
                conversations.sent(event.group_openid, text)
        except ModelError as error:
            logger.warning("AI request failed: {}", str(error))
        except Exception as error:
            # No message payloads, API response bodies or credentials in our logs.
            logger.warning("QQ reply failed: {}", type(error).__name__)

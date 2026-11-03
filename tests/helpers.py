"""Test-owned assembly of real services; no legacy application API or mocked collaborators."""

from pathlib import Path
from typing import cast

import httpx

from fullish.conversation.store import ConversationStore
from fullish.generation import ImageGenerator
from fullish.management.groups import GroupManagement
from fullish.management.identity import DeveloperIdentity
from fullish.management.mcp import MCPManagement
from fullish.management.service import ManagementService
from fullish.management.store import ControlStore
from fullish.mcp.store import MCPStore
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies
from fullish.providers.chat import ChatProvider
from fullish.reply.engine import ReplyEngine
from fullish.reply.handler import ChatHandler
from fullish.reply.media import ImageReader
from fullish.reply.postprocess import ReplyPostprocessor
from fullish.reply.schema import ReplyModel
from fullish.reply.service import ConversationService
from fullish.scheduling.generation import GenerationCoordinator
from fullish.sending.service import SendingService
from fullish.settings import ModelSettings
from fullish.tools.runtime import ToolRuntime


def make_reply_engine(
    settings: ModelSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tools: ToolRuntime | None = None,
    image_generator: ImageGenerator | None = None,
    policies: GroupPolicies | None = None,
) -> ReplyEngine:
    return ReplyEngine(
        ChatProvider(settings, transport=transport),
        ImageReader(),
        tools=tools,
        image_generator=image_generator,
        access=AccessPolicy(policies=policies),
    )


def model_provider(engine: ReplyEngine) -> ChatProvider:
    return cast(ChatProvider, engine.provider)


async def close_reply_engine(engine: ReplyEngine) -> None:
    await model_provider(engine).close()
    await engine.reader.close()
    if engine.tools:
        await engine.tools.close()
    if engine.image_generator:
        await engine.image_generator.close()


def make_conversation_service(
    model: ReplyModel,
    *,
    allowed_groups: frozenset[str] = frozenset(),
    min_interval: float = 5,
    control: ManagementService | None = None,
    postprocessor: ReplyPostprocessor | None = None,
) -> ConversationService:
    return ConversationService(
        model,
        store=ConversationStore(),
        access=AccessPolicy(
            policies=control.store.policies if control else None, allowed_groups=allowed_groups
        ),
        generation=GenerationCoordinator(min_interval=min_interval),
        observe=control.observe if control else None,
        postprocessor=postprocessor,
    )


def make_chat_handler(conversations: ConversationService) -> ChatHandler:
    return ChatHandler(
        conversations, SendingService(conversations.access, confirmed=conversations.store.sent)
    )


def make_management(directory: Path) -> ManagementService:
    store = ControlStore(directory)
    return ManagementService(
        store,
        DeveloperIdentity(store),
        GroupManagement(store.policies),
        MCPManagement(MCPStore(directory)),
    )

"""Conversation reply use case, composed from state, access and generation services."""

import asyncio
from collections.abc import Callable

from fullish.collection import CollectedMessage, MessagePart, ParsedContent, Sender
from fullish.conversation.store import ConversationStore
from fullish.policies.access import AccessPolicy
from fullish.reply.postprocess import ReplyPostprocessor
from fullish.reply.schema import ReplyModel
from fullish.scheduling.generation import GenerationCoordinator
from fullish.sending import Reply, ReplyPlan
from fullish.telemetry import emit, message_trace, reference
from fullish.tools.runtime import ToolContext


def is_called(message: CollectedMessage) -> bool:
    return message.addressed or "fullish" in message.content.text.lower()


class ConversationService:
    def __init__(
        self,
        model: ReplyModel,
        *,
        store: ConversationStore,
        access: AccessPolicy,
        generation: GenerationCoordinator,
        observe: Callable[[str], None] | None = None,
        postprocessor: ReplyPostprocessor | None = None,
        postprocess_when: Callable[[ToolContext], bool] | None = None,
    ):
        self.model = model
        self.store = store
        self.access = access
        self.generation = generation
        self.observe = observe
        self.postprocessor = postprocessor
        self.postprocess_when = postprocess_when

    async def receive(
        self,
        group_id: str,
        message_id: str,
        sender: str,
        text: str,
        *,
        addressed: bool,
        images: list[str] | None = None,
    ) -> Reply | ReplyPlan | None:
        """Convenience entry for text-based replay and callers without an adapter."""
        parts = (MessagePart("text", text),) + tuple(
            MessagePart("image", "", {"url": url}, status="pending") for url in images or ()
        )
        return await self.receive_message(
            CollectedMessage(group_id, message_id, Sender(sender), ParsedContent(parts), addressed)
        )

    async def receive_message(self, message: CollectedMessage) -> Reply | ReplyPlan | None:
        with message_trace(message.group_id, message.message_id):
            return await self._receive(message)

    async def _receive(self, message: CollectedMessage) -> Reply | ReplyPlan | None:
        called = is_called(message)
        emit(
            "conversation.received",
            sender_ref=reference(message.sender.id),
            text_chars=len(message.content.text),
            image_count=len(message.content.image_urls),
            reply_image_count=len(message.reply.content.image_urls) if message.reply else 0,
            addressed=message.addressed,
        )
        if self.observe:
            try:
                await asyncio.to_thread(self.observe, message.group_id)
            except Exception as error:
                emit("group.observation.failed", level="WARNING", error_type=type(error).__name__)
        if not self.access.group_allowed(message.group_id):
            emit("conversation.skipped", reason="group_not_allowed")
            return None
        recorded = self.store.record(message)
        if recorded is None:
            return None
        group, current_turn = recorded
        if not called:
            emit("conversation.skipped", reason="context_only")
            return None
        async with self.generation.slot(group.activity):
            if not self.access.group_allowed(message.group_id):
                emit("conversation.skipped", reason="group_policy_changed")
                return None
            history = self.store.snapshot(group, current_turn, addressed=message.addressed)
            context = ToolContext(message.group_id, message.sender.id, message.message_id)
            draft = await self.model.reply(
                history,
                addressed=called,
                context=context,
            )
            if (
                draft
                and self.postprocessor
                and (self.postprocess_when is None or self.postprocess_when(context))
                and self.access.reply_allowed(message.group_id, draft)
            ):
                return await self.postprocessor.process(
                    draft,
                    context=history,
                    allow_images=self.access.feature_allowed(message.group_id, "image"),
                )
            return draft

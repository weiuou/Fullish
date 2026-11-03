"""Bounded model/tool execution; transport, media and delivery are separate services."""

import asyncio
import json
import time

import httpx

from fullish.errors import ModelError
from fullish.generation import ImageGenerator
from fullish.media.assets import ImageAssets
from fullish.policies.access import AccessPolicy
from fullish.providers.chat import FINAL_ANSWER_SECONDS, ChatBackend, ChatSession
from fullish.reply.images import ImageActions
from fullish.reply.input import InputBuilder
from fullish.reply.media import ImageReader
from fullish.reply.prompts import IMAGE_TOOL, SEND_IMAGE_TOOL, TOOL_PROMPT
from fullish.reply.schema import Turn
from fullish.sending import Reply
from fullish.sending.schema import outgoing
from fullish.telemetry import elapsed_ms, emit, operation_trace
from fullish.tools.runtime import ToolContext, ToolRuntime

MAX_TOOL_CALLS = 12
INCOMPLETE_REPLY = "这轮我没能把事情处理完，暂时给不了可靠的结论。"


class ReplyEngine:
    def __init__(
        self,
        provider: ChatBackend,
        reader: ImageReader,
        *,
        tools: ToolRuntime | None = None,
        image_generator: ImageGenerator | None = None,
        access: AccessPolicy | None = None,
    ):
        self.provider = provider
        self.reader = reader
        self.settings = provider.settings
        self.tools = tools
        self.image_generator = image_generator
        self.images = ImageActions(image_generator, tools, access or AccessPolicy())

    async def download(self, url: str, **metadata) -> str | None:
        return await self.reader.download(url, **metadata)

    async def _messages(
        self, turns: list[Turn], addressed: bool, *, images: ImageAssets | None = None
    ) -> list[dict[str, object]]:
        return await InputBuilder(
            self.download, image_generation=self.image_generator is not None
        ).build(turns, addressed, images=images)

    async def reply(
        self, turns: list[Turn], *, addressed: bool, context: ToolContext | None = None
    ) -> Reply | None:
        with operation_trace():
            start = time.monotonic()
            emit(
                "model.preparing",
                model=self.settings.model,
                history_turns=len(turns),
                addressed=addressed,
                timeout_seconds=self.settings.timeout,
            )
            try:
                result = await self._reply(turns, addressed=addressed, context=context)
            except ModelError as error:
                emit(
                    "model.failed",
                    level="WARNING",
                    error_code=str(error),
                    elapsed_ms=elapsed_ms(start),
                )
                raise
            except asyncio.CancelledError:
                emit("model.cancelled", elapsed_ms=elapsed_ms(start))
                raise
            except Exception as error:
                emit(
                    "model.failed",
                    level="WARNING",
                    error_type=type(error).__name__,
                    elapsed_ms=elapsed_ms(start),
                )
                raise
            emit(
                "model.completed",
                outcome="silence" if result is None else "reply",
                text_chars=len(outgoing(result).text) if result else 0,
                elapsed_ms=elapsed_ms(start),
            )
            return result

    async def _reply(
        self, turns: list[Turn], *, addressed: bool, context: ToolContext | None = None
    ) -> Reply | None:
        # Reserve a final, tools-free answer inside the original total chat deadline.
        deadline = time.monotonic() + self.settings.timeout
        reserve = min(FINAL_ANSWER_SECONDS, self.settings.timeout / 3)
        session = ChatSession(self.settings.model)
        payload: list[dict[str, object]] = []
        calls_used = 0
        tool_started = False
        final_reason = ""
        images = ImageAssets()
        try:
            async with asyncio.timeout(self.settings.timeout - reserve):
                payload = await self._messages(
                    turns,
                    addressed,
                    images=images
                    if self.image_generator or self.images.sending_allowed(context)
                    else None,
                )
                if self.tools:
                    await self.tools.prepare(context)
                schemas = self.tools.schemas(context) if self.tools else []
                if schemas:
                    payload.insert(1, {"role": "system", "content": TOOL_PROMPT})
                if self.images.generation_allowed(context):
                    schemas = [*schemas, IMAGE_TOOL]
                if self.images.sending_allowed(context):
                    schemas = [*schemas, SEND_IMAGE_TOOL]
                seen_ids: set[str] = set()
                while True:
                    request: dict[str, object] = {
                        "model": self.settings.model,
                        "messages": payload,
                        "max_tokens": 1024,
                        "stream": False,
                    }
                    if schemas:
                        request.update(tools=schemas, tool_choice="auto")
                    response = await self.provider.request(
                        request, session, deadline=deadline - reserve
                    )
                    if response.rejection_status in {400, 422} and schemas and calls_used == 0:
                        # Unsupported provider schemas must not break ordinary group chat.
                        schemas = []
                        payload.insert(
                            1,
                            {
                                "role": "system",
                                "content": "本轮工具接口不可用，只能聊天，"
                                "不得声称已搜索、执行或生成图片。",
                            },
                        )
                        emit("model.tools.unavailable", level="WARNING")
                        continue
                    response.ensure_success()
                    message = response.message
                    calls = message.get("tool_calls")
                    if calls:
                        if not schemas:
                            raise ModelError("unexpected_tool_call")
                        if not isinstance(calls, list):
                            raise ModelError("invalid_tool_call")
                        # Validate the whole batch before executing any part of it.
                        batch_ids: set[str] = set()
                        for call in calls:
                            if not isinstance(call, dict) or call.get("type") != "function":
                                raise ModelError("invalid_tool_call")
                            call_id, function = call.get("id"), call.get("function")
                            if (
                                not isinstance(call_id, str)
                                or not 1 <= len(call_id) <= 256
                                or call_id in seen_ids
                                or call_id in batch_ids
                                or not isinstance(function, dict)
                                or not isinstance(function.get("name"), str)
                                or not isinstance(function.get("arguments"), str)
                            ):
                                raise ModelError("invalid_tool_call")
                            batch_ids.add(call_id)
                        if calls_used + len(calls) > MAX_TOOL_CALLS:
                            final_reason = "tool_call_limit"
                            break
                        image_calls = [
                            call
                            for call in calls
                            if call["function"]["name"] in {"generate_image", "send_image"}
                        ]
                        if image_calls:
                            if (
                                image_calls[0]["function"]["name"] == "generate_image"
                                and not self.image_generator
                            ):
                                raise ModelError("image_tool_unavailable")
                            if len(calls) != 1:
                                raise ModelError("image_invalid_tool_call")
                            image_function = image_calls[0]["function"]
                            break
                        if not self.tools or not context:
                            raise ModelError("unexpected_tool_call")
                        seen_ids.update(batch_ids)
                        # Keep provider tool-call metadata (e.g. Gemini thought signatures).
                        assistant: dict[str, object] = {
                            "role": "assistant",
                            "content": message.get("content"),
                            "tool_calls": calls,
                        }
                        if isinstance(message.get("reasoning_content"), str):
                            assistant["reasoning_content"] = message["reasoning_content"]
                        payload.append(assistant)
                        existing_images = set(images.items)
                        for call in calls:
                            function = call["function"]
                            tool_started = True
                            result = await self.tools.invoke(
                                function["name"],
                                function["arguments"],
                                context,
                                call_id=call["id"],
                                images=images,
                            )
                            payload.append(
                                {"role": "tool", "tool_call_id": call["id"], "content": result}
                            )
                            calls_used += 1
                        visual = [
                            part
                            for image_id, asset in images.items.items()
                            if image_id not in existing_images
                            for part in asset.vision()
                        ]
                        if visual:
                            # Complete all tool responses before adding visual user content.
                            payload.append({"role": "user", "content": visual})
                        if calls_used >= MAX_TOOL_CALLS:
                            final_reason = "tool_call_limit"
                            break
                        continue
                    content = message.get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise ModelError("empty_response")
                    text = content.strip()
                    return None if text == "<SILENCE>" else text[:1500]
            if final_reason:
                return await self._final_answer(
                    payload, final_reason, calls_used, deadline, session
                )
            if image_function["name"] == "send_image":
                if not self.images.sending_allowed(context):
                    raise ModelError("image_disabled")
                return self.images.send(image_function, images)
            if not self.images.generation_allowed(context):
                raise ModelError("image_disabled")
            return await self.images.generate(image_function, images)
        except httpx.HTTPStatusError as error:
            raise ModelError(f"http_{error.response.status_code}") from None
        except TimeoutError:
            if tool_started and payload:
                return await self._final_answer(
                    payload, "tool_time_limit", calls_used, deadline, session
                )
            raise ModelError("connection_or_timeout") from None
        except httpx.RequestError:
            raise ModelError("connection_or_timeout") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("invalid_response") from None

    async def _final_answer(
        self,
        payload: list[dict[str, object]],
        reason: str,
        calls_used: int,
        deadline: float,
        session: ChatSession,
    ) -> str:
        emit("model.finalizing", reason=reason, tool_calls=calls_used, tool_limit=MAX_TOOL_CALLS)
        messages = list(payload)
        # A deadline may interrupt a tool batch: close pending calls before asking
        # the provider for a final answer, without executing any additional action.
        pending: dict[str, object] = {}
        for item in payload:
            if item.get("role") == "assistant":
                calls = item.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        pending[call["id"]] = call
            elif item.get("role") == "tool":
                pending.pop(str(item.get("tool_call_id")), None)
        messages.extend(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"ok": False, "error": "tool_time_limit"}),
            }
            for call_id in pending
        )
        messages.append(
            {
                "role": "system",
                "content": "本轮工具调用已结束，不能再调用任何工具。请现在给出最终答复："
                "只依据已获得的信息，说明已完成和未完成的部分；没有可靠结论就坦诚承认。"
                "不要输出等待提示、工具调用请求或 <SILENCE>。",
            }
        )
        fallback_reason = "deadline"
        try:
            async with asyncio.timeout(
                max(0, min(FINAL_ANSWER_SECONDS, deadline - time.monotonic()))
            ):
                response = await self.provider.request(
                    {
                        "model": self.settings.model,
                        "messages": messages,
                        "max_tokens": 1024,
                        "stream": False,
                    },
                    session,
                    deadline=min(deadline, time.monotonic() + FINAL_ANSWER_SECONDS),
                    final_answer=True,
                )
                response.ensure_success()
                message = response.message
                content = message.get("content")
                if (
                    not message.get("tool_calls")
                    and isinstance(content, str)
                    and content.strip()
                    and content.strip() != "<SILENCE>"
                ):
                    return content.strip()[:1500]
                fallback_reason = "invalid_final_response"
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            ModelError,
        ):
            fallback_reason = "final_request_failed"
        emit("model.final_answer.fallback", level="WARNING", reason=fallback_reason)
        return INCOMPLETE_REPLY

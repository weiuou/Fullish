"""Real chat/report services share group coordination; only external transports are simulated."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from nonebot import logger
from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
from nonebot.drivers import URL

from fullish.conversation.store import ConversationStore
from fullish.notifications.deployment import ReportError, ReportRequest
from fullish.notifications.report import DeploymentReporter
from fullish.platforms.qq.adapter import handle_group
from fullish.policies.access import AccessPolicy
from fullish.providers.chat import ChatProvider
from fullish.reply.engine import ReplyEngine
from fullish.reply.handler import ChatHandler
from fullish.reply.media import ImageReader
from fullish.reply.postprocess import ReplyPostprocessor
from fullish.reply.service import ConversationService
from fullish.scheduling.generation import GenerationCoordinator
from fullish.scheduling.queue import GroupMessageQueue
from fullish.sending.service import SendingService
from fullish.settings import DeploymentReportSettings, ModelSettings, ReplyPostprocessSettings
from fullish.telemetry import reference

GROUP = "g" * 32
REVISION = "a" * 40


def event(identifier: str):
    incoming = GroupAtMessageCreateEvent.model_validate(
        {
            "id": identifier,
            "content": "看看情况",
            "timestamp": "2026-11-03T06:00:00Z",
            "group_id": GROUP,
            "group_openid": GROUP,
            "author": {"id": "u", "member_openid": "u", "bot": False, "member_role": "member"},
        }
    )
    incoming.to_me = True
    return incoming


def bot():
    instance = object.__new__(Bot)
    instance.adapter = Mock()
    instance.adapter.get_api_base.return_value = URL("https://api.sgroup.qq.com")
    return instance


@pytest.mark.parametrize("notification", ["confirmed", "revoked", "unknown"])
def test_chat_plan_and_report_share_ack_order_policy_and_history(notification):
    async def run():
        records = []
        sink = logger.add(lambda message: records.append(message.record["extra"].get("flow", {})))
        generating = asyncio.Event()
        finish = asyncio.Event()
        requests = []
        posted = []
        permission = True

        async def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                generating.set()
                await finish.wait()
            return httpx.Response(200, json={"choices": [{"message": {"content": "处理完成"}}]})

        def express(request):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "messages": [
                                            {"parts": [{"type": "text", "text": "第一条"}]},
                                            {"parts": [{"type": "text", "text": "第二条"}]},
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        async def permission_response(request):
            return {"allow_proactive_msg": permission}

        async def send(**payload):
            nonlocal permission
            posted.append(payload)
            if notification == "revoked" and len(posted) == 1:
                permission = False
            if notification == "unknown" and "msg_id" not in payload:
                # Simulate QQ accepting the request while the client loses its acknowledgement.
                raise TimeoutError
            return {"id": "receipt-" + str(len(posted))}

        backend = ModelSettings("http://localhost/v1", "synthetic-key")
        provider = ChatProvider(backend, transport=httpx.MockTransport(respond))
        reader = ImageReader()
        engine = ReplyEngine(provider, reader)
        processor = ReplyPostprocessor(
            backend, ReplyPostprocessSettings(), transport=httpx.MockTransport(express)
        )
        access = AccessPolicy(notification_targets={"deployment": frozenset({GROUP})})
        store = ConversationStore()
        conversations = ConversationService(
            engine,
            store=store,
            access=access,
            generation=GenerationCoordinator(min_interval=0),
            postprocessor=processor,
        )
        sending = SendingService(access, confirmed=store.sent)
        handler = ChatHandler(conversations, sending)
        queue = GroupMessageQueue()
        reporter = DeploymentReporter(
            DeploymentReportSettings(GROUP, "weiuou/Fullish"),
            provider,
            queue=queue,
            sending=sending,
        )
        qq = bot()
        report = None
        try:
            with (
                patch.object(Bot, "_request", new=AsyncMock(side_effect=permission_response)),
                patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=send)),
            ):
                await queue.submit(
                    GROUP,
                    "first",
                    lambda: handle_group(handler, qq, event("first")),
                )
                await generating.wait()
                report = asyncio.create_task(
                    reporter.send(ReportRequest(previous=REVISION, current=REVISION), qq)
                )
                await asyncio.sleep(0)
                assert not posted and not report.done()
                finish.set()
                if notification == "confirmed":
                    assert await report == "sent"
                else:
                    with pytest.raises(ReportError):
                        await report
                assert [item["content"] for item in posted[:2]] == ["第一条", "第二条"]
                assert [item["msg_seq"] for item in posted[:2]] == [1, 2]
                notifications = [item for item in posted if "msg_id" not in item]
                assert len(notifications) == (0 if notification == "revoked" else 1)
                history = [
                    item for item in store.groups[GROUP].turns if item["role"] == "assistant"
                ]
                assert len(history) == (3 if notification == "confirmed" else 2)
                if notification == "confirmed":
                    assert history[-1]["source"] == "deployment"
                    assert REVISION in history[-1]["content"]
                await queue.execute(
                    GROUP,
                    "next",
                    lambda: handle_group(handler, qq, event("next")),
                )
                model_input = json.dumps(requests[-1], ensure_ascii=False)
                assert ("[机器人通知：deployment]" in model_input) == (notification == "confirmed")
                sends = [record for record in records if record.get("stage") == "qq.send.succeeded"]
                assert all(record["group_ref"] == reference(GROUP) for record in sends)
                assert len({record["trace_id"] for record in sends}) == (
                    3 if notification == "confirmed" else 2
                )
                if notification == "unknown":
                    failure = next(r for r in records if r.get("stage") == "delivery.failed")
                    assert failure["delivery_states"] == ["unknown"]
        finally:
            finish.set()
            if report and not report.done():
                report.cancel()
                await asyncio.gather(report, return_exceptions=True)
            await queue.close()
            await processor.close()
            await reader.close()
            await provider.close()
            logger.remove(sink)

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("when", ["queued", "sending", "shutdown"])
def test_report_cancellation_does_not_send_later_or_poison_group_worker(when):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        sending = asyncio.Event()
        posted = []

        async def hold():
            started.set()
            await release.wait()

        async def send(**payload):
            posted.append(payload)
            sending.set()
            await asyncio.Event().wait()

        provider = ChatProvider(ModelSettings("http://localhost/v1", "synthetic-key"))
        queue = GroupMessageQueue()
        reporter = DeploymentReporter(
            DeploymentReportSettings(GROUP, "weiuou/Fullish"),
            provider,
            queue=queue,
            sending=SendingService(
                AccessPolicy(notification_targets={"deployment": frozenset({GROUP})})
            ),
        )
        report = None
        try:
            with (
                patch.object(
                    Bot, "_request", new=AsyncMock(return_value={"allow_proactive_msg": True})
                ),
                patch.object(Bot, "post_group_messages", new=AsyncMock(side_effect=send)),
            ):
                await queue.submit(GROUP, "held", hold)
                await started.wait()
                report = asyncio.create_task(
                    reporter.send(ReportRequest(previous=REVISION, current=REVISION), bot())
                )
                await asyncio.sleep(0)
                if when != "queued":
                    release.set()
                    await sending.wait()
                if when == "shutdown":
                    await queue.close(timeout=0.01)
                else:
                    report.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await report
                release.set()
                if when != "shutdown":

                    async def next_job():
                        return "continued"

                    assert await queue.execute(GROUP, "after", next_job) == "continued"
                assert len(posted) == (0 if when == "queued" else 1)
        finally:
            release.set()
            if report and not report.done():
                report.cancel()
                await asyncio.gather(report, return_exceptions=True)
            await queue.close()
            await provider.close()

    asyncio.run(asyncio.wait_for(run(), 5))

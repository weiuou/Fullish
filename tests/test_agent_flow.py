"""Replay real agent RPC, event claims and QQ delivery with only external peers simulated."""

import asyncio
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Literal

import pytest

from fullish.agents.backend import AgentRouter, DimReplyModel
from fullish.agents.rpc import DimError, DimRPC
from fullish.agents.session import final_text
from fullish.agents.settings import AgentSettings
from fullish.agents.store import RunStore
from fullish.collection import CollectedMessage, MessagePart, ParsedContent, Sender
from fullish.conversation.store import ConversationStore
from fullish.policies.access import AccessPolicy
from fullish.reply.handler import ChatHandler
from fullish.reply.media import ImageReader
from fullish.reply.service import ConversationService
from fullish.scheduling.generation import GenerationCoordinator
from fullish.sending.service import SendingService
from fullish.tools.runtime import ToolContext


def completed_events(text="最终回复"):
    return [
        {"type": "turn", "name": "start", "data": {"turn_id": "turn"}},
        {
            "type": "step",
            "name": "text",
            "data": {
                "request_id": "intermediate",
                "turn_id": "turn",
                "delta": "中间说明不能发送",
            },
        },
        {"type": "step", "name": "thinking", "data": {"delta": "不能发送的思考"}},
        {
            "type": "step",
            "name": "text",
            "data": {
                "request_id": "final",
                "turn_id": "turn",
                "delta": text,
            },
        },
        {
            "type": "step",
            "name": "model-output-end",
            "data": {
                "request_id": "final",
                "turn_id": "turn",
                "status": "completed",
                "finish_reason": "end_turn",
            },
        },
        {"type": "turn", "name": "end", "data": {"turn_id": "turn", "status": "completed"}},
    ]


class DimPeer:
    def __init__(self):
        self.created = []
        self.submitted = []
        self.deleted = []
        self.stopped = []
        self.started = asyncio.Event()
        self.hang = False

    async def serve(self, reader, writer):
        try:
            while line := await reader.readline():
                request = json.loads(line)
                method, params = request["method"], request["params"]
                if method == "session.create":
                    self.created.append(params)
                    result = {"sessionId": str(len(self.created))}
                elif method == "session.submit":
                    self.submitted.append(params)
                    self.started.set()
                    result = {"sequence": 1}
                elif method == "session.history":
                    result = {
                        "events": [] if self.hang else completed_events(),
                        "through": 6,
                        "nextAfter": 6,
                        "done": True,
                    }
                elif method == "session.delete":
                    self.deleted.append(params["sessionId"])
                    result = {"deleted": True}
                elif method == "session.stop":
                    self.stopped.append(params["sessionId"])
                    result = {"sequence": 6}
                else:
                    result = {"sequence": 6}
                writer.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": result,
                        }
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class QQPeer:
    mode: Literal["reply", "proactive"] = "reply"
    group_id = "group-a"

    def __init__(self):
        self.messages = []
        self.plan = None
        self.revoke: Callable[[], None] | None = None

    async def allowed(self):
        if self.revoke:
            self.revoke()
        return True

    def remaining(self):
        return 5

    async def prepare(self, content):
        self.plan = content

    async def send(self, index):
        self.messages.append(self.plan)
        return {"id": f"qq-{index}"}


def incoming(message_id="m1"):
    return CollectedMessage(
        "group-a",
        message_id,
        Sender("member"),
        ParsedContent((MessagePart("text", "你好"),)),
        True,
    )


def test_agent_to_confirmed_qq_and_restart_redelivery(tmp_path, monkeypatch):
    async def replay():
        peer = DimPeer()
        server = await asyncio.start_server(peer.serve, "127.0.0.1", 0)

        async def connect(*_args, **_kwargs):
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            return DimRPC(reader, writer)

        monkeypatch.setattr(DimRPC, "connect", connect)
        settings = AgentSettings(
            "external-fixture",
            tmp_path / "workspace",
            tmp_path / "runs.db",
            frozenset({"group-a"}),
            enabled=True,
        )
        access = AccessPolicy(allowed_groups=frozenset({"group-a"}))
        reader = ImageReader()
        store = RunStore(settings.state_path, bot_id="bot")
        model = DimReplyModel(settings, store, reader, access)
        conversations = ConversationService(
            model,
            store=ConversationStore(),
            access=access,
            generation=GenerationCoordinator(min_interval=0),
        )
        handler = ChatHandler(
            conversations,
            SendingService(access, confirmed=conversations.store.sent),
            runs=store,
        )
        qq = QQPeer()
        try:
            await handler.handle(incoming(), qq, segment_types=["text"])
            assert qq.messages == ["最终回复"]
            assert peer.created[0]["tools"] == []
            assert peer.created[0]["interactive"] is False
            assert peer.created[0]["permissionPolicy"] == {
                "preset": "read-only",
                "allowedTools": [],
            }
            assert len(peer.submitted) == 1 and len(peer.deleted) == 1
            snapshot = conversations.store.groups["group-a"].turns
            assert snapshot[-1]["content"] == "最终回复"
            context = ToolContext("group-a", "member", "m1")
            record = store.get(context)
            assert record and (record.state, record.delivery) == ("completed", "confirmed")
            # New process state and a backend configuration rollback retain the event claim.
            restarted_store = RunStore(settings.state_path, bot_id="bot")
            restarted_store.recover()
            disabled = DimReplyModel(
                replace(settings, enabled=False), restarted_store, reader, access
            )
            router = AgentRouter(model, disabled)
            assert await router.reply([], addressed=True, context=context) is None
            assert len(peer.submitted) == 1
            # A different event can execute, but revocation after generation blocks its delivery.
            qq.revoke = lambda: setattr(access, "allowed_groups", frozenset({"different-group"}))
            await handler.handle(incoming("m2"), qq, segment_types=["text"])
            blocked = store.get(ToolContext("group-a", "member", "m2"))
            assert blocked and blocked.delivery == "blocked"
            assert len(qq.messages) == 1
        finally:
            await model.close()
            await reader.close()
            server.close()
            await server.wait_closed()

    asyncio.run(replay())


def test_cancelling_agent_stops_and_deletes_its_exclusive_session(tmp_path, monkeypatch):
    async def replay():
        peer = DimPeer()
        peer.hang = True
        server = await asyncio.start_server(peer.serve, "127.0.0.1", 0)

        async def connect(*_args, **_kwargs):
            streams = await asyncio.open_connection(*server.sockets[0].getsockname())
            return DimRPC(*streams)

        monkeypatch.setattr(DimRPC, "connect", connect)
        settings = AgentSettings(
            "fixture",
            tmp_path / "workspace",
            tmp_path / "runs.db",
            frozenset({"group-a"}),
            enabled=True,
        )
        reader = ImageReader()
        store = RunStore(settings.state_path, bot_id="bot")
        model = DimReplyModel(settings, store, reader, AccessPolicy())
        context = ToolContext("group-a", "member", "cancel")
        try:
            task = asyncio.create_task(model.reply([], addressed=True, context=context))
            await peer.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert peer.stopped == ["1"] and peer.deleted == ["1"]
            assert await model.reply([], addressed=True, context=context) is None
        finally:
            await model.close()
            await reader.close()
            server.close()
            await server.wait_closed()

    asyncio.run(replay())


def test_concurrent_claim_crash_recovery_and_unresolved_retention(tmp_path):
    store = RunStore(tmp_path / "runs.db", bot_id="bot")
    context = ToolContext("group", "member", "same-event")
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim(context), range(8)))
    claimed = [run for run in claims if run]
    assert len(claimed) == 1
    run = claimed[0]
    store.update(run.run_id, state="running", session_id="native-session")
    store.delivery(run.run_id, "sending")
    restarted = RunStore(store.path, bot_id="bot")
    assert restarted.recover() == 1
    recovered = restarted.get(context)
    assert recovered and (recovered.state, recovered.delivery) == ("unknown", "unknown")
    assert restarted.prune(now=time.time() + 365 * 86400) == 0
    assert restarted.claim(context) is None
    assert restarted.claim(ToolContext("other-group", "member", "same-event")) is not None
    # The ledger contains no payload that could replay a chat reply or a transfer.
    assert b"\xe4\xbd\xa0\xe5\xa5\xbd" not in store.path.read_bytes()


def test_final_output_excludes_intermediate_and_failed_model_text():
    assert final_text(completed_events()) == "最终回复"
    assert final_text(completed_events("<SILENCE>")) is None
    events = completed_events("不得发送的失败稿")
    events[-1]["data"]["status"] = "failed"
    with pytest.raises(DimError, match="not_completed"):
        final_text(events)

"""Replay web tool evidence across restarts without collecting raw group chat."""

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot import logger

from fullish.model import ChatModel
from fullish.settings import ModelSettings, ToolSettings
from fullish.telemetry import message_trace, reference, trace_id
from fullish.tool_audit import MAX_SNAPSHOT_BYTES, ToolAudit
from fullish.tools import ToolContext, ToolRuntime

GROUP = "g" * 32
CONTEXT = ToolContext(GROUP, "member-private", "message-private")


@pytest.fixture
def flow():
    records = []

    def collect(message):
        if record := message.record["extra"].get("flow"):
            records.append(record)

    sink = logger.add(collect)
    try:
        yield records
    finally:
        logger.remove(sink)


def tools(path: Path, handler) -> ToolRuntime:
    return ToolRuntime(
        ToolSettings(frozenset({GROUP}), "search-secret"),
        web_transport=httpx.MockTransport(handler),
        audit=ToolAudit(path, secrets=("search-secret",)),
    )


def test_model_search_then_fetch_records_exact_evidence_and_call_order(tmp_path, monkeypatch, flow):
    path = tmp_path / "audit.sqlite3"
    posted = []
    source = {"title": "Fullish", "url": "https://example.com/project", "summary": "项目说明"}

    def web(request):
        if request.url.path.endswith("search"):
            return httpx.Response(200, json={"results": [source]})
        return httpx.Response(200, json={"title": "Fullish", "content": "README 正文"})

    def chat(request):
        data = json.loads(request.content)
        posted.append(data)
        if len(posted) <= 2:
            search = len(posted) == 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "provider-call-" + str(len(posted)),
                                        "type": "function",
                                        "function": {
                                            "name": "web_search" if search else "web_fetch",
                                            "arguments": json.dumps(
                                                {"query": "Fullish GitHub"}
                                                if search
                                                else {"url": source["url"]}
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "找到项目了"}}]})

    async def run():
        monkeypatch.setattr(
            asyncio.get_running_loop(),
            "getaddrinfo",
            AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]),
        )
        model = ChatModel(
            ModelSettings("https://chat.example/v1", "model-secret"),
            transport=httpx.MockTransport(chat),
            tools=tools(path, web),
        )
        try:
            with message_trace(GROUP, CONTEXT.message_id):
                expected_trace = trace_id()
                assert (
                    await model.reply(
                        [{"role": "user", "content": "chat-body-private"}],
                        addressed=True,
                        context=CONTEXT,
                    )
                    == "找到项目了"
                )
        finally:
            await model.close()
        # A fresh reader represents a replacement bot container on the same volume.
        rows = list(reversed(ToolAudit(path).read(trace_id=expected_trace)))
        assert [row["tool"] for row in rows] == ["web_search", "web_fetch"]
        assert rows[0]["snapshot"]["request"] == {"query": "Fullish GitHub"}
        assert rows[0]["result_count"] == 1
        assert rows[1]["content_chars"] == len("README 正文")
        for i, row in enumerate(rows):
            assert row["snapshot"]["tool_call_id"] == f"provider-call-{i + 1}"
            assert row["snapshot"]["response"] == json.loads(
                posted[i + 1]["messages"][-1]["content"]
            )
            assert not row["snapshot_redacted"] and not row["snapshot_truncated"]
            completed = next(r for r in flow if r.get("audit_id") == row["audit_id"])
            assert completed["audit_saved"] is True
            assert completed["trace_id"] == expected_trace
            assert completed["elapsed_ms"] >= 0
            assert completed["tool_call_ref"] == reference(f"provider-call-{i + 1}")
        serialized = json.dumps(rows)
        for private in (
            "chat-body-private",
            "model-secret",
            "search-secret",
            CONTEXT.sender_id,
            CONTEXT.message_id,
            GROUP,
        ):
            assert private not in serialized
        assert "Fullish GitHub" not in json.dumps(flow)

    asyncio.run(run())


@pytest.mark.parametrize("case", ["empty", "http_error", "invalid_response", "timeout"])
def test_empty_results_and_failures_remain_distinguishable(tmp_path, flow, case):
    async def web(request):
        if case == "http_error":
            return httpx.Response(401, json={"error": "credential-in-upstream-body"})
        if case == "invalid_response":
            return httpx.Response(200, json={"unexpected": "private-upstream-body"})
        if case == "timeout":
            raise httpx.ReadTimeout("private-url", request=request)
        return httpx.Response(200, json={"results": []})

    async def run():
        path = tmp_path / "audit.sqlite3"
        runtime = tools(path, web)
        try:
            response = json.loads(await runtime.invoke("web_search", '{"query":"test"}', CONTEXT))
        finally:
            await runtime.close()
        row = ToolAudit(path).read()[0]
        assert row["snapshot"]["response"] == response
        if case == "empty":
            assert row["outcome"] == "completed" and row["result_count"] == 0
        else:
            assert row["outcome"] == "failed" and row["error_code"] == response["error"]
        assert "upstream-body" not in json.dumps(row)
        assert "test" not in json.dumps(flow)

    asyncio.run(run())


def test_storage_failure_is_visible_and_does_not_replace_tool_result(tmp_path, flow, monkeypatch):
    async def run():
        runtime = tools(
            tmp_path / "audit.sqlite3", lambda _: httpx.Response(200, json={"results": []})
        )
        monkeypatch.setattr(
            runtime.audit,
            "save",
            lambda _: (_ for _ in ()).throw(OSError("private-path-with-secret")),
        )
        try:
            assert json.loads(await runtime.invoke("web_search", '{"query":"test"}', CONTEXT))["ok"]
        finally:
            await runtime.close()
        assert flow[-1]["outcome"] == "completed" and not flow[-1]["audit_saved"]
        assert any(r["stage"] == "tool.audit.failed" for r in flow)
        assert "private-path" not in json.dumps(flow)

    asyncio.run(run())


def test_cancelled_web_call_retains_request_without_claiming_success(tmp_path, flow):
    async def web(request):
        raise asyncio.CancelledError

    async def run():
        path = tmp_path / "audit.sqlite3"
        runtime = tools(path, web)
        try:
            with pytest.raises(asyncio.CancelledError):
                await runtime.invoke("web_search", '{"query":"cancelled query"}', CONTEXT)
        finally:
            await runtime.close()
        row = ToolAudit(path).read()[0]
        assert row["outcome"] == "cancelled"
        assert row["snapshot"]["response"] == {"ok": False, "error": "tool_cancelled"}
        assert flow[-1]["outcome"] == "cancelled" and flow[-1]["audit_saved"]

    asyncio.run(run())


def record(identifier: str, snapshot: dict | None = None) -> dict:
    return {
        "audit_id": identifier,
        "trace_id": "trace-" + identifier,
        "snapshot": snapshot or {"request": {"query": "hello"}, "response": {"ok": True}},
    }


def test_credentials_are_redacted_in_queries_results_urls_and_bodies(tmp_path):
    path = tmp_path / "audit.sqlite3"
    snapshot = {
        "request": {"query": "search-secret"},
        "response": {
            "api_key": "other-private-key",
            "body": "Bearer bearer-private",
            "content": "search-secret sk-1234567890123456",
            "url": "https://user:pass@example.com/page?token=url-private&q=Fullish#fragment",
        },
    }
    ToolAudit(path, secrets=("search-secret",)).save(record("one", snapshot))
    row = ToolAudit(path).read()[0]
    data = json.dumps(row)
    for secret in (
        "search-secret",
        "other-private-key",
        "bearer-private",
        "sk-1234567890123456",
        "user:pass",
        "url-private",
        "fragment",
    ):
        assert secret not in data
        assert secret.encode() not in path.read_bytes()
    assert row["snapshot_redacted"] is True
    assert "q=Fullish" in row["snapshot"]["response"]["url"]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_count_expiry_size_and_concurrent_writes_are_bounded(tmp_path):
    path = tmp_path / "audit.sqlite3"
    audit = ToolAudit(path, max_records=10)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: audit.save(record(str(i))), range(20)))
    assert len(audit.read()) == 10
    with sqlite3.connect(path) as db:
        db.execute("UPDATE calls SET created = ?", (time.time() - 8 * 24 * 60 * 60,))
    audit.save(record("new", {"response": "大" * 100_000}))
    rows = audit.read()
    assert len(rows) == 1 and rows[0]["audit_id"] == "new"
    assert rows[0]["snapshot_truncated"] is True
    assert rows[0]["snapshot"]["truncated"] is True
    assert len(json.dumps(rows[0]["snapshot"], ensure_ascii=False).encode()) <= MAX_SNAPSHOT_BYTES
    assert ToolAudit(path).read(trace_id="no-match") == []


def test_denied_requests_and_sandbox_commands_never_create_web_snapshots(tmp_path):
    async def run():
        path = tmp_path / "audit.sqlite3"
        runtime = tools(path, lambda _: httpx.Response(200, json={"results": []}))
        try:
            await runtime.invoke(
                "web_search", '{"query":"private"}', ToolContext("other", "u", "m")
            )
            await runtime.invoke("web_search", '{"not-query":"private"}', CONTEXT)
            await runtime.invoke("sandbox_exec", '{"command":"private-command"}', CONTEXT)
            assert not path.exists()
        finally:
            await runtime.close()

    asyncio.run(run())


def test_inspection_cli_filters_without_creating_missing_store(tmp_path):
    path = tmp_path / "audit.sqlite3"
    audit = ToolAudit(path)
    audit.save(record("one"))
    audit.save(record("two"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fullish.tool_audit",
            "--path",
            str(path),
            "--trace-id",
            "trace-one",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["audit_id"] == "one"
    missing = tmp_path / "missing.sqlite3"
    result = subprocess.run(
        [sys.executable, "-m", "fullish.tool_audit", "--path", str(missing)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and not missing.exists()

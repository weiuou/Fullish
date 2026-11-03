"""CPA quota normalization, credential boundaries and actual MCP requests."""

import asyncio
import json

import httpx
import pytest
from mcp import Client

from fullish.cpa_usage import (
    CPAUsage,
    CPAUsageError,
    CPAUsageSettings,
    account_ref,
    claude_windows,
    codex_windows,
    google_windows,
)
from fullish.cpa_usage_mcp import create_server

KEY = "private-management-key"
EMAIL = "private-account@example.test"
ROWS = [
    {
        "provider": "codex",
        "auth_index": "codex-index",
        "name": EMAIL + ".json",
        "email": EMAIL,
        "id_token": {"chatgpt_account_id": "account-id", "chatgpt_plan_type": "team"},
        "status_message": KEY,
    },
    {
        "provider": "cline-pass",
        "auth_index": "plugin-index",
        "quota_plugin_id": "clinepassbridge",
        "supports_quota": True,
    },
    {"provider": "antigravity", "auth_index": "google-index", "project_id": "private-project"},
]
CODEX = {
    "plan_type": "team",
    "email": EMAIL,
    "rate_limit": {
        "primary_window": {
            "used_percent": 0,
            "limit_window_seconds": 18000,
            "reset_at": 1800000000,
        },
        "secondary_window": {
            "used_percent": 25,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 3600,
        },
    },
}
PLUGIN = {
    "groups": [
        {
            "displayName": "Cline Pass",
            "buckets": [
                {"window": "7d", "remainingFraction": 0.65, "resetTime": "2026-11-05T01:00:00Z"}
            ],
        }
    ],
    "subscription": {"plan": "pro"},
    "summary": [{"key": "remaining", "value": 42, "label": EMAIL}],
}
GOOGLE = {
    "groups": [
        {
            "displayName": "Gemini",
            "buckets": [{"remainingFraction": 1, "resetTime": "2026-11-04T00:00:00Z"}],
        }
    ]
}


class Backend:
    def __init__(self, legacy=False):
        self.requests = []
        self.legacy = legacy

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + KEY
        path = request.url.path
        if path == "/v8/management/credentials":
            return httpx.Response(404 if self.legacy else 200, json={"files": ROWS})
        if path == "/v0/management/auth-files":
            return httpx.Response(200, json={"files": [ROWS[0], ROWS[2]]})
        if path == "/v8/management/plugins/clinepassbridge/quota":
            assert request.method == "GET" and request.url.params["auth_index"] == "plugin-index"
            return httpx.Response(200, json=PLUGIN)
        if path.endswith("/api-call"):
            body = json.loads(request.content)
            assert body["header"]["Authorization"] == "Bearer $TOKEN$"
            if body["url"] == "https://chatgpt.com/backend-api/wham/usage":
                assert body["method"] == "GET"
                assert body["header"]["Chatgpt-Account-Id"] == "account-id"
                result = CODEX
            else:
                assert (
                    body["url"]
                    == "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
                )
                assert body["method"] == "POST"
                assert json.loads(body["data"]) == {"project": "private-project"}
                result = GOOGLE
            return httpx.Response(200, json={"status_code": 200, "body": json.dumps(result)})
        if path == "/v0/management/usage":
            return httpx.Response(404, json={})
        raise AssertionError("Unexpected endpoint: " + path)


def test_v8_quotas_are_read_only_sanitized_and_cached():
    backend = Backend()

    async def run():
        client = CPAUsage(
            CPAUsageSettings("http://localhost", KEY), transport=httpx.MockTransport(backend)
        )
        try:
            accounts = await client.subscriptions()
            assert accounts["total"] == 3
            data = await client.quotas(limit=3)
            assert [r["quota_status"] for r in data["accounts"]] == ["ok", "ok", "ok"]
            assert data["accounts"][0]["windows"][0]["remaining_percent"] == 100
            assert data["accounts"][1]["windows"][0]["remaining_percent"] == 65
            assert data["accounts"][1]["summary"] == [{"metric": "remaining", "value": 42}]
            raw = json.dumps({"accounts": accounts, "quota": data})
            for private in [KEY, EMAIL, "private-project", "account-id", "codex-index"]:
                assert private not in raw
            before = len(backend.requests)
            cached = await client.quotas(limit=3)
            assert all(r["cached"] for r in cached["accounts"])
            assert len(backend.requests) == before + 1  # Account permissions/state are refreshed.
            with pytest.raises(CPAUsageError, match="not_found"):
                await client.quotas(account="../../config")
            assert not (await client.request_usage())["available"]
            assert all(
                "usage-queue" not in str(r.url) and "reset" not in str(r.url)
                for r in backend.requests
            )
        finally:
            await client.close()

    asyncio.run(run())


def test_legacy_gateway_uses_only_known_read_routes():
    backend = Backend(legacy=True)

    async def run():
        client = CPAUsage(
            CPAUsageSettings("http://localhost", KEY), transport=httpx.MockTransport(backend)
        )
        try:
            result = await client.quotas(provider="codex", limit=1)
            assert result["total"] == 1 and result["accounts"][0]["quota_status"] == "ok"
            assert client.prefix == "v0/management"
        finally:
            await client.close()

    asyncio.run(run())


def test_missing_quota_never_means_zero_or_full():
    row = codex_windows({"rate_limit": {"primary_window": {"reset_at": 1800000000}}})[0]
    assert row["used_percent"] is None and row["remaining_percent"] is None
    rows = google_windows(
        {"buckets": [{"modelId": "Gemini"}, {"remainingFraction": 0}, {"remainingFraction": True}]}
    )
    assert rows[0]["remaining_percent"] is None
    assert rows[1]["remaining_percent"] == 0 and rows[1]["used_percent"] == 100
    assert rows[2]["remaining_percent"] is None
    assert claude_windows({"five_hour": {"utilization": 0}})[0]["used_percent"] == 0
    assert claude_windows({"seven_day": {"utilization": "NaN"}})[0]["used_percent"] is None
    assert account_ref(ROWS[0]) == account_ref(dict(ROWS[0]))


def test_disabled_unsupported_and_failed_provider_stay_unknown():
    calls = []
    rows = [
        {"provider": "future", "auth_index": "future"},
        {"provider": "codex", "auth_index": "disabled", "disabled": True},
        {"provider": "gemini", "auth_index": "missing-project"},
    ]

    def backend(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/credentials")
        return httpx.Response(200, json={"files": rows})

    async def run():
        client = CPAUsage(
            CPAUsageSettings("http://localhost", KEY), transport=httpx.MockTransport(backend)
        )
        try:
            result = await client.quotas(limit=3)
            assert all(
                r["quota_status"] == "unknown" and r["windows"] == [] for r in result["accounts"]
            )
            assert len(calls) == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_bad_management_auth_stops_repeated_attempts():
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(401, json={"error": KEY})

    async def run():
        client = CPAUsage(
            CPAUsageSettings("http://localhost", KEY), transport=httpx.MockTransport(backend)
        )
        try:
            for _ in range(2):
                with pytest.raises(CPAUsageError, match="authentication_failed") as error:
                    await client.subscriptions()
                assert KEY not in str(error.value)
            assert len(calls) == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_discovers_and_calls_without_exposing_admin_operations():
    backend = Backend()
    settings = CPAUsageSettings("http://localhost", KEY)
    server = create_server(
        settings, client_factory=lambda s: CPAUsage(s, transport=httpx.MockTransport(backend))
    )

    async def run():
        async with Client(server) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {
                "cpa_subscriptions",
                "cpa_subscription_quota",
                "cpa_request_usage",
            }
            assert all(t.annotations and t.annotations.read_only_hint for t in tools)
            for tool in tools:
                assert not {"url", "auth_index", "headers", "key", "method"} & set(
                    tool.input_schema.get("properties", {})
                )
            result = await client.call_tool("cpa_subscription_quota", {"limit": 3})
            assert not result.is_error and result.structured_content["total"] == 3
            rejected = await client.call_tool("cpa_subscription_quota", {"limit": 10})
            assert rejected.is_error

    asyncio.run(run())


def test_settings_read_private_file_without_repr_leak(tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_text(KEY)
    monkeypatch.setenv("CPA_MANAGEMENT_KEY_FILE", str(path))
    monkeypatch.delenv("CPA_MANAGEMENT_KEY", raising=False)
    monkeypatch.setenv("CPA_BASE_URL", "http://localhost")
    assert KEY not in repr(CPAUsageSettings.from_env())
    monkeypatch.setenv("CPA_BASE_URL", "http://remote.example")
    with pytest.raises(CPAUsageError, match="invalid_management_url"):
        CPAUsageSettings.from_env()

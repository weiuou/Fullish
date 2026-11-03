"""Read-only CLIProxyAPI account/quota views. Credentials and raw responses stay private."""

import asyncio
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class CPAUsageError(ValueError):
    pass


@dataclass(frozen=True)
class CPAUsageSettings:
    base_url: str
    management_key: str = field(repr=False)
    unix_socket: str | None = None

    @classmethod
    def from_env(cls):
        base = os.environ.get("CPA_BASE_URL", "http://127.0.0.1:8317").rstrip("/")
        socket = os.environ.get("CPA_UNIX_SOCKET") or None
        url = urlsplit(base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (
                url.scheme == "http"
                and not socket
                and url.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise CPAUsageError("cpa_invalid_management_url")
        key = os.environ.get("CPA_MANAGEMENT_KEY", "").strip()
        if not key and (path := os.environ.get("CPA_MANAGEMENT_KEY_FILE")):
            try:
                with Path(path).open(encoding="utf-8") as stream:
                    key = stream.read(4097).strip()
            except OSError:
                raise CPAUsageError("cpa_management_key_unavailable") from None
        if not key or len(key) > 4096 or "\n" in key or "\r" in key:
            raise CPAUsageError("cpa_management_key_required")
        return cls(base, key, socket)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except ValueError:
        return None


def percentage(value):
    result = number(value)
    return round(result, 2) if result is not None and 0 <= result <= 100 else None


def instant(value):
    try:
        if isinstance(value, str) and not re.fullmatch(r"[0-9.]+", value):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
        elif (seconds := number(value)) is not None:
            date = datetime.fromtimestamp(seconds / 1000 if seconds > 1e12 else seconds, UTC)
        else:
            return None
        return date.astimezone(UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def window(label, used=None, remaining=None, reset=None, seconds=None):
    used, remaining = percentage(used), percentage(remaining)
    if used is None and remaining is not None:
        used = round(100 - remaining, 2)
    if remaining is None and used is not None:
        remaining = round(100 - used, 2)
    duration = number(seconds)
    return {
        "window": label,
        "used_percent": used,
        "remaining_percent": remaining,
        "resets_at": instant(reset),
        "window_seconds": duration if duration and duration > 0 else None,
    }


def codex_windows(payload):
    result = []
    limits = [
        ("rate_limit", payload.get("rate_limit", payload.get("rateLimit"))),
        ("code_review", payload.get("code_review_rate_limit", payload.get("codeReviewRateLimit"))),
    ]
    for label, limit in limits:
        if not isinstance(limit, dict):
            continue
        for key in ("primary_window", "secondary_window"):
            row = limit.get(
                key, limit.get("primaryWindow" if key == "primary_window" else "secondaryWindow")
            )
            if not isinstance(row, dict):
                continue
            reset = row.get("reset_at", row.get("resetAt"))
            if (
                reset is None
                and (offset := number(row.get("reset_after_seconds", row.get("resetAfterSeconds"))))
                is not None
                and 0 <= offset <= 366 * 86400
            ):
                reset = (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()
            result.append(
                window(
                    f"{label}/{key}",
                    used=row.get("used_percent", row.get("usedPercent")),
                    reset=reset,
                    seconds=row.get("limit_window_seconds", row.get("limitWindowSeconds")),
                )
            )
    return result


def claude_windows(payload):
    keys = {
        "five_hour": 18000,
        "seven_day": 604800,
        "seven_day_sonnet": 604800,
        "seven_day_opus": 604800,
        "seven_day_oauth_apps": 604800,
        "seven_day_cowork": 604800,
    }
    return [
        window(key, used=row.get("utilization"), reset=row.get("resets_at"), seconds=seconds)
        for key, seconds in keys.items()
        if isinstance(row := payload.get(key), dict)
    ]


def google_windows(payload):
    result = []
    groups = payload.get("groups")
    if isinstance(groups, list):
        buckets = [
            (
                str(g.get("displayName", g.get("display_name", "quota")))
                + "/"
                + str(b.get("window", "quota")),
                b,
            )
            for g in groups
            if isinstance(g, dict)
            for b in g.get("buckets", [])
            if isinstance(b, dict)
        ]
    elif isinstance(payload.get("buckets"), list):
        buckets = [
            (b.get("modelId", "quota"), b) for b in payload["buckets"] if isinstance(b, dict)
        ]
    elif isinstance(payload.get("models"), dict):
        buckets = [
            (name, model.get("quotaInfo", {}))
            for name, model in payload["models"].items()
            if isinstance(model, dict)
        ]
    else:
        return result
    for name, bucket in buckets[:64]:
        fraction = number(bucket.get("remainingFraction", bucket.get("remaining_fraction")))
        # Model labels are data, never copied from credential names or email fields.
        label = (
            str(name)[:100] if re.fullmatch(r"[A-Za-z0-9 _./()-]{1,100}", str(name)) else "quota"
        )
        result.append(
            window(
                label,
                remaining=fraction * 100 if fraction is not None and 0 <= fraction <= 1 else None,
                reset=bucket.get("resetTime", bucket.get("reset_time")),
            )
        )
    return result


def account_ref(row):
    provider = str(row.get("provider") or row.get("type") or "unknown").lower()
    identifier = str(
        row.get("auth_index") or row.get("authIndex") or row.get("id") or row.get("name") or ""
    )
    return "acct_" + hashlib.sha256((provider + ":" + identifier).encode()).hexdigest()[:16]


def provider_name(row):
    value = str(row.get("provider") or row.get("type") or "unknown").lower()
    return value if re.fullmatch(r"[a-z0-9_-]{1,32}", value) else "unknown"


def plan_name(value):
    return (
        value
        if isinstance(value, str)
        and value
        in {
            "free",
            "go",
            "plus",
            "pro",
            "team",
            "business",
            "enterprise",
            "education",
            "max",
            "ultra",
        }
        else None
    )


class CPAUsage:
    def __init__(self, settings: CPAUsageSettings, *, transport=None):
        self.client = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            headers={"Authorization": "Bearer " + settings.management_key},
            transport=transport
            or (
                httpx.AsyncHTTPTransport(uds=settings.unix_socket) if settings.unix_socket else None
            ),
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        )
        self.prefix = "v8/management"
        self.auth_failed = False
        self.cache: dict[str, tuple[float, str, dict]] = {}
        self.lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, *, payload=None):
        if self.auth_failed:
            raise CPAUsageError("cpa_management_authentication_failed")
        try:
            async with self.client.stream(method, path, json=payload) as response:
                if response.status_code in {401, 403}:
                    self.auth_failed = True
                    raise CPAUsageError("cpa_management_authentication_failed")
                if response.status_code in {404, 405, 501}:
                    raise CPAUsageError("cpa_endpoint_unavailable")
                if response.status_code != 200:
                    raise CPAUsageError("cpa_management_unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2 * 1024 * 1024:
                        raise CPAUsageError("cpa_response_too_large")
                    body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise CPAUsageError("cpa_invalid_response")
            return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, CPAUsageError):
                raise
            raise CPAUsageError("cpa_management_unavailable") from None

    async def _accounts(self):
        path = "credentials" if self.prefix.startswith("v8") else "auth-files"
        try:
            result = await self._request("GET", f"{self.prefix}/{path}")
        except CPAUsageError as error:
            if str(error) != "cpa_endpoint_unavailable" or self.prefix.startswith("v0"):
                raise
            result = await self._request("GET", "v0/management/auth-files")
            self.prefix = "v0/management"
        rows = result.get("files", result.get("credentials"))
        if not isinstance(rows, list):
            raise CPAUsageError("cpa_invalid_account_list")
        if result.get("has_more") or result.get("next_cursor") or len(rows) > 500:
            raise CPAUsageError("cpa_account_pagination_not_supported")
        return [row for row in rows if isinstance(row, dict)]

    def _public_account(self, row):
        claims = row.get("id_token") if isinstance(row.get("id_token"), dict) else {}
        return {
            "account_ref": account_ref(row),
            "provider": provider_name(row),
            "plan": plan_name(claims.get("chatgpt_plan_type", row.get("plan_type"))),
            "disabled": row.get("disabled") is True,
            "unavailable": row.get("unavailable") is True,
            "quota_supported": row.get("supports_quota") is True
            or provider_name(row) in {"codex", "claude", "gemini", "gemini-cli", "antigravity"},
            "status": row.get("status")
            if row.get("status") in {"active", "error", "disabled", "ready", "idle"}
            else "unknown",
        }

    async def subscriptions(self, provider="all", offset=0, limit=20):
        rows = await self._accounts()
        selected = [
            self._public_account(row)
            for row in rows
            if provider == "all" or provider_name(row) == provider
        ]
        return {
            "accounts": selected[offset : offset + limit],
            "total": len(selected),
            "next_offset": offset + limit if offset + limit < len(selected) else None,
            "scope": "CPA 配置的账号；账号代号不包含邮箱或认证文件名",
        }

    async def _upstream(self, row, method, url, *, data=None, header=None):
        index = row.get("auth_index", row.get("authIndex"))
        if not isinstance(index, (str, int)) or isinstance(index, bool) or not str(index):
            raise CPAUsageError("cpa_account_auth_index_missing")
        headers = {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}
        headers.update(header or {})
        request = {"auth_index": str(index), "method": method, "url": url, "header": headers}
        if data is not None:
            request["data"] = json.dumps(data)
        route = "requests/api-call" if self.prefix.startswith("v8") else "api-call"
        result = await self._request("POST", f"{self.prefix}/{route}", payload=request)
        if result.get("status_code", result.get("statusCode")) != 200:
            raise CPAUsageError("cpa_provider_quota_unavailable")
        body = result.get("body")
        try:
            body = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            raise CPAUsageError("cpa_invalid_quota_response") from None
        if not isinstance(body, dict):
            raise CPAUsageError("cpa_invalid_quota_response")
        return body

    async def _quota(self, row):
        public = self._public_account(row)
        provider = public["provider"]
        windows = []
        try:
            if row.get("disabled") is True:
                raise CPAUsageError("cpa_account_disabled")
            plugin = row.get("quota_plugin_id")
            if (
                self.prefix.startswith("v8")
                and isinstance(plugin, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plugin)
            ):
                index = row.get("auth_index", row.get("authIndex"))
                if not isinstance(index, (str, int)) or isinstance(index, bool) or not str(index):
                    raise CPAUsageError("cpa_account_auth_index_missing")
                query = httpx.QueryParams({"auth_index": str(index)})
                payload = await self._request(
                    "GET", f"{self.prefix}/plugins/{plugin}/quota?{query}"
                )
                windows = google_windows(payload)
                summary = payload.get("summary")
                public["summary"] = [
                    {"metric": metric["key"], "value": number(metric.get("value"))}
                    for metric in (summary if isinstance(summary, list) else [])[:20]
                    if isinstance(metric, dict)
                    and isinstance(metric.get("key"), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", metric["key"])
                    and number(metric.get("value")) is not None
                ]
                subscription = payload.get("subscription")
                if isinstance(subscription, dict):
                    public["plan"] = plan_name(subscription.get("plan")) or public["plan"]
            elif provider == "codex":
                claims = row.get("id_token") if isinstance(row.get("id_token"), dict) else {}
                account_id = claims.get("chatgpt_account_id", row.get("account_id"))
                header = (
                    {"Chatgpt-Account-Id": account_id}
                    if isinstance(account_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account_id)
                    else {}
                )
                payload = await self._upstream(
                    row, "GET", "https://chatgpt.com/backend-api/wham/usage", header=header
                )
                windows = codex_windows(payload)
                public["plan"] = plan_name(payload.get("plan_type")) or public["plan"]
            elif provider == "claude":
                payload = await self._upstream(
                    row,
                    "GET",
                    "https://api.anthropic.com/api/oauth/usage",
                    header={"anthropic-beta": "oauth-2025-04-20"},
                )
                windows = claude_windows(payload)
            elif provider in {"gemini", "gemini-cli", "antigravity"}:
                project = row.get("project_id", row.get("projectId"))
                if not isinstance(project, str) or not re.fullmatch(
                    r"[A-Za-z0-9:._-]{1,256}", project
                ):
                    raise CPAUsageError("cpa_project_id_unavailable")
                method = (
                    "retrieveUserQuotaSummary" if provider == "antigravity" else "retrieveUserQuota"
                )
                host = (
                    "daily-cloudcode-pa.googleapis.com"
                    if provider == "antigravity"
                    else "cloudcode-pa.googleapis.com"
                )
                payload = await self._upstream(
                    row,
                    "POST",
                    f"https://{host}/v1internal:{method}",
                    data={"project": project},
                    header={"User-Agent": "antigravity/hub/2.23.0 darwin/arm64"}
                    if provider == "antigravity"
                    else None,
                )
                windows = google_windows(payload)
            else:
                raise CPAUsageError("cpa_provider_quota_not_supported")
            public.update(
                windows=windows[:16],
                window_count=len(windows),
                windows_truncated=len(windows) > 16,
                quota_status="ok"
                if any(w["remaining_percent"] is not None for w in windows) or public.get("summary")
                else "unknown",
                source="provider_quota",
                checked_at=datetime.now(UTC).isoformat(),
            )
        except CPAUsageError as error:
            public.update(
                windows=[],
                quota_status="unknown",
                error=str(error),
                checked_at=datetime.now(UTC).isoformat(),
            )
        return public

    async def quotas(self, provider="all", account="", offset=0, limit=5):
        async with self.lock:
            rows = [
                row
                for row in await self._accounts()
                if (provider == "all" or provider_name(row) == provider)
                and (not account or account_ref(row) == account)
            ]
            if account and not rows:
                raise CPAUsageError("cpa_account_not_found")

            async def query(row):
                ref = account_ref(row)
                revision = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
                cached = self.cache.get(ref)
                if cached and cached[1] == revision and time.monotonic() - cached[0] < 60:
                    return {**cached[2], "cached": True}
                data = await self._quota(row)
                self.cache[ref] = (time.monotonic(), revision, data)
                return {**data, "cached": False}

            results = await asyncio.gather(*(query(row) for row in rows[offset : offset + limit]))
            return {
                "accounts": results,
                "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "scope": "订阅账号的共享额度，包含其他客户端使用；不是仅 Fullish 消耗",
                "missing_values": "null 表示未提供，不代表 0% 或 100%；"
                "额度与 CPA 本地路由冷却状态分别展示",
            }

    async def request_usage(self):
        try:
            data = await self._request("GET", "v0/management/usage")
        except CPAUsageError as error:
            if str(error) != "cpa_endpoint_unavailable":
                raise
            return {
                "available": False,
                "reason": "cpa_aggregate_usage_endpoint_unavailable",
                "scope": "新版 CPA 可能不提供累计统计；不会消费 usage-queue 或修改统计设置",
            }
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise CPAUsageError("cpa_invalid_usage_response")
        fields = ("total_requests", "success_count", "failure_count", "total_tokens")
        return {
            "available": True,
            "counts": {k: number(usage.get(k)) for k in fields},
            "scope": "CPA 进程本地累计统计，受统计开关和重启影响；不是订阅剩余额度",
        }

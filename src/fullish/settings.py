"""Explicit application settings; credentials never appear in representations."""

import os
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str = "gemini-3.8-flash-high"
    unix_socket: str | None = None
    timeout: float = 90

    @classmethod
    def from_env(cls) -> Self | None:
        base = os.environ.get("AI_BASE_URL", "").strip().rstrip("/")
        key = os.environ.get("AI_API_KEY", "").strip()
        if not base and not key:
            return None
        if not base or not key:
            raise ValueError("AI_BASE_URL and AI_API_KEY must be configured together")
        url = urlsplit(base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("AI_BASE_URL must be an HTTP(S) URL without embedded credentials")
        socket = os.environ.get("AI_UNIX_SOCKET") or None
        if url.scheme == "http" and not socket and url.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Use HTTPS for a remote AI endpoint")
        model = os.environ.get("AI_MODEL", "gemini-3.8-flash-high").strip()
        if not model:
            raise ValueError("AI_MODEL must not be empty")
        return cls(base, key, model, socket)


@dataclass(frozen=True)
class QQSettings:
    app_id: str
    app_secret: str = field(repr=False)
    allowed_groups: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> Self | None:
        if os.environ.get("QQ_ENABLED", "false").lower() != "true":
            return None
        app_id = os.environ.get("QQ_APP_ID", "").strip()
        secret = os.environ.get("QQ_APP_SECRET", "").strip()
        if not app_id or not secret:
            raise ValueError("QQ_ENABLED=true requires QQ_APP_ID and QQ_APP_SECRET")
        groups = frozenset(
            x.strip() for x in os.environ.get("QQ_ALLOWED_GROUPS", "").split(",") if x.strip()
        )
        return cls(app_id, secret, groups)

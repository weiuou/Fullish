"""Explicit application settings; credentials never appear in representations."""

import os
import re
from dataclasses import dataclass, field
from math import isfinite
from typing import Self
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    api_key: str = field(repr=False)
    model: str = "gemini-3.8-flash-high"
    unix_socket: str | None = None
    timeout: float = 90
    fallback_model: str = "gpt-6-luna"

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
        fallback = os.environ.get("AI_FALLBACK_MODEL", "gpt-6-luna").strip()
        return cls(base, key, model, socket, fallback_model=fallback)


@dataclass(frozen=True)
class ImageGenerationSettings:
    model: str = "gpt-image-2.5"
    timeout: float = 120

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.model) or any(
            part in {"", ".", ".."} for part in self.model.split("/")
        ):
            raise ValueError("AI_IMAGE_MODEL must be a model ID or auto")
        if not isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("AI_IMAGE_TIMEOUT_SECONDS must be greater than 0 and at most 300")

    @classmethod
    def from_env(cls) -> Self | None:
        model = os.environ.get("AI_IMAGE_MODEL", "gpt-image-2.5").strip()
        if model.lower() == "off":
            return None
        try:
            timeout = float(os.environ.get("AI_IMAGE_TIMEOUT_SECONDS", "120"))
        except ValueError:
            raise ValueError("AI_IMAGE_TIMEOUT_SECONDS must be a number") from None
        return cls(model, timeout)


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


@dataclass(frozen=True)
class DeploymentReportSettings:
    group_openid: str
    repository: str

    @classmethod
    def from_env(cls) -> Self | None:
        group = os.environ.get("QQ_DEPLOY_REPORT_GROUP_OPENID", "").strip()
        if not group:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", group):
            raise ValueError(
                "QQ_DEPLOY_REPORT_GROUP_OPENID must be a group OpenID, not a QQ number"
            )
        repository = os.environ.get("DEPLOY_REPORT_REPOSITORY", "weiuou/Fullish").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("DEPLOY_REPORT_REPOSITORY must be owner/repository")
        return cls(group, repository)


@dataclass(frozen=True)
class ToolSettings:
    allowed_groups: frozenset[str]
    deepseek_key: str = field(default="", repr=False)
    sandbox_socket: str | None = None
    sandbox_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Self | None:
        groups = frozenset(
            x.strip() for x in os.environ.get("TOOLS_ALLOWED_GROUPS", "").split(",") if x.strip()
        )
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        socket = os.environ.get("SANDBOX_SOCKET", "").strip() or None
        token = os.environ.get("SANDBOX_TOKEN", "").strip()
        if not groups and not key and not socket and not token:
            return None
        if (not groups and not os.environ.get("GROUP_CONTROL_DIR")) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", group) for group in groups
        ):
            raise ValueError("Tools require explicit TOOLS_ALLOWED_GROUPS OpenIDs")
        if key and (len(key) > 2048 or any(not 33 <= ord(char) <= 126 for char in key)):
            raise ValueError("DEEPSEEK_API_KEY must be a single ASCII token")
        if bool(socket) != bool(token) or (
            socket
            and (not socket.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token))
        ):
            raise ValueError(
                "SANDBOX_SOCKET requires an absolute Unix socket path and SANDBOX_TOKEN (32+ chars)"
            )
        if not key and not socket:
            return None
        return cls(groups, key, socket, token)

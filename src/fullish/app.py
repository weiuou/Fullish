"""Expose process health through the same driver that will host the QQ adapter."""

import os
from functools import lru_cache
from typing import cast

import nonebot
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fullish import __version__
from fullish.conversation import Conversations
from fullish.generation.openai import OpenAIImageGenerator
from fullish.model import ChatModel
from fullish.settings import (
    DeploymentReportSettings,
    ImageGenerationSettings,
    ModelSettings,
    QQSettings,
    ToolSettings,
)
from fullish.telemetry import emit


@lru_cache(maxsize=1)
def create_app() -> FastAPI:
    """Initialize the process-wide NoneBot driver once, without external credentials."""
    model_settings = ModelSettings.from_env()
    qq_settings = QQSettings.from_env()
    report_settings = DeploymentReportSettings.from_env()
    tool_settings = ToolSettings.from_env()
    if tool_settings and not model_settings:
        raise ValueError("Tools require the AI backend")
    if report_settings and (not qq_settings or not model_settings):
        raise ValueError("Deployment reports require QQ and the AI backend")
    if qq_settings and not model_settings:
        raise ValueError("Configure the AI backend before enabling QQ")
    qq_bots = []
    if qq_settings:
        qq_bots = [
            {
                "id": qq_settings.app_id,
                "secret": qq_settings.app_secret,
                "use_websocket": True,
                "intent": {
                    "guilds": False,
                    "guild_members": False,
                    "guild_message_reactions": False,
                    "message_audit": False,
                    "at_messages": False,
                    "c2c_group_at_messages": True,
                },
            }
        ]
    nonebot.init(
        driver="~fastapi+~httpx+~websockets" if qq_settings else "~fastapi",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        fastapi_reload=False,
        qq_bots=qq_bots,
    )
    app = cast(FastAPI, nonebot.get_app())
    driver = nonebot.get_driver()
    app.state.ready = False
    revision = os.environ.get("APP_REVISION", "dev")
    mode = "qq" if qq_settings else "ai_configured" if model_settings else "foundation"
    from fullish.tools import ToolRuntime

    tools = ToolRuntime(tool_settings) if tool_settings else None
    image_settings = ImageGenerationSettings.from_env() if model_settings else None
    image_generator = (
        OpenAIImageGenerator(model_settings, image_settings)
        if model_settings and image_settings
        else None
    )
    model = (
        ChatModel(model_settings, tools=tools, image_generator=image_generator)
        if model_settings
        else None
    )
    app.state.model = model
    queue = None
    if qq_settings and model:
        from fullish.qq import GroupOnlyAdapter, install_group_handler

        driver.register_adapter(GroupOnlyAdapter)
        queue = install_group_handler(
            Conversations(model, allowed_groups=qq_settings.allowed_groups)
        )
        if report_settings:
            from fullish.release_report import DeploymentReporter, install_report_route

            install_report_route(
                app, DeploymentReporter(report_settings, model), qq_settings, revision
            )

    app.state.message_queue = queue

    def qq_connected() -> bool:
        return bool(qq_settings and qq_settings.app_id in nonebot.get_bots())

    @driver.on_startup
    async def mark_ready() -> None:
        app.state.ready = True
        emit("service.started", revision=revision, mode=mode)

    @driver.on_bot_connect
    async def connected() -> None:
        emit("qq.connection.connected")

    @driver.on_bot_disconnect
    async def disconnected() -> None:
        emit("qq.connection.disconnected", level="WARNING")

    @driver.on_shutdown
    async def mark_stopped() -> None:
        app.state.ready = False
        emit("service.stopping", revision=revision)
        if queue:
            await queue.close()
        if model:
            await model.close()

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str | bool]:
        return {"status": "ok", "mode": mode, "qq_connected": qq_connected()}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        # Deploy readiness proves process startup. Dependency outages must not
        # cause repeated rollbacks or model calls on every Docker health probe.
        return JSONResponse(
            status_code=200 if app.state.ready else 503,
            content={
                "status": "ready" if app.state.ready else "not_ready",
                "mode": mode,
                "qq_connected": qq_connected(),
            },
        )

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, str]:
        return {
            "service": "fullish",
            "version": __version__,
            "revision": revision,
            "mode": mode,
        }

    @app.get("/health/dependencies", include_in_schema=False)
    async def dependencies() -> dict[str, str | bool]:
        return {
            "model_configured": model is not None,
            "model": model_settings.model if model_settings else "",
            "fallback_model": model_settings.fallback_model if model_settings else "",
            "model_available": await model.available() if model else False,
            "qq_enabled": qq_settings is not None,
            "qq_connected": qq_connected(),
            "image_generation_enabled": image_generator is not None,
            "image_model": image_settings.model if image_settings else "",
            "tools_enabled": tools is not None,
            "search_configured": bool(tool_settings and tool_settings.deepseek_key),
            "sandbox_configured": bool(tool_settings and tool_settings.sandbox_socket),
        }

    return app

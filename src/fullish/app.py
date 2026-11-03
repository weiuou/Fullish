"""Assemble Fullish services, resource lifetimes and health endpoints."""

import os
from contextlib import AsyncExitStack
from functools import lru_cache
from pathlib import Path
from typing import cast

import nonebot
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fullish import __version__
from fullish.conversation.store import ConversationStore
from fullish.generation.openai import OpenAIImageGenerator
from fullish.management.groups import GroupManagement
from fullish.management.identity import DeveloperIdentity
from fullish.management.mcp import MCPManagement
from fullish.management.service import ManagementService
from fullish.management.store import ControlStore
from fullish.mcp.store import MCPStore
from fullish.platforms.qq.menu import DeveloperMenu
from fullish.policies.access import AccessPolicy
from fullish.providers.chat import ChatProvider
from fullish.providers.completion import CompletionClient
from fullish.reply.engine import ReplyEngine
from fullish.reply.handler import ChatHandler
from fullish.reply.media import ImageReader
from fullish.reply.postprocess import ReplyPostprocessor
from fullish.reply.service import ConversationService
from fullish.scheduling.generation import GenerationCoordinator
from fullish.scheduling.queue import GroupMessageQueue
from fullish.sending.assets import ReplyAssets
from fullish.sending.service import SendingService
from fullish.settings import (
    DeploymentReportSettings,
    ImageGenerationSettings,
    ModelSettings,
    QQSettings,
    ReplyPostprocessSettings,
    ToolSettings,
    WalletSettings,
)
from fullish.telemetry import emit


@lru_cache(maxsize=1)
def create_app() -> FastAPI:
    """Initialize the process-wide NoneBot driver once, without external credentials."""
    model_settings = ModelSettings.from_env()
    qq_settings = QQSettings.from_env()
    report_settings = DeploymentReportSettings.from_env()
    tool_settings = ToolSettings.from_env()
    wallet_settings = WalletSettings.from_env()
    reply_settings = ReplyPostprocessSettings.from_env(default_enabled=model_settings is not None)
    if reply_settings and not model_settings:
        raise ValueError("Reply postprocessing requires the AI backend")
    if (tool_settings or wallet_settings) and not model_settings:
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
    from fullish.mcp.runtime import MCPRegistry
    from fullish.tools.runtime import ToolRuntime
    from fullish.wallet.client import WalletMCPClient

    resources = AsyncExitStack()
    control = None
    policies = None
    mcp_store = None
    if qq_settings and (directory := os.environ.get("GROUP_CONTROL_DIR")):
        control_store = ControlStore(Path(directory))
        policies = control_store.policies
        mcp_store = MCPStore(Path(directory))
        control = ManagementService(
            control_store,
            DeveloperIdentity(control_store),
            GroupManagement(policies),
            MCPManagement(mcp_store),
        )
    if mcp_store and model_settings:
        from fullish.integrations.cpa.server import auto_register

        if auto_register(mcp_store):
            emit("tool.mcp.registered", server="cpa_usage")
    mcp = MCPRegistry(mcp_store) if mcp_store and model_settings else None
    wallet = WalletMCPClient(wallet_settings) if wallet_settings else None
    if mcp:
        resources.push_async_callback(mcp.close)
    if wallet:
        resources.push_async_callback(wallet.close)
    access = AccessPolicy(
        policies=policies,
        allowed_groups=qq_settings.allowed_groups if qq_settings else frozenset(),
        notification_targets={"deployment": frozenset({report_settings.group_openid})}
        if report_settings
        else None,
    )
    tools = (
        ToolRuntime(
            tool_settings or ToolSettings(frozenset()),
            policies=policies,
            wallet=wallet,
            mcp=mcp,
            owns_integrations=False,
        )
        if tool_settings or wallet_settings or mcp
        else None
    )
    image_settings = ImageGenerationSettings.from_env() if model_settings else None
    image_generator = (
        OpenAIImageGenerator(model_settings, image_settings)
        if model_settings and image_settings
        else None
    )
    provider = ChatProvider(model_settings) if model_settings else None
    reader = ImageReader() if provider else None
    model = (
        ReplyEngine(provider, reader, tools=tools, image_generator=image_generator, access=access)
        if provider and reader
        else None
    )
    for resource in (tools, image_generator, provider, reader):
        if resource is not None:
            resources.push_async_callback(resource.close)
    app.state.model = model
    expression_backend = (
        CompletionClient(model_settings, timeout=reply_settings.timeout)
        if model_settings and reply_settings
        else None
    )
    if expression_backend:
        resources.push_async_callback(expression_backend.close)
    postprocessor = (
        ReplyPostprocessor(
            model_settings,
            reply_settings,
            provider=expression_backend,
            assets=ReplyAssets.from_file(reply_settings.assets_file)
            if reply_settings.assets_file
            else ReplyAssets(),
        )
        if model_settings and reply_settings
        else None
    )
    app.state.reply_postprocessor = postprocessor
    app.state.group_control = control
    queue = None
    if qq_settings and model:
        from fullish.platforms.qq.adapter import GroupOnlyAdapter, install_group_handler

        conversations = ConversationService(
            model,
            store=ConversationStore(),
            access=access,
            generation=GenerationCoordinator(),
            observe=control.observe if control else None,
            postprocessor=postprocessor,
        )
        sending = SendingService(access, confirmed=conversations.store.sent)
        queue = GroupMessageQueue()
        driver.register_adapter(GroupOnlyAdapter)
        install_group_handler(
            ChatHandler(conversations, sending),
            developer=DeveloperMenu(control, mcp=mcp) if control else None,
            queue=queue,
        )
        if report_settings and provider:
            from fullish.notifications.report import DeploymentReporter, install_report_route

            install_report_route(
                app,
                DeploymentReporter(report_settings, provider, queue=queue, sending=sending),
                qq_settings,
                revision,
            )
        app.state.conversations = conversations
        app.state.sending = sending

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
        try:
            if queue:
                await queue.close()
        finally:
            await resources.aclose()

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
            "model_available": await provider.available() if provider else False,
            "qq_enabled": qq_settings is not None,
            "qq_connected": qq_connected(),
            "image_generation_enabled": image_generator is not None,
            "image_model": image_settings.model if image_settings else "",
            "tools_enabled": tools is not None,
            "search_configured": bool(tool_settings and tool_settings.deepseek_key),
            "sandbox_configured": bool(tool_settings and tool_settings.sandbox_socket),
            "wallet_enabled": bool(tools and tools.wallet),
            "wallet_address": tools.wallet.address if tools and tools.wallet else "",
            "developer_menu_enabled": control is not None,
            "reply_postprocessing_enabled": postprocessor is not None,
            "reply_postprocessing_model": postprocessor.model if postprocessor else "",
        }

    return app

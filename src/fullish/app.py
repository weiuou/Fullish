"""Expose process health through the same driver that will host the QQ adapter."""

import os
from functools import lru_cache
from typing import cast

import nonebot
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fullish import __version__


@lru_cache(maxsize=1)
def create_app() -> FastAPI:
    """Initialize the process-wide NoneBot driver once, without external credentials."""
    nonebot.init(
        driver="~fastapi",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        fastapi_reload=False,
    )
    app = cast(FastAPI, nonebot.get_app())
    driver = nonebot.get_driver()
    app.state.ready = False
    revision = os.environ.get("APP_REVISION", "dev")

    @driver.on_startup
    async def mark_ready() -> None:
        app.state.ready = True

    @driver.on_shutdown
    async def mark_stopped() -> None:
        app.state.ready = False

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str | bool]:
        return {"status": "ok", "mode": "foundation", "qq_connected": False}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        # Readiness currently proves only that the infrastructure probe has started.
        # QQ, model and storage checks belong here when those integrations exist.
        return JSONResponse(
            status_code=200 if app.state.ready else 503,
            content={
                "status": "ready" if app.state.ready else "not_ready",
                "mode": "foundation",
                "qq_connected": False,
            },
        )

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, str]:
        return {
            "service": "fullish",
            "version": __version__,
            "revision": revision,
            "mode": "foundation",
        }

    return app

"""Authenticated deployment-report entry point using shared coordination and delivery."""

import argparse
import asyncio
import hashlib
import hmac
import os
import re
import sys
import time
from typing import Annotated

import httpx
import nonebot
from fastapi import FastAPI, Header, HTTPException
from nonebot.adapters.qq import Bot

from fullish.notifications.deployment import (
    SHA_PATTERN,
    DeploymentSummary,
    ReportError,
    ReportRequest,
    TextModel,
)
from fullish.scheduling.queue import GroupMessageQueue
from fullish.sending.qq import QQTransport
from fullish.sending.service import DeliveryRequest, SendingService
from fullish.settings import DeploymentReportSettings, QQSettings
from fullish.telemetry import emit, reference

REPORT_TIMEOUT = 240


def report_token(secret: str) -> str:
    return hmac.new(secret.encode(), b"fullish-deployment-report-v1", hashlib.sha256).hexdigest()


class DeploymentReporter:
    def __init__(
        self,
        settings: DeploymentReportSettings,
        model: TextModel,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        queue: GroupMessageQueue,
        sending: SendingService,
    ):
        self.settings = settings
        self.summary = DeploymentSummary(settings, model, transport=transport)
        self.queue = queue
        self.sending = sending
        self.lock = asyncio.Lock()

    async def send(self, request: ReportRequest, bot: Bot) -> str:
        async with self.lock, asyncio.timeout(REPORT_TIMEOUT):
            return await self.queue.execute(
                self.settings.group_openid,
                "deployment:" + request.current,
                lambda: self._send(request, bot),
            )

    async def _send(self, request: ReportRequest, bot: Bot) -> str:
        # Check before summary generation as well as immediately before delivery.
        transport = QQTransport(bot, self.settings.group_openid)
        if not await transport.allowed():
            raise ReportError("proactive_messages_disabled")
        text = await self.summary.build(request)
        result = await self.sending.deliver(
            DeliveryRequest(
                self.settings.group_openid, text, source="deployment", mode="proactive"
            ),
            transport,
        )
        if not result.confirmed:
            raise ReportError(result.reason or "report_not_sent")
        emit(
            "deployment.report.sent",
            revision=request.current,
            group_ref=reference(self.settings.group_openid),
        )
        return "sent"


def install_report_route(
    app: FastAPI,
    reporter: DeploymentReporter,
    qq: QQSettings,
    revision: str,
) -> None:
    expected = report_token(qq.app_secret)

    @app.post("/internal/deployment-report", include_in_schema=False)
    async def report(
        request: ReportRequest, authorization: Annotated[str, Header()] = ""
    ) -> dict[str, str]:
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + expected).encode()):
            raise HTTPException(403, "Invalid deployment report token")
        if request.current != revision:
            raise HTTPException(409, "Running revision does not match the deployed revision")
        try:
            bot = nonebot.get_bot(qq.app_id)
        except KeyError:
            raise HTTPException(503, "QQ is not connected") from None
        if not isinstance(bot, Bot):
            raise HTTPException(503, "QQ is not connected")
        try:
            outcome = await reporter.send(request, bot)
            return {"outcome": outcome}
        except Exception as error:
            emit(
                "deployment.report.failed",
                level="WARNING",
                error_type=type(error).__name__,
                error_code=str(error) if isinstance(error, ReportError) else None,
            )
            raise HTTPException(502, "Deployment report failed; inspect safe flow logs") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", default="")
    parser.add_argument("--current", required=True)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if DeploymentReportSettings.from_env() is None:
        print("Deployment report disabled (no target group configured).")
        return 0
    qq = QQSettings.from_env()
    if (
        not qq
        or not re.fullmatch(SHA_PATTERN, args.current)
        or (args.previous and not re.fullmatch(SHA_PATTERN, args.previous))
    ):
        print("Invalid deployment report configuration.", file=sys.stderr)
        return 1
    try:
        deadline = time.monotonic() + 40
        url = f"http://127.0.0.1:{int(os.environ.get('PORT', '8080'))}/internal/deployment-report"
        with httpx.Client(timeout=REPORT_TIMEOUT + 10, trust_env=False) as client:
            while True:
                response = client.post(
                    url,
                    headers={"Authorization": "Bearer " + report_token(qq.app_secret)},
                    json={
                        "previous": args.previous,
                        "current": args.current,
                        "rollback": args.rollback,
                    },
                )
                if response.status_code != 503 or time.monotonic() >= deadline:
                    break
                # No report was started while QQ is disconnected. Never retry an ambiguous send.
                time.sleep(2)
            response.raise_for_status()
            if response.json().get("outcome") != "sent":
                raise ReportError("report_not_sent")
        print("Deployment report accepted by QQ.")
        return 0
    except Exception as error:
        print(f"Deployment report failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

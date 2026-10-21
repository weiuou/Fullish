"""Deployment reports use the actual release range and never invent a successful summary."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from nonebot.adapters.qq import Bot
from pydantic import ValidationError

from fullish.model import ChatModel
from fullish.release_report import (
    DeploymentReporter,
    GitHubHistory,
    ReportError,
    ReportRequest,
    install_report_route,
    report_token,
    summarize,
)
from fullish.settings import DeploymentReportSettings, ModelSettings, QQSettings

BASE, FIRST, CURRENT = "a" * 40, "b" * 40, "c" * 40
GROUP = "d" * 32


def history_transport(request: httpx.Request) -> httpx.Response:
    if "/compare/" in request.url.path:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "status": "ahead",
                "total_commits": 2,
                "commits": [{"sha": FIRST if page == 1 else CURRENT}],
            },
        )
    sha = request.url.path.rsplit("/", 1)[1]
    return httpx.Response(
        200,
        json={
            "sha": sha,
            "commit": {
                "message": "add feature\n\nDetailed body" if sha == FIRST else "revert feature"
            },
            "files": [
                {
                    "filename": "src/example.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "+feature" if sha == FIRST else "-feature",
                }
            ],
        },
    )


def test_all_commit_pages_and_intermediate_diffs_are_read() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(history_transport)) as client:
            commits = await GitHubHistory(client, "weiuou/Fullish").between(BASE, CURRENT)
        assert [commit["sha"] for commit in commits] == [FIRST, CURRENT]
        assert "Detailed body" in commits[0]["message"]
        assert commits[0]["files"][0]["patch"] == "+feature"
        assert commits[1]["files"][0]["patch"] == "-feature"

    asyncio.run(run())


def test_commit_file_pagination_keeps_every_file() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 1
        return httpx.Response(
            200,
            json={
                "sha": CURRENT,
                "commit": {"message": "many files"},
                "files": [{"filename": f"{page}-{i}.bin", "status": "added"} for i in range(count)],
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            commit = await GitHubHistory(client, "weiuou/Fullish").commit(CURRENT)
        assert len(commit["files"]) == 101
        assert not any(item["diff_available"] for item in commit["files"])

    asyncio.run(run())


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_unexpected_history_is_not_misrepresented_as_new_commits(status: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": status}))
        ) as client:
            with pytest.raises(ReportError, match="non_forward_history"):
                await GitHubHistory(client, "weiuou/Fullish").between(BASE, CURRENT)

    asyncio.run(run())


def test_large_inputs_are_all_summarized_without_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fullish.release_report.CHUNK_CHARS", 80)
    inputs = [{"sha": FIRST, "message": "X" * 350}, {"sha": CURRENT, "message": "Y" * 350}]
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return "摘要"

    assert asyncio.run(summarize(inputs, complete)) == "摘要"
    chunks = [text.partition("\n")[2] for text in calls if text.startswith("完整变更资料")]
    assert "".join(chunks) == "\n".join(json.dumps(item, ensure_ascii=False) for item in inputs)


@pytest.mark.parametrize(
    "case", ["success", "model_failure", "same_revision", "rollback", "disabled"]
)
def test_report_delivery_and_failure_boundaries(case: str) -> None:
    async def run() -> None:
        model = ChatModel(ModelSettings("http://localhost/v1", "test-secret"))
        reporter = DeploymentReporter(
            DeploymentReportSettings(GROUP, "weiuou/Fullish"),
            model,
            transport=httpx.MockTransport(history_transport),
        )
        reporter.complete = AsyncMock(return_value="新增功能，并修正处理流程。")
        if case == "model_failure":
            reporter.complete.side_effect = RuntimeError("upstream-private-body")
        bot = Mock(spec=Bot)
        bot.get_group_bot_state = AsyncMock(
            return_value=SimpleNamespace(allow_proactive_msg=case != "disabled")
        )
        bot.post_group_messages = AsyncMock()
        request = ReportRequest(
            previous=CURRENT if case == "same_revision" else BASE,
            current=CURRENT,
            rollback=case == "rollback",
        )
        try:
            if case == "disabled":
                with pytest.raises(ReportError, match="proactive_messages_disabled"):
                    await reporter.send(request, bot)
                reporter.complete.assert_not_awaited()
                bot.post_group_messages.assert_not_awaited()
                return
            if case == "rollback":
                # Reverse the requested range to summarize the commits being removed.
                with patch.object(GitHubHistory, "between", new_callable=AsyncMock) as between:
                    between.return_value = [{"sha": FIRST, "message": "feature", "files": []}]
                    assert await reporter.send(request, bot) == "sent"
                    between.assert_awaited_once_with(CURRENT, BASE)
            else:
                assert await reporter.send(request, bot) == "sent"
            kwargs = bot.post_group_messages.call_args.kwargs
            assert kwargs["group_openid"] == GROUP and kwargs["msg_type"] == 0
            assert "msg_id" not in kwargs  # deployment reports are active messages
            assert CURRENT in kwargs["content"]
            if case == "model_failure":
                assert "暂时生成失败" in kwargs["content"]
                assert "upstream-private-body" not in kwargs["content"]
            elif case == "same_revision":
                assert "没有新增 commit" in kwargs["content"]
                reporter.complete.assert_not_awaited()
            elif case == "rollback":
                assert "回滚撤销" in kwargs["content"]
            else:
                assert "2 个新 commit" in kwargs["content"]
                material = reporter.complete.call_args.args[0]
                assert "+feature" in material and "-feature" in material
        finally:
            await model.close()

    asyncio.run(run())


def test_internal_route_requires_authentication_matching_revision_and_connected_qq() -> None:
    async def run() -> None:
        app = FastAPI()
        reporter = Mock(spec=DeploymentReporter)
        reporter.send = AsyncMock(return_value="sent")
        install_report_route(app, reporter, QQSettings("app-id", "private-secret"), CURRENT)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            payload = {"previous": BASE, "current": CURRENT}
            assert (
                await client.post("/internal/deployment-report", json=payload)
            ).status_code == 403
            headers = {"Authorization": "Bearer " + report_token("private-secret")}
            wrong = await client.post(
                "/internal/deployment-report", json={**payload, "current": FIRST}, headers=headers
            )
            assert wrong.status_code == 409
            with patch("fullish.release_report.nonebot.get_bot", side_effect=KeyError):
                offline = await client.post(
                    "/internal/deployment-report", json=payload, headers=headers
                )
                assert offline.status_code == 503
            reporter.send.assert_not_awaited()
            with patch("fullish.release_report.nonebot.get_bot", return_value=Mock(spec=Bot)):
                sent = await client.post(
                    "/internal/deployment-report", json=payload, headers=headers
                )
                assert sent.json() == {"outcome": "sent"}
        reporter.send.assert_awaited_once()

    asyncio.run(run())


def test_report_configuration_and_revisions_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QQ_DEPLOY_REPORT_GROUP_OPENID", raising=False)
    assert DeploymentReportSettings.from_env() is None
    monkeypatch.setenv("QQ_DEPLOY_REPORT_GROUP_OPENID", "123456789")
    with pytest.raises(ValueError, match="OpenID"):
        DeploymentReportSettings.from_env()
    monkeypatch.setenv("QQ_DEPLOY_REPORT_GROUP_OPENID", GROUP)
    assert DeploymentReportSettings.from_env() == DeploymentReportSettings(GROUP, "weiuou/Fullish")
    with pytest.raises(ValidationError):
        ReportRequest(previous="main;command", current=CURRENT)

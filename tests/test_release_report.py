"""Deployment reports use the actual release range and never invent a successful summary."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from nonebot.adapters.qq import Bot
from nonebot.drivers import URL
from pydantic import ValidationError

from fullish.notifications.deployment import GitHubHistory, ReportError, ReportRequest, summarize
from fullish.notifications.report import DeploymentReporter, install_report_route, report_token
from fullish.policies.access import AccessPolicy
from fullish.providers.chat import ChatProvider
from fullish.scheduling.queue import GroupMessageQueue
from fullish.sending.qq import proactive_messages_allowed
from fullish.sending.service import DeliveryError, SendingService
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
    monkeypatch.setattr("fullish.notifications.deployment.CHUNK_CHARS", 80)
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
        model_calls = []
        history_calls = []

        def summarize_transport(request):
            model_calls.append(json.loads(request.content)["messages"][-1]["content"])
            if case == "model_failure":
                return httpx.Response(503, text="upstream-private-body")
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "新增功能，并修正处理流程。"}}]}
            )

        def history_boundary(request):
            history_calls.append(request.url.path)
            if case == "rollback" and "/compare/" in request.url.path:
                page = int(request.url.params["page"])
                return httpx.Response(
                    200,
                    json={
                        "status": "ahead",
                        "total_commits": 2,
                        "commits": [{"sha": FIRST if page == 1 else BASE}],
                    },
                )
            return history_transport(request)

        provider = ChatProvider(
            ModelSettings("http://localhost/v1", "test-secret"),
            transport=httpx.MockTransport(summarize_transport),
        )
        queue = GroupMessageQueue()
        sending = SendingService(
            AccessPolicy(notification_targets={"deployment": frozenset({GROUP})})
        )
        reporter = DeploymentReporter(
            DeploymentReportSettings(GROUP, "weiuou/Fullish"),
            provider,
            transport=httpx.MockTransport(history_boundary),
            queue=queue,
            sending=sending,
        )
        bot = Mock(spec=Bot)
        bot.adapter = Mock()
        bot.adapter.get_api_base.return_value = URL("https://api.sgroup.qq.com")
        bot._request = AsyncMock(
            return_value={"allow_proactive_msg": case != "disabled", "recv_msg_setting": ""}
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
                assert not model_calls
                bot.post_group_messages.assert_not_awaited()
                return
            assert await reporter.send(request, bot) == "sent"
            if case == "rollback":
                assert history_calls[0] == f"/repos/weiuou/Fullish/compare/{CURRENT}...{BASE}"
            kwargs = bot.post_group_messages.call_args.kwargs
            assert kwargs["group_openid"] == GROUP and kwargs["msg_type"] == 0
            assert "msg_id" not in kwargs  # deployment reports are active messages
            assert CURRENT in kwargs["content"]
            if case == "model_failure":
                assert "暂时生成失败" in kwargs["content"]
                assert "upstream-private-body" not in kwargs["content"]
            elif case == "same_revision":
                assert "没有新增 commit" in kwargs["content"]
                assert not model_calls
            elif case == "rollback":
                assert "回滚撤销" in kwargs["content"]
            else:
                assert "2 个新 commit" in kwargs["content"]
                material = model_calls[-1]
                assert "+feature" in material and "-feature" in material
        finally:
            await queue.close()
            await provider.close()

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
            with patch("fullish.notifications.report.nonebot.get_bot", side_effect=KeyError):
                offline = await client.post(
                    "/internal/deployment-report", json=payload, headers=headers
                )
                assert offline.status_code == 503
            reporter.send.assert_not_awaited()
            with patch("fullish.notifications.report.nonebot.get_bot", return_value=Mock(spec=Bot)):
                sent = await client.post(
                    "/internal/deployment-report", json=payload, headers=headers
                )
                assert sent.json() == {"outcome": "sent"}
        reporter.send.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("allowed", [True, False, None, "false", 1])
def test_group_permission_ignores_unrelated_enum_values_but_requires_a_boolean(allowed) -> None:
    async def run() -> None:
        bot = Mock(spec=Bot)
        bot.adapter = Mock()
        bot.adapter.get_api_base.return_value = URL("https://api.sgroup.qq.com")
        bot._request = AsyncMock(
            return_value={"allow_proactive_msg": allowed, "recv_msg_setting": ""}
        )
        bot.get_group_bot_state = AsyncMock(side_effect=AssertionError("strict adapter enum used"))
        if isinstance(allowed, bool):
            assert await proactive_messages_allowed(bot, GROUP) is allowed
        else:
            with pytest.raises(DeliveryError, match="invalid_group_permission_response"):
                await proactive_messages_allowed(bot, GROUP)
        request = bot._request.call_args.args[0]
        assert str(request.url) == f"https://api.sgroup.qq.com/v2/groups/{GROUP}/bot_state"
        bot.get_group_bot_state.assert_not_awaited()

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

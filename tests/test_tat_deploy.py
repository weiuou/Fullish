"""Deployment orchestration tests run without the Tencent SDK or credentials."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "tat_deploy.py"
SPEC = importlib.util.spec_from_file_location("tat_deploy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tat
SPEC.loader.exec_module(tat)

IMAGE = "ghcr.io/example/fullish@sha256:" + "a" * 64


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self, responses=(), invoke_error=None):
        self.responses = list(responses)
        self.invoke_error = invoke_error
        self.invocations = []
        self.queries = []

    def InvokeCommand(self, request):  # noqa: N802 - SDK API spelling
        self.invocations.append(request)
        if self.invoke_error:
            raise self.invoke_error
        return SimpleNamespace(InvocationId="inv-example")

    def DescribeInvocationTasks(self, request):  # noqa: N802 - SDK API spelling
        self.queries.append(request)
        response = self.responses.pop(0) if self.responses else []
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(InvocationTaskSet=response)


def task(status="SUCCESS", exit_code: object = 0, **overrides):
    fields = {
        "InvocationId": "inv-example",
        "CommandId": "cmd-example",
        "InstanceId": "ins-example",
        "TaskStatus": status,
        "TaskResult": SimpleNamespace(ExitCode=exit_code, Output="do-not-log-output"),
        "ErrorInfo": "do-not-log-errors",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def run(client, image=IMAGE):
    fake_clock = FakeClock()
    messages = []
    invocation = tat.run_deployment(
        client,
        tat.DeploymentConfig("cmd-example", "ins-example", image, 3, 1),
        request_factory=lambda _name, values: SimpleNamespace(**values),
        clock=fake_clock,
        sleep=fake_clock.sleep,
        emit=messages.append,
    )
    return invocation, messages


def test_waits_for_actual_success_with_zero_exit_code():
    client = FakeClient([[], [task("RUNNING", None)], [task()]])
    invocation, messages = run(client)
    assert invocation == "inv-example"
    assert len(client.invocations) == 1
    assert len(client.queries) == 3
    assert client.invocations[0].CommandId == "cmd-example"
    assert client.invocations[0].InstanceIds == ["ins-example"]
    assert json.loads(client.invocations[0].Parameters) == {"image": IMAGE}
    assert client.queries[0].HideOutput is True
    assert client.queries[0].Filters == [
        {"Name": "invocation-id", "Values": ["inv-example"]},
        {"Name": "instance-id", "Values": ["ins-example"]},
    ]
    assert "exit_code=0" in messages[-1]
    assert "do-not-log" not in "\n".join(messages)


@pytest.mark.parametrize("status", sorted(tat.FAILED_STATUSES))
def test_terminal_failures_fail_the_workflow(status):
    client = FakeClient([[task(status, 1)]])
    with pytest.raises(tat.DeploymentError, match=f"status={status}"):
        run(client)
    assert len(client.invocations) == 1


@pytest.mark.parametrize("exit_code", [None, 1, 137, "0", False])
def test_success_status_alone_cannot_mark_deployment_successful(exit_code):
    with pytest.raises(tat.DeploymentError, match="Deployment failed"):
        run(FakeClient([[task(exit_code=exit_code)]]))


def test_running_task_times_out_without_invoking_again():
    client = FakeClient([[task("RUNNING", None)]] * 3)
    with pytest.raises(tat.DeploymentError, match="Remote execution may continue"):
        run(client)
    assert len(client.invocations) == 1
    assert len(client.queries) == 3


@pytest.mark.parametrize(
    "wrong_field",
    [{"InvocationId": "inv-other"}, {"InstanceId": "ins-other"}, {"CommandId": "cmd-other"}],
)
def test_an_unrelated_successful_task_is_not_accepted(wrong_field):
    client = FakeClient([[task(**wrong_field)]] * 3)
    with pytest.raises(tat.DeploymentError, match="Timed out"):
        run(client)


def test_missing_task_is_not_success():
    client = FakeClient()
    with pytest.raises(tat.DeploymentError, match="Timed out"):
        run(client)
    assert len(client.invocations) == 1


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example/fullish:latest",
        "ghcr.io/example/fullish@sha256:abc",
        "example/fullish@sha256:" + "a" * 64,
        IMAGE + ";echo injected",
        IMAGE + "\n",
        IMAGE.replace("example", "$(whoami)"),
        IMAGE.replace("example", "../example"),
    ],
)
def test_invalid_images_never_reach_api(image):
    client = FakeClient()
    with pytest.raises(tat.DeploymentError, match="FULLISH_IMAGE"):
        run(client, image)
    assert client.invocations == []


def test_ambiguous_invoke_is_not_retried_or_logged():
    client = FakeClient(invoke_error=TimeoutError("secret request payload"))
    with pytest.raises(tat.DeploymentError, match="not retried") as exc:
        run(client)
    assert len(client.invocations) == 1
    assert client.queries == []
    assert "secret request payload" not in str(exc.value)


def test_observation_failure_never_invokes_again():
    client = FakeClient([TimeoutError("secret response")])
    with pytest.raises(tat.DeploymentError, match="remote execution may continue") as exc:
        run(client)
    assert len(client.invocations) == 1
    assert "secret response" not in str(exc.value)


def test_duplicate_matching_tasks_fail_closed():
    with pytest.raises(tat.DeploymentError, match="multiple matching tasks"):
        run(FakeClient([[task(), task()]]))


def test_unknown_status_is_not_logged():
    with pytest.raises(tat.DeploymentError, match="unrecognized status") as exc:
        run(FakeClient([[task("secret unexpected status")]]))
    assert "secret unexpected status" not in str(exc.value)


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(tat.DeploymentError, match="TAT_WAIT_TIMEOUT"):
        tat.config_from_env(
            {
                "TAT_COMMAND_ID": "cmd-example",
                "TAT_INSTANCE_ID": "ins-example",
                "FULLISH_IMAGE": IMAGE,
                "TAT_WAIT_TIMEOUT": timeout,
            }
        )

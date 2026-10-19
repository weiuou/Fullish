"""Invoke one saved Tencent Cloud TAT deployment command and verify its result.

The SDK is loaded only by the executable path, so tests need no cloud credentials
or deployment dependencies. Remote command output is deliberately not logged.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

IMAGE_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}"
)
ACTIVE_STATUSES = {"PENDING", "DELIVERING", "DELIVER_DELAYED", "RUNNING", "CANCELLING"}
FAILED_STATUSES = {
    "DELIVER_FAILED",
    "START_FAILED",
    "FAILED",
    "TIMEOUT",
    "TASK_TIMEOUT",
    "CANCELLED",
    "TERMINATED",
}


class DeploymentError(RuntimeError):
    """A safe-to-log failure message, without API or command output."""


@dataclass(frozen=True)
class DeploymentConfig:
    command_id: str
    instance_id: str
    image: str
    wait_timeout: float = 900
    poll_interval: float = 5

    def validate(self) -> None:
        if not re.fullmatch(r"cmd-[a-z0-9]+", self.command_id):
            raise DeploymentError("TAT_COMMAND_ID must be a saved command ID.")
        if not re.fullmatch(r"(?:ins|lhins|rins)-[a-z0-9]+", self.instance_id):
            raise DeploymentError("TAT_INSTANCE_ID must be a CVM, Lighthouse or TAT instance ID.")
        registry = self.image.split("/", 1)[0]
        if not IMAGE_PATTERN.fullmatch(self.image) or not (
            "." in registry or ":" in registry or registry == "localhost"
        ):
            raise DeploymentError(
                "FULLISH_IMAGE must be a full "
                "registry/repository@sha256:<64 lowercase hex> reference."
            )
        if not math.isfinite(self.wait_timeout) or self.wait_timeout <= 0:
            raise DeploymentError("TAT_WAIT_TIMEOUT must be a positive number of seconds.")
        if not math.isfinite(self.poll_interval) or self.poll_interval <= 0:
            raise DeploymentError("Polling interval must be a positive number of seconds.")


def required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip():
        raise DeploymentError(f"Set {name} to a nonempty value without surrounding whitespace.")
    return value


def config_from_env(env: Mapping[str, str]) -> DeploymentConfig:
    try:
        timeout = float(env.get("TAT_WAIT_TIMEOUT", "900"))
    except ValueError:
        raise DeploymentError("TAT_WAIT_TIMEOUT must be a positive number of seconds.") from None
    config = DeploymentConfig(
        command_id=required_env(env, "TAT_COMMAND_ID"),
        instance_id=required_env(env, "TAT_INSTANCE_ID"),
        image=required_env(env, "FULLISH_IMAGE"),
        wait_timeout=timeout,
    )
    config.validate()
    return config


def sdk_request(name: str, values: dict[str, Any]) -> Any:
    from tencentcloud.tat.v20201028 import models

    request = getattr(models, name)()
    request.from_json_string(json.dumps(values))
    return request


def create_client(env: Mapping[str, str]) -> Any:
    from tencentcloud.common.credential import Credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.common.retry import NoopRetryer
    from tencentcloud.tat.v20201028.tat_client import TatClient

    region = required_env(env, "TENCENTCLOUD_REGION")
    credentials = Credential(
        required_env(env, "TENCENTCLOUD_SECRET_ID"),
        required_env(env, "TENCENTCLOUD_SECRET_KEY"),
        env.get("TENCENTCLOUD_TOKEN") or None,
    )
    profile = ClientProfile(
        httpProfile=HttpProfile(endpoint="tat.tencentcloudapi.com", reqTimeout=30),
        retryer=NoopRetryer(),
        disable_region_breaker=True,
    )
    return TatClient(credentials, region, profile)


def run_deployment(
    client: Any,
    config: DeploymentConfig,
    *,
    request_factory: Callable[[str, dict[str, Any]], Any] = sdk_request,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> str:
    """Invoke exactly once, then observe only the matching instance's task.

    An ambiguous invocation result is never retried: TAT InvokeCommand has no
    idempotency token. Operators must inspect the console before rerunning it.
    """
    config.validate()
    deadline = clock() + config.wait_timeout
    request = request_factory(
        "InvokeCommandRequest",
        {
            "CommandId": config.command_id,
            "InstanceIds": [config.instance_id],
            "Parameters": json.dumps({"image": config.image}),
        },
    )
    try:
        response = client.InvokeCommand(request)
    except Exception:
        raise DeploymentError(
            "InvokeCommand was not confirmed and was not retried. The remote command may "
            "already be running; inspect the Tencent Cloud TAT console before rerunning."
        ) from None
    invocation_id = getattr(response, "InvocationId", None)
    if not isinstance(invocation_id, str) or not re.fullmatch(r"inv-[a-z0-9]+", invocation_id):
        raise DeploymentError(
            "TAT returned no valid invocation ID. The remote command may be running; "
            "inspect the Tencent Cloud TAT console before rerunning."
        )
    emit(f"TAT invocation={invocation_id} instance={config.instance_id} accepted")
    query = request_factory(
        "DescribeInvocationTasksRequest",
        {
            "Filters": [
                {"Name": "invocation-id", "Values": [invocation_id]},
                {"Name": "instance-id", "Values": [config.instance_id]},
            ],
            "HideOutput": True,
            "Limit": 100,
        },
    )
    last_status = None
    while clock() < deadline:
        try:
            response = client.DescribeInvocationTasks(query)
        except Exception:
            raise DeploymentError(
                f"Cannot observe TAT invocation={invocation_id}; remote execution may continue. "
                "Inspect the Tencent Cloud TAT console before rerunning."
            ) from None
        tasks = [
            task
            for task in (getattr(response, "InvocationTaskSet", None) or [])
            if getattr(task, "InvocationId", None) == invocation_id
            and getattr(task, "InstanceId", None) == config.instance_id
            and getattr(task, "CommandId", None) == config.command_id
        ]
        if len(tasks) > 1:
            raise DeploymentError(
                f"TAT invocation={invocation_id} returned multiple matching tasks; "
                "inspect the console."
            )
        status = "NOT_VISIBLE"
        if tasks:
            task = tasks[0]
            status = getattr(task, "TaskStatus", None)
            if status not in ACTIVE_STATUSES | FAILED_STATUSES | {"SUCCESS"}:
                raise DeploymentError(
                    f"TAT invocation={invocation_id} returned an unrecognized status; "
                    "remote execution may continue. Inspect the Tencent Cloud TAT console."
                )
            result = getattr(task, "TaskResult", None)
            exit_code = getattr(result, "ExitCode", None)
            if status == "SUCCESS" or status in FAILED_STATUSES:
                safe_exit_code = exit_code if type(exit_code) is int else "unknown"
                emit(f"TAT invocation={invocation_id} status={status} exit_code={safe_exit_code}")
                if status == "SUCCESS" and type(exit_code) is int and exit_code == 0:
                    return invocation_id
                raise DeploymentError(
                    f"Deployment failed: invocation={invocation_id} status={status} "
                    f"exit_code={safe_exit_code}. Inspect the Tencent Cloud TAT console."
                )
        if status != last_status:
            emit(f"TAT invocation={invocation_id} status={status}")
            last_status = status
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(config.poll_interval, remaining))
    raise DeploymentError(
        f"Timed out observing TAT invocation={invocation_id}. Remote execution may continue; "
        "inspect the Tencent Cloud TAT console before rerunning."
    )


def main() -> int:
    try:
        config = config_from_env(os.environ)
        client = create_client(os.environ)
        run_deployment(client, config)
    except DeploymentError as error:
        print(f"Deployment error: {error}", file=sys.stderr)
        return 1
    except ImportError:
        print(
            "Install the locked deploy dependency group before running this script.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        # SDK exceptions can contain request data. Do not dump them into public CI logs.
        print(
            "Deployment client setup failed; check configuration and installed SDK.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

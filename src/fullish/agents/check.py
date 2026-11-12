"""Pinned Dim and its direct renderer; no model, group sandbox or QQ calls."""

import argparse
import asyncio
import json
import sys

import httpx

from fullish.agents.rpc import DimRPC
from fullish.agents.settings import AgentSettings
from fullish.policies.access import AccessPolicy
from fullish.policies.groups import GroupPolicies


async def check(*, preflight: bool = False) -> None:
    settings = AgentSettings.from_env()
    if not settings or not settings.enabled:
        print("Dim backend disabled; no agent dependency required.")
        return
    rpc = await DimRPC.connect(settings.endpoint, executable=settings.executable)
    try:
        if not settings.visualization:
            print("Pinned Dim private transport passed; no model was invoked.")
            return
        catalog = await rpc.call("plugin.list", {})
        if not isinstance(catalog, list) or not any(
            item.get("name") == "fullish-visualize"
            and item.get("status") == "running"
            and item.get("off") is False
            for item in catalog
        ):
            raise RuntimeError("visualize_plugin_missing")
        result = await rpc.call("visualize.probe", {}, timeout=150)
        cases = result.get("cases", [])
        if (
            result.get("protocol") != 2
            or result.get("execution") != "dim-plugin"
            or len(cases) != 6
            or {case.get("format") for case in cases if case.get("outcome") == "png"}
            != {"svg", "html", "mermaid"}
            or sum(case.get("outcome") == "rejected" for case in cases) != 3
            or any(
                case.get("width") != 1000
                or case.get("height") != 650
                or not isinstance(case.get("bytes"), int)
                or not 1000 < case["bytes"] <= 10 * 1024 * 1024
                for case in cases
                if case.get("outcome") == "png"
            )
        ):
            raise RuntimeError("visualize_direct_probe_failed")
    finally:
        await rpc.close()
    policies = GroupPolicies.from_env()
    if policies:
        policies.read()
    access = AccessPolicy(policies=policies)
    eligible = sum(
        access.group_allowed(group) and access.feature_allowed(group, "image")
        for group in settings.groups
    )
    if not preflight:
        assert settings.bridge_socket and settings.bridge_token_file
        token = settings.bridge_token_file.read_text(encoding="utf-8").strip()
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(settings.bridge_socket)),
            base_url="http://bridge",
            timeout=5,
            trust_env=False,
        ) as client:
            response = await client.post(
                "/invoke",
                headers={"Authorization": "Bearer " + token},
                json={
                    "session_id": "unbound-deployment-probe",
                    "name": "fullish_reply",
                    "arguments": {"text": ""},
                },
            )
            response.raise_for_status()
            if response.json() != {
                "isError": True,
                "content": [{"type": "text", "text": "dim_run_not_bound"}],
            }:
                raise RuntimeError("visualize_bridge_acceptance_failed")
    print(
        json.dumps(
            {
                "runtime": "dim",
                "renderer_protocol": 2,
                "direct_png_checks": 3,
                "rejected_sources": 3,
                "authorized_groups": eligible,
                "model_calls": 0,
                "sandbox_calls": 0,
                "qq_calls": 0,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(check(preflight=args.preflight))
    except Exception:
        print(
            "Dim/visualize acceptance failed; inspect private runtime and direct renderer.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

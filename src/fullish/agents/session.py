"""One-task sessions and final-model-output parsing for Dim alpha.20 protocol v3."""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from fullish.agents.rpc import DimError, DimRPC


def completed_turn(events: list[dict]) -> str:
    terminal = next(
        (
            event
            for event in reversed(events)
            if (event.get("type"), event.get("name")) == ("turn", "end")
        ),
        None,
    )
    if terminal is None or terminal["data"].get("status") != "completed":
        raise DimError("dim_turn_not_completed")
    return terminal["data"]["turn_id"]


def final_text(events: list[dict]) -> str | None:
    turn_id = completed_turn(events)
    completed = next(
        (
            event
            for event in reversed(events)
            if event.get("name") == "model-output-end"
            and event.get("data", {}).get("turn_id") == turn_id
        ),
        None,
    )
    if completed is None or completed["data"].get("status") != "completed":
        raise DimError("dim_final_output_missing")
    if completed["data"].get("finish_reason") not in {"end_turn", "stop_sequence"}:
        raise DimError("dim_final_output_missing")
    request_id = completed["data"]["request_id"]
    text = "".join(
        event["data"]["delta"]
        for event in events
        if event.get("name") == "text" and event.get("data", {}).get("request_id") == request_id
    ).strip()
    if not text:
        raise DimError("dim_empty_final_output")
    return None if text == "<SILENCE>" else text[:1500]


async def history(rpc: DimRPC, session_id: str) -> list[dict[str, Any]]:
    events = []
    after = 0
    through = None
    for _ in range(32):
        request = {"sessionId": session_id, "branchId": "main", "limit": 128, "after": after}
        if through is not None:
            request["through"] = through
        page = await rpc.call("session.history", request)
        through = page["through"]
        events.extend(page["events"])
        if page["done"]:
            return events
        next_after = page["nextAfter"]
        if next_after <= after:
            break
        after = next_after
    raise DimError("dim_history_limit")


async def wait_turn(
    rpc: DimRPC,
    session_id: str,
    *,
    deadline: float,
    allowed: Callable[[], bool] | None = None,
) -> list[dict]:
    """Poll durable terminal facts; an idle RPC or dropped connection alone is insufficient."""
    while time.monotonic() < deadline:
        if allowed and not allowed():
            raise DimError("dim_group_revoked")
        events = await history(rpc, session_id)
        if any((event.get("type"), event.get("name")) == ("turn", "end") for event in events):
            return events
        # Bound the native loop even if a model repeatedly emits invalid calls.
        if sum(event.get("name") == "model-start" for event in events) > 16:
            raise DimError("dim_model_request_budget")
        await asyncio.sleep(0.1)
    raise DimError("dim_turn_timeout")

"""Correlated message-flow metadata; never include message bodies or credentials."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from nonebot import logger

_context: ContextVar[dict[str, str] | None] = ContextVar("message_flow", default=None)


def reference(value: str) -> str:
    return sha256(value.encode()).hexdigest()[:16]


def trace_id() -> str:
    return (_context.get() or {}).get("trace_id", "")


@contextmanager
def message_trace(group_id: str, message_id: str) -> Iterator[None]:
    token = _context.set(
        {
            "trace_id": reference(json.dumps([group_id, message_id]))
            if message_id
            else uuid4().hex,
            "group_ref": reference(group_id) if group_id else "",
        }
    )
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def operation_trace() -> Iterator[None]:
    """Give standalone model calls a trace, preserving an enclosing QQ trace."""
    token = _context.set(_context.get() or {"trace_id": uuid4().hex})
    try:
        yield
    finally:
        _context.reset(token)


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def emit(stage: str, *, level: str = "INFO", **fields: object) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        **(_context.get() or {}),
        "stage": stage,
        **fields,
    }
    logger.bind(flow=record).log(level, "flow {}", json.dumps(record, ensure_ascii=False))

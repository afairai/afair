"""Low-overhead, content-free recall latency diagnostics.

Local time includes SQLite, hydration, ranking and serialization preparation;
it is deliberately not presented as engine-only SQL execution time.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class _Timing:
    embedding_wait_seconds: float = 0
    fallback: bool = False


_timing: ContextVar[_Timing | None] = ContextVar("recall_timing", default=None)
log = structlog.get_logger(__name__)


def record_embedding_wait(seconds: float, *, fallback: bool = False) -> None:
    state = _timing.get()
    if state is not None:
        state.embedding_wait_seconds += seconds
        state.fallback = fallback


def timed_recall[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        state = _Timing()
        token = _timing.set(state)
        start = time.monotonic()
        succeeded = False
        try:
            result = fn(*args, **kwargs)
            succeeded = True
            return result
        finally:
            total = time.monotonic() - start
            _timing.reset(token)
            log.info(
                "recall.timing",
                total_ms=round(total * 1000, 2),
                embedding_wait_ms=round(state.embedding_wait_seconds * 1000, 2),
                local_sqlite_hydration_ms=round(
                    max(0, total - state.embedding_wait_seconds) * 1000, 2
                ),
                semantic_fallback=state.fallback,
                succeeded=succeeded,
            )

    return wrapped

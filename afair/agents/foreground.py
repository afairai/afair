"""Process-local foreground priority: background cycles yield between units.

An already running provider call cannot be preempted. New cold-path work waits
until recall completes, with a short quiet period to avoid racing its followups.
"""

from __future__ import annotations

import threading
import time
from functools import wraps
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
_lock = threading.Lock()
_active = 0
_last_completed = float("-inf")
QUIET_SECONDS = 2.0


def background_should_yield() -> bool:
    with _lock:
        return _active > 0 or time.monotonic() - _last_completed < QUIET_SECONDS


def foreground_request[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        global _active, _last_completed
        with _lock:
            _active += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with _lock:
                _active -= 1
                _last_completed = time.monotonic()

    return wrapped


def clear_foreground_state() -> None:
    """Reset process state when tearing down the server context in tests."""
    global _active, _last_completed
    with _lock:
        _active = 0
        _last_completed = float("-inf")

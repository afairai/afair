"""Content-free diagnostic boundary shared by logs and error reporting.

Free-form strings are private by default, including short provider errors.
Code-owned diagnostic names are the only string exception; never put user
content in event names, exception classes or source-code locations.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "[redacted]"
_SENSITIVE = (
    "authorization",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "cookie",
    "vault",
    "bearer",
    "credential",
    "email",
    "payload",
    "text",
    "prompt",
    "content",
    "query",
    "message",
    "error",
    "exception",
    "event_id",
    "entity_id",
    "cluster_id",
    "content_hash",
    "request_id",
    "principal",
    "customer",
    "user",
    "session",
    "url",
    "path",
    "body",
    "response",
    "input",
    "output",
    "args",
    "params",
    "stack",
)
_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}\Z")
# These keys must contain code-owned names, never interpolated content.
_CODE_FIELDS = frozenset(
    {"event", "level", "logger", "worker", "component", "stage", "failure_class"}
)
_STATUSES = frozenset({"ok", "failed", "success", "error", "timeout", "pending", "keep"})


def key_is_sensitive(key: str) -> bool:
    return any(token in key.lower() for token in _SENSITIVE)


def diagnostic(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Recursively mask content, sequences and objects without invoking repr.

    Depth is bounded so cyclic/adversarial diagnostic payloads fail closed.
    Numeric scalars remain useful for timings, status codes and counters.
    """
    if depth > 12:
        return MASK
    if key_is_sensitive(key):
        return MASK
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in _CODE_FIELDS and _CODE.fullmatch(value):
            return value
        if key in {"status", "ok"} and value in _STATUSES:
            return value
        return MASK
    if isinstance(value, Mapping):
        return {
            str(k): diagnostic(v, key=str(k), depth=depth + 1)
            for k, v in value.items()
            if isinstance(k, str) and _CODE.fullmatch(k)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [diagnostic(v, depth=depth + 1) for v in value]
    return MASK

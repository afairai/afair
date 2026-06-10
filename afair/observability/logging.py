"""Structured logging configuration with built-in credential/PII redaction.

afair is a personal-memory product running on the public internet. The
binding security rule (``~/.claude/rules/security.md``) is: redaction is
built into the logger, never left to each call site to remember. Until
this module existed, ``structlog.get_logger(...)`` ran on the library
default — meaning the moment any call site logged a token, email, or
vault text it landed in cleartext in the Fly log stream.

``configure_logging()`` installs a processor chain that:
  - masks any event-dict key whose name looks like a credential or PII
    field (recursing into nested mappings),
  - truncates over-long string values (vault text / prompts) so a stray
    ``error=<huge blob>`` can't dump a memory into the logs,
  - renders JSON in prod (queryable) and a console renderer in dev.

It is idempotent and safe to call once at boot.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

# Substring match (lowercased) against event-dict keys whose values must
# be masked before the line is emitted.
_SENSITIVE_KEY_TOKENS = (
    "authorization",
    "auth_token",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "cookie",
    "vault_key",
    "bearer",
    "credential",
    "email",
)

# Keys that legitimately carry user/agent text we want for debugging but
# which could be large; we keep them but cap the length.
_MAX_VALUE_CHARS = 512
_MASK = "[redacted]"


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_MASK if _key_is_sensitive(str(k)) else _redact(v)) for k, v in value.items()}
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + "…[truncated]"
    return value


def redact_processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: mask credential-shaped keys, cap long strings."""
    for key, value in list(event_dict.items()):
        if _key_is_sensitive(str(key)):
            event_dict[key] = _MASK
        else:
            event_dict[key] = _redact(value)
    return event_dict


_configured = False


def configure_logging(*, environment: str = "prod") -> None:
    """Install the redacting structlog chain. Idempotent.

    JSON output in prod; human-friendly console output otherwise.
    """
    global _configured
    if _configured:
        return

    is_prod = environment not in ("dev", "local", "test")
    renderer: Any = (
        structlog.processors.JSONRenderer() if is_prod else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, os.environ.get("AFAIR_LOG_LEVEL", "INFO").upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True

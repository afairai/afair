"""Structured logging configuration with built-in credential/PII redaction.

afair is a personal-memory product running on the public internet. The
binding security rule (``~/.claude/rules/security.md``) is: redaction is
built into the logger, never left to each call site to remember. Until
this module existed, ``structlog.get_logger(...)`` ran on the library
default — meaning the moment any call site logged a token, email, or
vault text it landed in cleartext in the Fly log stream.

``configure_logging()`` installs a final privacy boundary after exception and
stack formatting. Free-form strings and private identifiers are masked, including
nested sequences; numeric diagnostics and code-owned event names remain. Both
production JSON and development console output use the same policy.

It is idempotent and safe to call once at boot.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger

from .privacy import diagnostic, key_is_sensitive

_key_is_sensitive = key_is_sensitive


def exception_class_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Preserve the exception class, never its message or provider repr."""
    info = event_dict.get("exc_info")
    error = info if isinstance(info, BaseException) else None
    if isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], BaseException):
        error = info[1]
    elif info is True:
        error = sys.exc_info()[1]
    if error is not None:
        event_dict["failure_class"] = type(error).__name__
    return event_dict


def redact_processor(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Final processor before rendering: no raw content or exception objects."""
    cleaned = diagnostic(event_dict)
    event_dict.clear()
    event_dict.update(cleaned)
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
            exception_class_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, os.environ.get("AFAIR_LOG_LEVEL", "INFO").upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True

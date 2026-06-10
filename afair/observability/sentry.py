"""Sentry initialization for the afair MCP server.

DSN is read from ``SENTRY_DSN``. If unset, init is a no-op so local
development and self-hosted instances do not phone home unless the
operator explicitly opts in.

Sampling stays low (10% traces) because the cold-path LLM-calling
workers create plenty of internal traffic; we want errors and a
representative sample, not every span.

EU region is implied by the DSN host (``de.sentry.io``); the SDK
honours that automatically.

Privacy posture (binding): afair is a personal-memory product. The
single most sensitive data in the system is the user's raw vault text,
and the most sensitive secrets (provider API keys, the auth bearer,
the vault key) live in worker stack frames and request headers. So we
do NOT send PII, do NOT attach local variables, and run a defensive
``before_send`` scrubber that strips request headers/cookies/body and
masks any extra/context value whose key looks like a credential. An
exception in the extractor must never ship a private memory or a live
key to a third-party error tracker.
"""

from __future__ import annotations

import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.starlette import StarletteIntegration

# Substring match (case-insensitive) against context/extra keys whose
# values must be masked before an event leaves the process.
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
    "payload",
    "text",
    "prompt",
)

_MASK = "[redacted]"


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def _scrub_mapping(data: dict[str, Any]) -> None:
    """Mask sensitive values in-place, recursing into nested dicts."""
    for key, value in list(data.items()):
        if _key_is_sensitive(str(key)):
            data[key] = _MASK
        elif isinstance(value, dict):
            _scrub_mapping(value)


def _before_send(event: Any, _hint: Any) -> Any:
    """Strip request internals and mask credential-shaped fields.

    Defense-in-depth on top of ``send_default_pii=False`` /
    ``include_local_variables=False``: even if a future code path attaches
    request data or sets an ``extra`` containing vault text, it is masked
    here before transmission.
    """
    request = event.get("request")
    if isinstance(request, dict):
        # Headers and cookies carry the bearer + session; the body can be
        # raw vault content. None of it belongs in an error report.
        request.pop("headers", None)
        request.pop("cookies", None)
        request.pop("data", None)

    for section in ("extra", "contexts", "tags"):
        value = event.get(section)
        if isinstance(value, dict):
            _scrub_mapping(value)

    # Belt-and-braces: drop any frame-local vars the SDK may still attach.
    exception = event.get("exception")
    if isinstance(exception, dict):
        for entry in exception.get("values", []):
            stacktrace = entry.get("stacktrace") if isinstance(entry, dict) else None
            if isinstance(stacktrace, dict):
                for frame in stacktrace.get("frames", []):
                    if isinstance(frame, dict):
                        frame.pop("vars", None)

    return event


def init_sentry() -> None:
    """Initialize Sentry if ``SENTRY_DSN`` is configured. No-op otherwise.

    Safe to call multiple times — the SDK guards against double init.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    sentry_sdk.init(
        dsn=dsn,
        # Personal-memory product: never attach IP/headers/cookies/body,
        # and never serialize stack-frame locals (they hold prompts, vault
        # text, and provider keys). See module docstring.
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=0.1,
        before_send=_before_send,
        # Starlette covers the FastMCP HTTP surface + all our routes.
        # We avoid the FastAPI integration because we mount via Starlette
        # directly, and double-init produces noisy log lines.
        integrations=[StarletteIntegration()],
        environment=os.environ.get("AFAIR_ENVIRONMENT", "prod"),
        release=os.environ.get("FLY_IMAGE_REF") or os.environ.get("GIT_SHA"),
    )

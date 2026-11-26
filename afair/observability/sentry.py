"""Sentry initialization for the afair MCP server.

DSN is read from ``SENTRY_DSN``. If unset, init is a no-op so local
development and self-hosted instances do not phone home unless the
operator explicitly opts in.

Only content-free errors are sent; performance tracing is disabled because
span descriptions and provider attributes may contain vault content.

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
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.starlette import StarletteIntegration

from .privacy import MASK, diagnostic, key_is_sensitive

_key_is_sensitive = key_is_sensitive


def _before_breadcrumb(crumb: Any, _hint: Any) -> Any:
    """Breadcrumb messages, URLs, HTTP/SQL data and provider bodies are private."""
    return diagnostic(crumb)


def _frames(stacktrace: Any) -> dict[str, Any]:
    """Keep source locations, never locals, absolute paths or source text."""
    if not isinstance(stacktrace, dict):
        return {"frames": []}
    frames = []
    for frame in stacktrace.get("frames", []):
        if not isinstance(frame, dict):
            continue
        clean = {
            k: frame[k]
            for k in ("lineno", "colno", "in_app")
            if isinstance(frame.get(k), (int, bool))
        }
        for key in ("function", "module"):
            value = frame.get(key)
            # Source-code symbols only, never repr, vars, paths or context lines.
            if isinstance(value, str) and value.replace(".", "_").isidentifier():
                clean[key] = value[:128]
        frames.append(clean)
    return {"frames": frames}


def _before_send(event: Any, _hint: Any) -> Any:
    """Allow content-free incident metadata; fail closed on unknown fields.

    Filtering precedes sanitization so the established narrow uvicorn
    disconnect-noise rule continues to work on the original message.
    """
    if event.get("logger") == "uvicorn.error":
        logentry = event.get("logentry") or {}
        message = logentry.get("message") or event.get("message") or ""
        if "ASGI callable returned without completing response" in str(message):
            return None

    # Rebuild rather than chasing every SDK/provider extension field. SDK event
    # ids are error-tracker ids, distinct from private vault event identifiers.
    clean = {k: event[k] for k in ("event_id", "timestamp", "platform", "level") if k in event}
    if "event_id" in clean and not (
        isinstance(clean["event_id"], str) and re.fullmatch(r"[0-9a-f]{32}", clean["event_id"])
    ):
        clean["event_id"] = MASK
    for key in ("logger", "extra", "contexts", "tags", "breadcrumbs"):
        if key in event:
            clean[key] = diagnostic(event[key], key="logger" if key == "logger" else "")
    if "message" in event:
        clean["message"] = MASK
    if "logentry" in event:
        clean["logentry"] = {"message": MASK}
    if "transaction" in event:
        clean["transaction"] = MASK
    # Only accept deployment-controlled release dimensions.
    deployment_env = os.environ.get("AFAIR_ENVIRONMENT", "prod")
    if event.get("environment") == deployment_env and deployment_env in {
        "prod",
        "production",
        "dev",
        "staging",
        "test",
        "local",
        "fly",
    }:
        clean["environment"] = deployment_env
    release = event.get("release")
    if isinstance(release, str) and (
        re.fullmatch(r"[0-9a-f]{40}", release) or re.fullmatch(r"\d+\.\d+\.\d+", release)
    ):
        clean["release"] = release
    # Omit request/user/thread/module/server sections altogether: substituting
    # strings for their SDK mapping schemas would create invalid incidents.
    exception = event.get("exception")
    if isinstance(exception, dict):
        entries = []
        for entry in exception.get("values", []):
            if not isinstance(entry, dict):
                continue
            item: dict[str, Any] = {"value": MASK}
            for key in ("type", "module"):
                value = entry.get(key)
                if isinstance(value, str) and value.replace(".", "_").isidentifier():
                    item[key] = value[:128]
            if "stacktrace" in entry:
                item["stacktrace"] = _frames(entry["stacktrace"])
            entries.append(item)
        clean["exception"] = {"values": entries}
    if "stacktrace" in event:
        clean["stacktrace"] = _frames(event["stacktrace"])
    event.clear()
    event.update(clean)
    return event


def init_sentry() -> None:
    """Initialize Sentry if ``SENTRY_DSN`` is configured. No-op otherwise.

    Safe to call multiple times — the SDK guards against double init.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    from afair import __version__

    sentry_sdk.init(
        dsn=dsn,
        # Personal-memory product: never attach IP/headers/cookies/body,
        # and never serialize stack-frame locals (they hold prompts, vault
        # text, and provider keys). See module docstring.
        send_default_pii=False,
        include_local_variables=False,
        # Error-only telemetry: transactions/spans carry URLs and provider data.
        traces_sample_rate=0.0,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        # Starlette covers the FastMCP HTTP surface + all our routes.
        # We avoid the FastAPI integration because we mount via Starlette
        # directly, and double-init produces noisy log lines.
        integrations=[StarletteIntegration()],
        environment=os.environ.get("AFAIR_ENVIRONMENT", "prod"),
        release=os.environ.get("GIT_SHA") or __version__,
    )

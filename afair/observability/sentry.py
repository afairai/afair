"""Sentry initialization for the afair MCP server.

DSN is read from ``SENTRY_DSN``. If unset, init is a no-op so local
development and self-hosted instances do not phone home unless the
operator explicitly opts in.

Sampling stays low (10% traces) because the cold-path LLM-calling
workers create plenty of internal traffic; we want errors and a
representative sample, not every span.

EU region is implied by the DSN host (``de.sentry.io``); the SDK
honours that automatically.
"""

from __future__ import annotations

import os

import sentry_sdk
from sentry_sdk.integrations.starlette import StarletteIntegration


def init_sentry() -> None:
    """Initialize Sentry if ``SENTRY_DSN`` is configured. No-op otherwise.

    Safe to call multiple times — the SDK guards against double init.
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=True,
        traces_sample_rate=0.1,
        # AI cold-path workers can fail in dozens of ways. Capture stack
        # traces with locals so we see the prompt that broke.
        include_local_variables=True,
        # Starlette covers the FastMCP HTTP surface + all our routes.
        # We avoid the FastAPI integration because we mount via Starlette
        # directly, and double-init produces noisy log lines.
        integrations=[StarletteIntegration()],
        environment=os.environ.get("AFAIR_ENVIRONMENT", "prod"),
        release=os.environ.get("FLY_IMAGE_REF") or os.environ.get("GIT_SHA"),
    )

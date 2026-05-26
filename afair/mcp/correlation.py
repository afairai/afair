"""Correlation-ID middleware — per-request trace anchor.

Every incoming request gets a request id (minted fresh or accepted from
an upstream ``X-Request-ID`` header) that:

  1. Becomes a structlog contextvar — every log line emitted while
     handling the request carries ``request_id=...``.
  2. Echoes back to the client in the ``X-Request-ID`` response header
     so the caller can correlate their side with our side.
  3. Survives across the async-handler call chain because structlog's
     contextvars binding is task-local.

Why this matters: when a user says "recall was slow at 14:23", we can
ask them for the request id from the response header and grep that one
key across the logs. Without it, finding the right invocation among
hundreds requires guesswork.

Foundation for the eventual Sentry integration — Sentry's transaction
id slot will use the same value so server-side errors map back to the
client's request id.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response


_HEADER = "X-Request-ID"
_MAX_INCOMING_LEN = 128
"""Caps incoming request-id length so a malicious client can't blow up
log fields with a megabyte-long header value."""


def _mint_request_id() -> str:
    """Compact, URL-safe, 128 bits of entropy. Format chosen to be
    grep-friendly and not visually overwhelming in log lines."""
    return secrets.token_urlsafe(16)


def _accept_or_mint(provided: str | None) -> str:
    if provided is None:
        return _mint_request_id()
    cleaned = provided.strip()
    if not cleaned or len(cleaned) > _MAX_INCOMING_LEN:
        return _mint_request_id()
    # Only ASCII printable, no whitespace, no control chars.
    if not all(0x20 < ord(c) < 0x7F for c in cleaned):
        return _mint_request_id()
    return cleaned


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a request id to structlog's contextvars for the duration of
    one request, and echo it back in the response.

    Should sit FAR OUT in the middleware stack — outermost is best —
    so even errors thrown by other middlewares carry the id in their
    log output.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _accept_or_mint(request.headers.get(_HEADER))

        # structlog.contextvars.bind_contextvars is task-local. Cleanup
        # at the end of the request to keep the binding from leaking
        # across event-loop iterations.
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        response.headers[_HEADER] = request_id
        return response

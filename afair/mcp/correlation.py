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

Implementation note (Perf audit C2): this is pure ASGI — it does NOT
inherit from BaseHTTPMiddleware. Starlette's BaseHTTPMiddleware
materializes the request body to provide the high-level Request/Response
API, which roughly doubles request overhead for streaming responses
(every chunk crosses an extra anyio bridge). Pure ASGI lets the SSE
stream from the MCP app pass through untouched.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


_HEADER = "X-Request-ID"
_HEADER_LOWER_BYTES = b"x-request-id"
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


def _header_value(headers: list[tuple[bytes, bytes]], name_lower: bytes) -> str | None:
    """Linear scan over the raw ASGI header list. Cheap for the small
    number of headers a request carries; avoids materializing a
    case-insensitive Headers object until we actually need it."""
    for k, v in headers:
        if k.lower() == name_lower:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


class CorrelationIdMiddleware:
    """ASGI middleware: bind a request id to structlog's contextvars for
    the duration of one request, and echo it back in the response.

    Should sit FAR OUT in the middleware stack — outermost is best —
    so even errors thrown by other middlewares carry the id in their
    log output.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        provided = _header_value(scope["headers"], _HEADER_LOWER_BYTES)
        request_id = _accept_or_mint(provided)
        header_pair = (_HEADER_LOWER_BYTES, request_id.encode("latin-1"))

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Inject our header onto outgoing response.start. Use a
                # fresh list so we don't mutate any caller's reference.
                headers = list(message.get("headers") or [])
                # Replace any existing X-Request-ID rather than appending —
                # double-headers cause caching/proxy weirdness.
                headers = [h for h in headers if h[0].lower() != _HEADER_LOWER_BYTES]
                headers.append(header_pair)
                message = {**message, "headers": headers}
            await send(message)

        # structlog.contextvars.bind_contextvars is task-local. Clean up
        # at the end of the request to keep the binding from leaking
        # across event-loop iterations.
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

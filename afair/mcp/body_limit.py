"""HTTP-layer request-body size cap.

Why this exists separately from the Pydantic ``MAX_REMEMBER_BYTES``
check in handlers: by the time Pydantic sees the body, uvicorn has
already read the entire request into memory. A 1 GB POST against
/remember would OOM the machine before our application-level reject
fires.

This middleware checks the ``Content-Length`` header BEFORE the body
is consumed and rejects with 413 if it's too large. Streaming uploads
that lie about (or omit) Content-Length still slip past — that's fine,
uvicorn's own request-line / header limits stop those much earlier.

12 MB cap is ~MAX_REMEMBER_BYTES (10 MB) plus JSON-envelope overhead
plus a generous tolerance.

Implementation (Perf audit C2): pure ASGI — header check happens on
the scope dict; no body read, no Request wrapper. The 413 reject is
a hand-rolled response so we don't allocate a JSONResponse object.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


DEFAULT_MAX_BODY_BYTES = 12 * 1024 * 1024  # 12 MB

_CONTENT_LENGTH = b"content-length"


def _header_value(headers: list[tuple[bytes, bytes]], name_lower: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name_lower:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


async def _send_json(send: Send, *, status: int, body: dict[str, str], close: bool = False) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    if close:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


class BodySizeLimitMiddleware:
    """Reject requests whose Content-Length exceeds ``max_body_bytes``.

    The check is header-only — we never read the body just to measure
    it. Clients that omit Content-Length get past this gate; uvicorn's
    own body limits handle them.

    Paths in ``exempt_paths`` bypass the cap entirely. Used by the
    streaming-upload endpoint which has its own (much larger) cap
    enforced via the per-chunk feed loop.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        exempt_paths: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self._max = max_body_bytes
        self._exempt = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._exempt:
            await self.app(scope, receive, send)
            return

        length_header = _header_value(scope["headers"], _CONTENT_LENGTH)
        if length_header:
            try:
                length = int(length_header)
            except ValueError:
                await _send_json(
                    send,
                    status=400,
                    body={"error": "bad_request", "detail": "malformed Content-Length"},
                )
                return
            if length > self._max:
                await _send_json(
                    send,
                    status=413,
                    body={
                        "error": "payload_too_large",
                        "detail": f"request body {length} bytes exceeds {self._max}",
                    },
                    close=True,
                )
                return

        await self.app(scope, receive, send)

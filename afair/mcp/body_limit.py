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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response


DEFAULT_MAX_BODY_BYTES = 12 * 1024 * 1024  # 12 MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds ``max_body_bytes``.

    The check is header-only — we never read the body just to measure
    it. Clients that omit Content-Length get past this gate; uvicorn's
    own body limits handle them.
    """

    def __init__(self, app: object, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max = max_body_bytes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                length = int(length_header)
            except ValueError:
                return JSONResponse(
                    {"error": "bad_request", "detail": "malformed Content-Length"},
                    status_code=400,
                )
            if length > self._max:
                return JSONResponse(
                    {
                        "error": "payload_too_large",
                        "detail": f"request body {length} bytes exceeds {self._max}",
                    },
                    status_code=413,
                    headers={"Connection": "close"},
                )
        return await call_next(request)

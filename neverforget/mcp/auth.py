"""HTTP-level bearer-token authentication for the MCP server.

This lives BELOW the MCP tool surface (Invariant I1): the four tool
signatures stay locked, only the transport layer adds a check. Future
OAuth/OIDC layers can stack on top without disturbing the tools.

Phase 0 design (the simplest secure thing):
  - Single 256-bit random token shared by the operator and embedded in
    each MCP client's connection config as ``Authorization: Bearer <token>``.
  - One token per machine, single-tenant (I8) — there is no per-user
    namespace because every machine belongs to exactly one user.
  - /health is exempt so Fly's orchestrator can probe liveness without
    knowing the secret.
  - Constant-time comparison to prevent token-length timing attacks.

Rotation: regenerate, set on Fly + .env.local + .env.secrets.backup,
redeploy. Old token stops working immediately at next deploy.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


_BEARER_PREFIX = "Bearer "
_WWW_AUTHENTICATE = 'Bearer realm="neverforget"'


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing a single shared bearer token.

    When ``token`` is ``None`` the middleware is a pass-through (intended
    only for local development on loopback). When a token is set, every
    request to a non-exempt path must carry ``Authorization: Bearer <token>``
    or receive a ``401 Unauthorized`` response.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None,
        exempt_paths: Iterable[str] = (),
    ) -> None:
        super().__init__(app)
        self._token = token
        self._exempt = frozenset(exempt_paths)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # No token configured — middleware is a no-op. (Boot validator
        # in settings.py refuses this combination in production.)
        if self._token is None:
            return await call_next(request)

        # Exempt paths bypass auth entirely (typically /health).
        if request.url.path in self._exempt:
            return await call_next(request)

        # Some MCP clients send the path with or without trailing slash;
        # normalize the exempt check to cover both.
        normalized = request.url.path.rstrip("/")
        if normalized in {p.rstrip("/") for p in self._exempt}:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith(_BEARER_PREFIX):
            return _unauthorized("missing Bearer token in Authorization header")

        provided = auth_header[len(_BEARER_PREFIX) :].strip()
        if not hmac.compare_digest(provided, self._token):
            return _unauthorized("invalid token")

        return await call_next(request)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )

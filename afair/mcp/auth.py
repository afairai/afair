"""HTTP-level authentication for the MCP server — accepts EITHER:

  1. The static bearer token (`AFAIR_AUTH_TOKEN`) — defense in depth,
     server-to-server convenience, CI smoke usability.
  2. A JWT we issued via the OAuth resource server (Phase 1+).

Both authenticated paths still enforce the I8 single-tenant allowlist
where applicable (JWT subject must be in `IDENTITY_ALLOWLIST`).

This lives BELOW the MCP tool surface (Invariant I1): the four tool
signatures stay locked, only the transport layer adds a check.

/health is exempt so Fly's orchestrator can probe liveness.
OAuth metadata + dance endpoints are also exempt (clients need to
discover and authenticate WITHOUT credentials).
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .oauth import jwt as jwt_mod

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

    from ..settings import Settings


_BEARER_PREFIX = "Bearer "


def _www_authenticate_header(settings: Settings) -> str:
    """Build the WWW-Authenticate header per RFC 6750.

    Includes a ``resource_metadata`` parameter pointing at our
    /.well-known/oauth-protected-resource endpoint so MCP clients can
    discover the authorization server and start the OAuth dance.
    """
    issuer = settings.effective_oauth_issuer
    return (
        f'Bearer realm="afair", resource_metadata="{issuer}/.well-known/oauth-protected-resource"'
    )


class BearerOrJwtMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing bearer-token OR JWT auth on /mcp.

    Auth modes accepted at the same endpoint:
      - static bearer (constant-time compare against ``static_token``)
      - JWT issued by us (validated via Authlib + allowlist check)

    Either passes the request through. Neither → 401 with the standard
    WWW-Authenticate header pointing at OAuth metadata.

    Exempt paths bypass auth entirely — used for /health and the OAuth
    discovery/dance endpoints which by definition can't carry auth yet.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        static_token: str | None,
        exempt_paths: Iterable[str] = (),
        exempt_prefixes: Iterable[str] = (),
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._token = static_token
        self._exempt = frozenset(exempt_paths)
        self._exempt_prefixes = tuple(exempt_prefixes)
        self._allowlist = settings.allowlist

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        # Exempt paths bypass auth entirely (health, /.well-known/*, /oauth/*)
        if path in self._exempt or path.rstrip("/") in {p.rstrip("/") for p in self._exempt}:
            return await call_next(request)
        if any(path.startswith(p) for p in self._exempt_prefixes):
            return await call_next(request)

        # No static token AND no JWT secret → pure dev mode (loopback only).
        if self._token is None and self._settings.jwt_secret is None:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith(_BEARER_PREFIX):
            return _unauthorized(self._settings, "missing Bearer credential")

        provided = auth_header[len(_BEARER_PREFIX) :].strip()

        # Try static bearer first (cheap constant-time compare).
        if self._token is not None and hmac.compare_digest(provided, self._token):
            # All static-bearer traffic shares a single rate-limit bucket.
            # Hashing the bytes works too but a stable label keeps logs
            # readable and means rotating the token doesn't create a new
            # bucket with a fresh burst window.
            request.state.rate_limit_identity = "static-bearer"
            return await call_next(request)

        # Try JWT.
        if self._settings.jwt_secret is not None:
            try:
                claims = jwt_mod.validate(provided, settings=self._settings)
            except jwt_mod.JWTError:
                pass
            else:
                # Enforce allowlist at the auth layer too (defense in depth
                # — the OAuth /authorize callback also enforces it).
                if self._allowlist and claims.sub.lower() not in self._allowlist:
                    return _unauthorized(
                        self._settings,
                        f"identity '{claims.sub}' is not on the allowlist",
                    )
                # Key rate-limit buckets by the verified JWT subject — NOT
                # by the raw token bytes — so a flood of fresh JWT mints
                # for the same identity still lands in one bucket
                # (Sec audit I2).
                request.state.rate_limit_identity = f"jwt:{claims.sub.lower()}"
                return await call_next(request)

        return _unauthorized(self._settings, "invalid token")


def _unauthorized(settings: Settings, detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "detail": detail},
        status_code=401,
        headers={"WWW-Authenticate": _www_authenticate_header(settings)},
    )


# Backwards-compatible alias for the older single-mode middleware name.
# Existing tests + server.py wire to this name; the class is now the
# bearer-OR-JWT one.
BearerTokenMiddleware = BearerOrJwtMiddleware

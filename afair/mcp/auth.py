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

Implementation (Perf audit C2): pure ASGI — header checks run on the
scope dict directly. The verified identity is stashed under a private
scope key (``afair_rate_limit_identity``) for the downstream rate-limit
middleware to pick up; that path used to thread through request.state
which required BaseHTTPMiddleware to materialize a Request object.
"""

from __future__ import annotations

import hmac
import json
from typing import TYPE_CHECKING

from .oauth import jwt as jwt_mod

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.types import ASGIApp, Receive, Scope, Send

    from ..settings import Settings


_BEARER_PREFIX = "Bearer "
_AUTHORIZATION = b"authorization"
SCOPE_IDENTITY_KEY = "afair_rate_limit_identity"
"""Scope key the auth middleware sets after a successful auth so the
rate-limit middleware can bucket on identity rather than token bytes."""


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


def _header_value(headers: list[tuple[bytes, bytes]], name_lower: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name_lower:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


async def _send_unauthorized(send: Send, *, settings: Settings, detail: str) -> None:
    payload = json.dumps({"error": "unauthorized", "detail": detail}, separators=(",", ":")).encode(
        "utf-8"
    )
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (
                    b"www-authenticate",
                    _www_authenticate_header(settings).encode("latin-1"),
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload, "more_body": False})


class BearerOrJwtMiddleware:
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
        self.app = app
        self._settings = settings
        self._token = static_token
        # Normalize exempt-path lookups once. The set uses the original
        # value AND a trailing-slash-stripped variant so both spellings
        # match without per-request string ops.
        self._exempt: frozenset[str] = frozenset(p for p in exempt_paths) | frozenset(
            p.rstrip("/") for p in exempt_paths
        )
        self._exempt_prefixes: tuple[str, ...] = tuple(exempt_prefixes)
        self._allowlist = settings.allowlist

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Exempt paths bypass auth entirely (health, /.well-known/*, /oauth/*).
        if path in self._exempt or path.rstrip("/") in self._exempt:
            await self.app(scope, receive, send)
            return
        if any(path.startswith(p) for p in self._exempt_prefixes):
            await self.app(scope, receive, send)
            return

        # No static token AND no JWT secret → pure dev mode (loopback only).
        if self._token is None and self._settings.jwt_secret is None:
            await self.app(scope, receive, send)
            return

        auth_header = _header_value(scope["headers"], _AUTHORIZATION) or ""
        if not auth_header.startswith(_BEARER_PREFIX):
            await _send_unauthorized(
                send, settings=self._settings, detail="missing Bearer credential"
            )
            return

        provided = auth_header[len(_BEARER_PREFIX) :].strip()

        # Try static bearer first (cheap constant-time compare).
        if self._token is not None and hmac.compare_digest(provided, self._token):
            # All static-bearer traffic shares a single rate-limit bucket.
            scope[SCOPE_IDENTITY_KEY] = "static-bearer"
            await self.app(scope, receive, send)
            return

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
                    await _send_unauthorized(
                        send,
                        settings=self._settings,
                        detail=f"identity '{claims.sub}' is not on the allowlist",
                    )
                    return
                # Key rate-limit buckets by the verified JWT subject — NOT
                # by the raw token bytes — so a flood of fresh JWT mints
                # for the same identity still lands in one bucket
                # (Sec audit I2).
                scope[SCOPE_IDENTITY_KEY] = f"jwt:{claims.sub.lower()}"
                await self.app(scope, receive, send)
                return

        await _send_unauthorized(send, settings=self._settings, detail="invalid token")


# Backwards-compatible alias for the older single-mode middleware name.
# Existing tests + server.py wire to this name; the class is now the
# bearer-OR-JWT one.
BearerTokenMiddleware = BearerOrJwtMiddleware

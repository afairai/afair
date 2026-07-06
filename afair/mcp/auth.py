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
    from .api_tokens import ApiToken


_BEARER_PREFIX = "Bearer "
_AUTHORIZATION = b"authorization"
SCOPE_IDENTITY_KEY = "afair_rate_limit_identity"
"""Scope key the auth middleware sets after a successful auth so the
rate-limit middleware can bucket on identity rather than token bytes."""

SCOPE_TOKEN_SCOPE_KEY = "afair_token_scope"
"""ASGI-scope key holding the authenticated credential's permission scope
("full" | "read" | "write"). The tool layer reads this to enforce that a
read-only minted token cannot perform write verbs (remember/observe).
Full-access credentials (static bearer, JWT) always set "full"."""

SCOPE_CLIENT_KEY = "afair_client"
"""ASGI-scope key holding the sanitized client slug of the authenticated
credential (ADR-0006). Derived ONLY from the credential itself — the master
bearer, the api-token label, or the OAuth ``client_name`` claim — never from
client-supplied headers or tool args. The tool layer reads it via
``current_client()`` to stamp ``event_provenance``."""

SCOPE_AUTH_KIND_KEY = "afair_auth_kind"
"""ASGI-scope key holding the auth mechanism that produced the client slug:
'master' | 'api-token' | 'oauth'. Local no-auth mode stamps neither this nor
``SCOPE_CLIENT_KEY``; ``current_client()`` then reports ('local', 'none')."""


_CLIENT_SLUG_KEEP = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_MAX_CLIENT_SLUG_LEN = 64


def client_slug(raw: str | None) -> str:
    """Sanitize a credential-derived client label into a stable slug (ADR-0006).

    Pure function: lowercases, keeps ``[a-z0-9._-]``, collapses every other run
    of characters to a single ``-``, strips leading/trailing separators, caps at
    64 chars, and maps empty/None to ``"unknown"``. Used on the api-token label
    and the OAuth ``client_name`` claim so an arbitrary user-chosen string
    becomes a bounded, index-friendly identifier that never carries raw content.
    """
    if not raw:
        return "unknown"
    out: list[str] = []
    prev_dash = False
    for ch in raw.lower():
        if ch in _CLIENT_SLUG_KEEP:
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-._")[:_MAX_CLIENT_SLUG_LEN].strip("-._")
    return slug or "unknown"


def current_client() -> tuple[str, str] | None:
    """The authenticated ``(client_slug, auth_kind)`` for the current request.

    Mirrors ``enforce_write_scope``'s HTTP-context handling (ADR-0006):

    - No HTTP request context (``get_http_request`` raises) → ``None``. That path
      is a direct/in-process call (unit tests, cold-path workers); no provenance
      is stamped.
    - HTTP context WITH a stamped client (every successful auth path in
      ``BearerOrJwtMiddleware`` sets both scope keys) → that ``(client,
      auth_kind)``.
    - HTTP context WITHOUT a stamped client → local self-host / no-auth mode
      (the middleware let the request through without stamping) → ``('local',
      'none')`` so self-hosters also get provenance.
    """
    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except Exception:
        return None  # no HTTP context — direct/in-process call
    client = request.scope.get(SCOPE_CLIENT_KEY)
    auth_kind = request.scope.get(SCOPE_AUTH_KIND_KEY)
    if isinstance(client, str) and isinstance(auth_kind, str):
        return (client, auth_kind)
    return ("local", "none")


def enforce_write_scope() -> None:
    """Reject write verbs (remember/observe) when the caller's token is read-only.

    Reads the authenticated credential's scope from the current HTTP request's
    ASGI scope (set by ``BearerOrJwtMiddleware``). Fails OPEN when there is no
    HTTP request context — that path is direct/in-process invocation (unit
    tests, the cold-path workers) which is implicitly full-access. (Security L2.)

    Missing-scope handling: every successful auth path in
    ``BearerOrJwtMiddleware`` stamps BOTH ``SCOPE_IDENTITY_KEY`` and
    ``SCOPE_TOKEN_SCOPE_KEY``; local self-host / no-auth mode stamps
    neither. So "identity present but scope absent" is anomalous
    (auth ran but no scope was recorded) and fails CLOSED, while
    "no identity" is the local no-auth path and stays allowed.
    """
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except Exception:
        return  # no HTTP context — direct/in-process call, allow
    token_scope = request.scope.get(SCOPE_TOKEN_SCOPE_KEY)
    if token_scope is None:
        if request.scope.get(SCOPE_IDENTITY_KEY) is not None:
            # Authenticated request without a stamped scope — should be
            # impossible; deny writes rather than fail open.
            raise ToolError(
                "authenticated credential carries no permission scope; "
                "write operations (remember, observe) are denied"
            )
        return  # no auth configured (local self-host) — allow
    if token_scope == "read":
        raise ToolError(
            "this token has read-only scope; write operations (remember, "
            "observe) require a token with write or full scope"
        )


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

    async def _verify_api_token(self, provided: str) -> ApiToken | None:
        """Look up a user-minted API token in substrate.

        Wrapped in an inline import + thread-pool hop so the
        synchronous SQLite read does not block the event loop. Returns
        None on miss / revoked / DB error so auth falls through.
        """
        from . import api_tokens as _toks
        from .context import connect_for_thread

        try:
            import anyio

            def _lookup() -> ApiToken | None:
                conn = connect_for_thread()
                return _toks.verify(conn, provided)

            return await anyio.to_thread.run_sync(_lookup)
        except Exception:
            return None

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
            # The master bearer is full-access by definition.
            scope[SCOPE_TOKEN_SCOPE_KEY] = "full"
            # Provenance (ADR-0006): the master credential is its own client.
            scope[SCOPE_CLIENT_KEY] = "master"
            scope[SCOPE_AUTH_KIND_KEY] = "master"
            await self.app(scope, receive, send)
            return

        # Try minted API tokens (user-revocable, stored in api_tokens
        # table). Hash-keyed DB lookup, falls through if no hit so JWT
        # gets its turn next.
        api_token = await self._verify_api_token(provided)
        if api_token is not None:
            # Each minted token is its own rate-limit identity so one
            # abusive bot does not starve the user's own session.
            scope[SCOPE_IDENTITY_KEY] = f"api-token:{api_token.id}"
            # Enforce the token's stored scope at the tool boundary (L2).
            scope[SCOPE_TOKEN_SCOPE_KEY] = api_token.scope
            # Provenance (ADR-0006): the token's user-chosen label, sanitized.
            scope[SCOPE_CLIENT_KEY] = client_slug(api_token.label)
            scope[SCOPE_AUTH_KIND_KEY] = "api-token"
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
                # OAuth-issued JWTs grant full access (the user's own session).
                scope[SCOPE_TOKEN_SCOPE_KEY] = "full"
                # Provenance (ADR-0006): the DCR client_name claim, sanitized.
                # Legacy tokens minted before the claim existed carry None →
                # fall back to a generic 'oauth' slug rather than 'unknown'.
                scope[SCOPE_CLIENT_KEY] = (
                    client_slug(claims.client_name) if claims.client_name else "oauth"
                )
                scope[SCOPE_AUTH_KIND_KEY] = "oauth"
                await self.app(scope, receive, send)
                return

        await _send_unauthorized(send, settings=self._settings, detail="invalid token")


# Backwards-compatible alias for the older single-mode middleware name.
# Existing tests + server.py wire to this name; the class is now the
# bearer-OR-JWT one.
BearerTokenMiddleware = BearerOrJwtMiddleware

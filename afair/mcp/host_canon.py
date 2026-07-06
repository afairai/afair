"""Host canonicalization — fix the fly.dev-vs-vanity connect footgun.

Each managed vault runs behind a per-user vanity host
(``<suffix>.mcp.afair.ai``) but is also reachable at its raw Fly alias
(``<app>.fly.dev``). The OAuth discovery documents advertise the vanity
issuer, so a strict MCP client that connected via the ``.fly.dev`` alias
sees an ``aud``/``resource`` that doesn't match the host it dialed and
rejects the handshake. This is a connect-DX bug, not a vulnerability — the
per-machine secret is the real trust boundary and the token even validates
on the alias — but it makes a first connection to the wrong host fail
confusingly.

The fix is transport-only: redirect browser/discovery GETs to the canonical
host, and answer a mis-hosted MCP POST with ``421 Misdirected Request``
naming the canonical URL. The token audience is NOT widened — there stays
exactly one canonical issuer (I-side invariant: one issuer, one audience).

No-op unless ``environment == "fly"`` AND an explicit ``oauth_issuer`` is set.
Locally the issuer is derived from host:port and the TestClient dials
``testserver``; enforcing there would break every HTTP test, so the guard is
a hard requirement.

Implementation (matches the house style): pure ASGI, no BaseHTTPMiddleware,
no body read. Slotted after SecurityHeadersMiddleware and before
BodySizeLimitMiddleware so a redirect/421 still carries the security headers
and the request id, and is decided before auth/rate-limit state is spent.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


_HOST = b"host"

# GET/HEAD browser + discovery paths that get a 308 to the canonical host.
_REDIRECT_PATHS = frozenset(
    {
        "/",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/oauth/authorize",
        "/oauth/identity/accept",
        "/oauth/identity/github/callback",
    }
)


def _header_value(headers: list[tuple[bytes, bytes]], name_lower: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name_lower:
            try:
                return v.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


def _normalize_netloc(netloc: str) -> str:
    """Lowercase and strip a default (80/443) port so ``Host`` comparison is
    scheme-agnostic and port-noise-free."""
    n = netloc.strip().lower()
    if n.endswith(":443"):
        n = n[:-4]
    elif n.endswith(":80"):
        n = n[:-3]
    return n


async def _send_json(send: Send, *, status: int, body: dict[str, str]) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload, "more_body": False})


async def _send_redirect(send: Send, *, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 308,
            "headers": [
                (b"location", location.encode("latin-1")),
                (b"content-length", b"0"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


class HostCanonicalizationMiddleware:
    """Redirect/reject requests that reach the vault on a non-canonical host.

    ``issuer`` is the canonical origin (scheme + host), e.g.
    ``https://alice.mcp.afair.ai``; the canonical netloc is derived from it.
    When ``enabled`` is False the middleware is a transparent pass-through.
    """

    def __init__(self, app: ASGIApp, *, environment: str, issuer: str | None) -> None:
        self.app = app
        self._enabled = environment == "fly" and bool(issuer)
        self._issuer = (issuer or "").rstrip("/")
        self._canonical_netloc = (
            _normalize_netloc(urllib.parse.urlsplit(self._issuer).netloc) if self._enabled else ""
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = _header_value(scope["headers"], _HOST)
        # No Host header, or already canonical → pass through untouched.
        if host is None or _normalize_netloc(host) == self._canonical_netloc:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Browser/discovery GET/HEAD on a known path → 308 to canonical URL,
        # preserving the path + query so the client re-dials the right host.
        if method in ("GET", "HEAD") and path in _REDIRECT_PATHS:
            query = scope.get("query_string", b"")
            location = f"{self._issuer}{path}"
            if query:
                location = f"{location}?{query.decode('latin-1')}"
            await _send_redirect(send, location=location)
            return

        # The MCP protocol endpoint on the wrong host → 421 so a strict client
        # re-resolves the canonical authority rather than silently trusting an
        # aud/resource mismatch. Body names the canonical MCP URL.
        if method == "POST" and (path == "/mcp" or path.startswith("/mcp/")):
            await _send_json(
                send,
                status=421,
                body={
                    "error": "misdirected_request",
                    "detail": (
                        "this vault's canonical MCP endpoint is "
                        f"{self._issuer}/mcp; reconnect there"
                    ),
                },
            )
            return

        # Everything else on a non-canonical host (health probes, /internal/*,
        # other methods) passes through — canonicalizing those risks breaking
        # Fly's internal probes and any control-plane call to the .fly.dev name.
        await self.app(scope, receive, send)

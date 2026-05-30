"""Standard HTTP security-header middleware.

The headers are belt-and-suspenders for an API host — most clients
(Claude.ai, Codex CLI, our own scripts) ignore them, but they're cheap
to set, hardening against the day the surface grows a browser-facing
endpoint, and they document our intent to security scanners.

Why a custom middleware instead of pulling in starlette-secure or
similar: the deps are over-broad (express-style framework helpers),
their defaults assume browser apps, and we want exactly what we want
— nothing more. Twelve lines vs another dep.

Implementation (Perf audit C2): pure ASGI — wraps ``send`` and injects
headers onto the http.response.start message. No body materialization,
no per-chunk overhead — the underlying response (often a streaming
MCP SSE) passes through untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Headers applied to every response. Values follow the ~/.claude/rules/
# security.md recommendation set. Stored as a pre-encoded list of
# (lowercase-name, value) tuples so the per-request wrap is just an
# extend.
def _build_header_bytes() -> list[tuple[bytes, bytes]]:
    raw: dict[str, str] = {
        # HSTS — force HTTPS on the apex + subdomains for 2 years.
        # 'preload' opts into Chrome's preload list (manual submission later).
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
        # Click-jacking — we never render in a frame.
        "X-Frame-Options": "DENY",
        # MIME sniffing — refuse to interpret a content-type that disagrees with us.
        "X-Content-Type-Options": "nosniff",
        # Referrer — leak the origin only on same-origin nav, never any path.
        "Referrer-Policy": "strict-origin-when-cross-origin",
        # Permissions — deny everything by default. We're an API + a static
        # landing page; we need none of these. Modern features (browsing-
        # topics, FLEDGE / Protected-Audience, attribution-reporting, FedCM,
        # private-state-tokens) all denied so a future browser surface
        # can't accidentally opt in via a third-party include (Sec audit M5).
        "Permissions-Policy": (
            "camera=(), microphone=(), geolocation=(), "
            "interest-cohort=(), browsing-topics=(), "
            "join-ad-interest-group=(), run-ad-auction=(), "
            "attribution-reporting=(), identity-credentials-get=(), "
            "private-state-token-issuance=(), private-state-token-redemption=(), "
            "fullscreen=(), payment=(), autoplay=(), encrypted-media=(), "
            "usb=(), serial=(), bluetooth=(), hid=(), midi=()"
        ),
        # Discourage server fingerprinting; Fly's proxy still surfaces its own
        # 'server' header so this is partial cover.
        "X-Robots-Tag": "noindex",
        # Content-Security-Policy — the backend serves JSON for everything
        # except the GET / pointer; no scripts, no styles, no images load.
        # default-src 'none' rejects every fetch type; frame-ancestors 'none'
        # is the modern X-Frame-Options. Belt-and-suspenders for the day a
        # future HTML endpoint lands here by accident (Sec audit M4).
        "Content-Security-Policy": (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        ),
    }
    return [(name.lower().encode("ascii"), value.encode("latin-1")) for name, value in raw.items()]


_HEADER_BYTES: list[tuple[bytes, bytes]] = _build_header_bytes()
_HEADER_NAMES_LOWER: frozenset[bytes] = frozenset(name for name, _ in _HEADER_BYTES)


class SecurityHeadersMiddleware:
    """Append standard security headers to every response.

    Idempotent — only sets headers if they're not already present, so
    handler-specific overrides (e.g., the landing page's own
    Cache-Control) keep their value.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
                existing_lower: set[bytes] = {h[0].lower() for h in existing}
                additions = [
                    (name, value) for name, value in _HEADER_BYTES if name not in existing_lower
                ]
                if additions:
                    message = {**message, "headers": existing + additions}
            await send(message)

        await self.app(scope, receive, send_wrapped)

"""Standard HTTP security-header middleware.

The headers are belt-and-suspenders for an API host — most clients
(Claude.ai, Codex CLI, our own scripts) ignore them, but they're cheap
to set, hardening against the day the surface grows a browser-facing
endpoint, and they document our intent to security scanners.

Why a custom middleware instead of pulling in starlette-secure or
similar: the deps are over-broad (express-style framework helpers),
their defaults assume browser apps, and we want exactly what we want
— nothing more. Twelve lines vs another dep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response


# Headers applied to every response. Values follow the ~/.claude/rules/
# security.md recommendation set.
_SECURITY_HEADERS: dict[str, str] = {
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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append standard security headers to every response.

    Idempotent — only sets headers if they're not already present, so
    handler-specific overrides (e.g., the landing page's own
    Cache-Control) keep their value.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

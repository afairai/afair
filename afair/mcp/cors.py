"""Shared CORS allow-list for the browser-facing /internal routes.

The /account dashboard loads from afair.ai but calls the per-user vault at
``<vanity>.mcp.afair.ai`` — a cross-origin request that carries the master
bearer. Both the token-management routes and the export route need the
SAME narrow allow-list, and a drift between them (one route allowing an
origin the other doesn't) would be a security bug. So the allow-list lives
here, once, and both import it.

Anything not on the list gets no CORS headers at all, so the browser blocks
the cross-origin read — the master token is far too sensitive to expose to
arbitrary origins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

PROD_ALLOWED_ORIGINS = frozenset({"https://afair.ai"})
# localhost is added ONLY off-prod — a production server must never reflect
# CORS for http://localhost:3000, or any local process that binds that port
# becomes a permitted reader of the master-bearer-protected responses.
DEV_ALLOWED_ORIGINS = frozenset({"http://localhost:3000"})


def _allowed_origins(request: Request) -> frozenset[str]:
    settings = request.app.state.settings
    if getattr(settings, "environment", None) == "fly":
        return PROD_ALLOWED_ORIGINS
    return PROD_ALLOWED_ORIGINS | DEV_ALLOWED_ORIGINS


def cors_headers(request: Request) -> dict[str, str]:
    """CORS headers for an allow-listed origin, else an empty dict.

    Returning nothing for non-allow-listed origins keeps same-origin and
    non-browser clients (curl, the MCP transport) completely unaffected —
    they never send an ``Origin`` header that matches, and get a normal
    response with no CORS decoration.

    The localhost dev origin is allow-listed only when ENVIRONMENT != "fly",
    so production (mcp.afair.ai) reflects CORS for https://afair.ai alone.
    """
    origin = request.headers.get("origin", "")
    if origin in _allowed_origins(request):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Max-Age": "300",
            "Vary": "Origin",
        }
    return {}


async def preflight_endpoint(request: Request) -> Response:
    """OPTIONS handler so browsers stop pre-failing the cross-origin fetch."""
    return JSONResponse({}, headers=cors_headers(request))

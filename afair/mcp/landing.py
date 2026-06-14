"""Root path ``/`` — minimal JSON pointer, NOT a marketing site.

The substrate machine's job is to be the MCP server. Marketing content,
landing pages, signup flows, and onboarding lives on a separate property
(see VISION.md §10 — afair.ai for marketing, app.afair.ai for control
plane, this machine only at /mcp for the MCP protocol itself).

Before 2026-05-26 this route served a full-page HTML manifesto. That was
fine for Phase 0 (developer's own machine, no users to onboard) but
violated the eventual phase-8 architecture where:

  - Each user gets their own dedicated Fly machine for THEIR substrate
  - The user-facing brand surface (afair.ai) is a SEPARATE deployment
  - These two should never co-tenant on a single node

So this route now returns a tiny JSON pointer. Curious visitors hitting
the bare URL see:
  - what this server is (an afair MCP instance)
  - where to find the marketing/install info (afair.ai)
  - where the MCP protocol endpoint actually lives (/mcp)
  - link to source for self-hosters

The page is GET-only on ``/``. POST ``/`` still routes to the MCP server
(Starlette tries the GET route, partial-match fails on POST, continues
to the Mount which catches it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request


# Static pointer payload. No version field on purpose — clients should
# call MCP tools/list and get capability info there, not from this page.
_POINTER = {
    "server": "afair-mcp",
    "description": "User-owned, vendor-neutral, self-organizing cognitive memory layer for AI agents.",
    "mcp_endpoint": "/mcp",
    "marketing": "https://afair.ai",
    "source": "https://github.com/afairai/afair",
}


async def index(_request: Request) -> JSONResponse:
    """Serve the static JSON pointer. GET /; POST / still goes to MCP."""
    return JSONResponse(
        _POINTER,
        # Long cache — the pointer is static metadata. Changes happen via
        # redeploy, which gives the page a new ETag through any CDN in
        # front of us.
        headers={"Cache-Control": "public, max-age=3600, must-revalidate"},
    )

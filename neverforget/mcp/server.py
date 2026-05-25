"""FastMCP server wiring.

Registers the four v1 tools with their AI-facing descriptions and exposes
a /health endpoint for the orchestrator (Fly) to probe. The bearer-token
auth middleware is layered on at HTTP-level via a thin Starlette wrapper —
this keeps authentication BELOW the MCP tool surface (Invariant I1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from ..substrate import open_db
from . import descriptions, handlers, schemas
from .auth import BearerTokenMiddleware
from .context import ServerContext, get_context, set_context

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..settings import Settings


def build_server(settings: Settings) -> FastMCP:
    """Construct a FastMCP instance wired to a freshly-opened substrate.

    The server name "neverforget" is the codename surface visible to MCP
    clients (see CLAUDE.md §1 — renaming requires a coordinated update of
    every client's connection config).
    """
    db = open_db(settings.vault_dir)
    set_context(
        ServerContext(
            db=db,
            vault_dir=settings.vault_dir,
            inline_text_max_bytes=settings.inline_text_max_bytes,
            extractor_model=settings.extractor_model,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            gemini_api_key=settings.gemini_api_key,
        )
    )

    mcp: FastMCP = FastMCP("neverforget")

    # ── tools — descriptions are AI-facing prompts, see descriptions.py ─────

    @mcp.tool(description=descriptions.REMEMBER, version="1")
    def remember(
        content: schemas.RememberContent,
        context: str | None = None,
        type_hint: str | None = None,
        parent_hashes: list[str] | None = None,
    ) -> schemas.RememberResult:
        return handlers.remember(
            content=content,
            context=context,
            type_hint=type_hint,
            parent_hashes=parent_hashes,
        )

    @mcp.tool(description=descriptions.RECALL, version="1")
    def recall(
        query: str,
        scope: str | None = None,
        depth: schemas.Depth = "shallow",
        limit: int = 20,
    ) -> schemas.RecallResult:
        return handlers.recall(query=query, scope=scope, depth=depth, limit=limit)

    @mcp.tool(description=descriptions.LIST_CONTEXT, version="1")
    def list_context(
        about: str | None = None,
        limit: int = 50,
    ) -> schemas.ListContextResult:
        return handlers.list_context(about=about, limit=limit)

    @mcp.tool(description=descriptions.OBSERVE, version="1")
    def observe(event: schemas.ObserveEvent) -> schemas.ObserveResult:
        return handlers.observe(event=event)

    # ── /health — orchestrator-facing, never goes through MCP protocol ──────

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            ctx = get_context()
            ctx.db.execute("SELECT 1").fetchone()
        except Exception as e:
            return JSONResponse(
                {"status": "degraded", "error": str(e)},
                status_code=503,
            )
        return JSONResponse({"status": "ok"})

    return mcp


def build_app(settings: Settings) -> Starlette:
    """Build the ASGI app: FastMCP wrapped in the bearer-token middleware.

    The Starlette wrapper composes two layers:
      1. ``BearerTokenMiddleware`` — checks Authorization: Bearer header
         on every request, except the exempt paths.
      2. The FastMCP app mounted at "/" — handles MCP protocol + /health.

    /health is the only exempt path: Fly's orchestrator probes it to
    determine liveness and cannot present the auth token.
    """
    mcp = build_server(settings)
    mcp_app = mcp.http_app()
    token = settings.auth_token.get_secret_value() if settings.auth_token is not None else None
    middleware = [
        Middleware(
            BearerTokenMiddleware,
            token=token,
            exempt_paths=["/health"],
        ),
    ]
    # CRITICAL: pass FastMCP's lifespan to the parent app so its
    # StreamableHTTPSessionManager initializes correctly. Without this,
    # all MCP-protocol requests fail with "Task group is not initialized".
    return Starlette(
        routes=[Mount("/", app=mcp_app)],
        middleware=middleware,
        lifespan=mcp_app.lifespan,
    )


def run(settings: Settings) -> None:
    """Run the MCP server until interrupted. Uses Streamable HTTP transport."""
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )

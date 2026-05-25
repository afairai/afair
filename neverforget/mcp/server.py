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
from starlette.routing import Mount, Route

from ..substrate import open_db
from . import descriptions, handlers, schemas
from .auth import BearerTokenMiddleware
from .context import ServerContext, get_context, set_context
from .oauth import routes as oauth_routes

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..settings import Settings


def build_server(settings: Settings) -> FastMCP:
    """Construct a FastMCP instance wired to a freshly-opened substrate.

    The server name "neverforget" is the codename surface visible to MCP
    clients (see CLAUDE.md §1 — renaming requires a coordinated update of
    every client's connection config).
    """
    db = open_db(settings.vault_dir, embedding_dim=settings.embedding_dim)
    set_context(
        ServerContext(
            db=db,
            vault_dir=settings.vault_dir,
            inline_text_max_bytes=settings.inline_text_max_bytes,
            extractor_model=settings.extractor_model,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            gemini_api_key=settings.gemini_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            semantic_recall_enabled=settings.semantic_recall_enabled,
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
        depth: schemas.Depth = "normal",
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
    static_token = (
        settings.auth_token.get_secret_value() if settings.auth_token is not None else None
    )

    # OAuth-related paths must bypass the auth middleware (clients need to
    # discover and start the dance without credentials).
    exempt_paths = {
        "/health",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    }
    exempt_prefixes = ("/oauth/",)

    middleware = [
        Middleware(
            BearerTokenMiddleware,
            settings=settings,
            static_token=static_token,
            exempt_paths=exempt_paths,
            exempt_prefixes=exempt_prefixes,
        ),
    ]

    # Routes: OAuth metadata + dance endpoints + the mounted MCP app.
    routes = [
        Route(
            "/.well-known/oauth-protected-resource",
            oauth_routes.well_known_oauth_protected_resource,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-authorization-server",
            oauth_routes.well_known_oauth_authorization_server,
            methods=["GET"],
        ),
        Route("/oauth/register", oauth_routes.oauth_register, methods=["POST"]),
        Route("/oauth/authorize", oauth_routes.oauth_authorize, methods=["GET"]),
        Route(
            "/oauth/identity/github/callback",
            oauth_routes.oauth_identity_github_callback,
            methods=["GET"],
        ),
        Route("/oauth/token", oauth_routes.oauth_token, methods=["POST"]),
        Route("/oauth/revoke", oauth_routes.oauth_revoke, methods=["POST"]),
        Mount("/", app=mcp_app),
    ]

    # CRITICAL: pass FastMCP's lifespan to the parent app so its
    # StreamableHTTPSessionManager initializes correctly.
    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=mcp_app.lifespan,
    )
    # Make settings accessible to OAuth route handlers via request.app.state.
    app.state.settings = settings
    return app


def run(settings: Settings) -> None:
    """Run the MCP server until interrupted. Uses Streamable HTTP transport."""
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )

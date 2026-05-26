"""FastMCP server wiring.

Registers the four v1 tools with their AI-facing descriptions and exposes
a /health endpoint for the orchestrator (Fly) to probe. The bearer-token
auth middleware is layered on at HTTP-level via a thin Starlette wrapper —
this keeps authentication BELOW the MCP tool surface (Invariant I1).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ..agents.cold_path import ColdPathScheduler
from ..agents.conflict_resolver import ConflictResolver
from ..agents.consolidator import Consolidator
from ..agents.embedding import embed_query
from ..agents.pruner import Pruner
from ..substrate import start_checkpoint_loop
from . import descriptions, handlers, landing, schemas
from .auth import BearerTokenMiddleware
from .body_limit import BodySizeLimitMiddleware
from .context import ServerContext, connect_for_thread, set_context
from .correlation import CorrelationIdMiddleware
from .oauth import routes as oauth_routes
from .rate_limit import RateLimitMiddleware, TokenBucketRateLimiter
from .security_headers import SecurityHeadersMiddleware

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..settings import Settings


def build_server(settings: Settings) -> FastMCP:
    """Construct a FastMCP instance wired to the substrate.

    Production no longer holds a single shared sqlite3.Connection on
    ServerContext — handlers acquire per-thread connections via
    ``connect_for_thread()`` so SQLite WAL's concurrent-reader model
    is actually exercised when multiple AI clients hit the server.

    The server name "neverforget" is the codename surface visible to MCP
    clients (see CLAUDE.md §1 — renaming requires a coordinated update of
    every client's connection config).
    """
    set_context(
        ServerContext(
            vault_dir=settings.vault_dir,
            inline_text_max_bytes=settings.inline_text_max_bytes,
            extractor_model=settings.extractor_model,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            gemini_api_key=settings.gemini_api_key,
            voyage_api_key=settings.voyage_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            semantic_recall_enabled=settings.semantic_recall_enabled,
            cold_path_enabled=settings.cold_path_enabled,
        )
    )

    # Phase 3 sleep swarm. Daemon thread runs Pruner + Conflict-Resolver
    # + Consolidator on their own intervals. Each worker is independently
    # tested + bounded so a single bad cycle can't crash the scheduler.
    if settings.cold_path_enabled:
        ColdPathScheduler(
            vault_dir=settings.vault_dir,
            embedding_dim=settings.embedding_dim,
            settings=settings,
            workers=[Pruner(), ConflictResolver(), Consolidator()],
        ).start()

    # Background WAL-checkpoint loop — folds back the WAL file every 5
    # minutes so it doesn't grow unbounded on long-running servers.
    start_checkpoint_loop(
        settings.vault_dir,
        embedding_dim=settings.embedding_dim,
        interval_seconds=300,
    )

    # Pre-warm in a background thread so boot stays fast but the first
    # user request hits an already-open SQLite connection AND an already-
    # warm OpenAI HTTPS connection. Pays ~1-2s of cold-start cost upfront
    # so the first real recall isn't penalized.
    _spawn_warmup(settings)

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

    @mcp.tool(description=descriptions.GET_EVENT, version="1")
    def get_event(
        event_id: str | None = None,
        content_hash: str | None = None,
    ) -> schemas.GetEventResult:
        return handlers.get_event(event_id=event_id, content_hash=content_hash)

    @mcp.tool(description=descriptions.INVALIDATE, version="1")
    def invalidate(
        target_hash: str,
        reason: str | None = None,
    ) -> schemas.InvalidateResult:
        return handlers.invalidate(target_hash=target_hash, reason=reason)

    # ── /health — orchestrator-facing, never goes through MCP protocol ──────

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            db = connect_for_thread()
            db.execute("SELECT 1").fetchone()
        except Exception as e:
            # Log internally with full detail; expose only a generic flag.
            # The orchestrator (Fly) acts on the HTTP status, not the body —
            # so leaking str(e) to anyone-on-the-internet buys nothing and
            # could surface internal paths or library quirks.
            log.warning("health.degraded", error=str(e), exc_type=type(e).__name__)
            return JSONResponse(
                {"status": "degraded"},
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
    # discover and start the dance without credentials). The landing page
    # at / is public too (visiting the URL should never gate on a token).
    exempt_paths = {
        "/",
        "/health",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    }
    exempt_prefixes = ("/oauth/",)

    # Per-identity rate limiter. Instance lives for process lifetime so
    # buckets aren't lost on every request. Settings stay defaults; if we
    # need per-deployment tuning later, add to Settings.
    rate_limiter = TokenBucketRateLimiter()

    middleware = [
        # Outermost — correlation id must bind BEFORE other middlewares so
        # their log lines also carry the request_id field.
        Middleware(CorrelationIdMiddleware),
        # Security headers — applied to every response, including 4xx
        # rejections from middlewares below. Belt-and-suspenders.
        Middleware(SecurityHeadersMiddleware),
        # gzip — compression sees responses from middlewares below.
        # min_size=500 avoids compressing tiny payloads.
        Middleware(GZipMiddleware, minimum_size=500, compresslevel=5),
        # Body-size cap — reject oversized requests BEFORE uvicorn reads
        # the whole body into memory. 12 MB > MAX_REMEMBER_BYTES (10MB)
        # + JSON envelope overhead.
        Middleware(BodySizeLimitMiddleware),
        # Authentication — must come BEFORE rate limiting so we don't burn
        # bucket entries on random unauthenticated probes.
        Middleware(
            BearerTokenMiddleware,
            settings=settings,
            static_token=static_token,
            exempt_paths=exempt_paths,
            exempt_prefixes=exempt_prefixes,
        ),
        # Rate limiter — per-token bucket, deny-with-429 above the cap.
        # Authenticated traffic only (auth already rejected unauthed).
        Middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            exempt_paths=exempt_paths,
            exempt_prefixes=exempt_prefixes,
        ),
    ]

    # Routes: landing page + OAuth metadata + dance endpoints + the
    # mounted MCP app. The landing Route is GET/HEAD-only at "/"; POST /
    # falls through Starlette's partial-match handling to the Mount("/")
    # below which routes to the FastMCP app for the MCP protocol.
    routes = [
        Route("/", landing.index, methods=["GET", "HEAD"]),
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


def _spawn_warmup(settings: Settings) -> None:
    """Pre-warm SQLite + the embedding provider in a background thread.

    Sequenced:
      1. Open a DB connection and run a trivial SELECT — pages the
         schema + sqlite-vec extension into the SQLite cache.
      2. Issue ONE dummy ``embed_query`` against the configured model.
         For OpenAI/Voyage/etc. this performs the TLS handshake + first
         HTTP/2 request so the persistent httpx client kept by litellm
         is warm. For fastembed it triggers the model download (if not
         cached) and ONNX session creation.

    Runs as a daemon thread so boot stays fast and a slow/failed warmup
    doesn't block server startup.
    """

    def warmup() -> None:
        try:
            db = connect_for_thread()
            db.execute("SELECT 1").fetchone()
        except Exception as e:
            log.warning("warmup.db_failed", error=str(e))

        # Skip embedding warmup if we have no API key for the configured
        # model — in dev without keys this would just log a warning.
        api_key: str | None = None
        if settings.embedding_model.startswith("openai/") and settings.openai_api_key:
            api_key = settings.openai_api_key.get_secret_value()
        elif settings.embedding_model.startswith("voyage/") and settings.voyage_api_key:
            api_key = settings.voyage_api_key.get_secret_value()
        elif settings.embedding_model.startswith("anthropic/") and settings.anthropic_api_key:
            api_key = settings.anthropic_api_key.get_secret_value()
        elif settings.embedding_model.startswith("gemini/") and settings.gemini_api_key:
            api_key = settings.gemini_api_key.get_secret_value()
        # fastembed/* needs no key.

        try:
            embed_query(model=settings.embedding_model, text="warmup", api_key=api_key)
            log.info("warmup.done", model=settings.embedding_model)
        except Exception as e:
            log.warning("warmup.embedding_failed", error=str(e))

    threading.Thread(target=warmup, name="boot-warmup", daemon=True).start()


def run(settings: Settings) -> None:
    """Run the MCP server until interrupted. Uses Streamable HTTP transport.

    Uses uvloop event loop on POSIX for ~10-30% I/O throughput improvement
    over asyncio default. On Windows, uvicorn falls back to asyncio
    automatically (the dependency is platform-gated in pyproject).
    """
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        loop="uvloop",
        http="httptools",
    )

"""FastMCP server wiring.

Registers the three v1 tools (remember, recall, observe) with their
AI-facing descriptions and exposes a /health endpoint for the orchestrator
(Fly) to probe. The bearer-token auth middleware is layered on at
HTTP-level via a thin Starlette wrapper — this keeps authentication BELOW
the MCP tool surface (Invariant I1).

The 3-tool surface was fixed on 2026-05-26 before any external user
adopted the API. Per I1, this surface is now forever-stable; future
additions are new tools, never signature changes to these three.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
import structlog
import uvicorn
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent as MCPTextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from ..agents.blob_sweeper import OrphanBlobSweeper
from ..agents.cold_path import ColdPathScheduler
from ..agents.conflict_resolver import ConflictResolver
from ..agents.consolidator import Consolidator
from ..agents.edge_scorer import EdgeConfidenceScorer
from ..agents.embedding import embed_text
from ..agents.entity_articles import EntityArticleWorker
from ..agents.entity_audit import EntityAuditWorker
from ..agents.entity_canonicalizer import EntityCanonicalizer
from ..agents.entity_dedup import EntityDeduplicator
from ..agents.expectation_checker import ExpectationChecker
from ..agents.extraction_retry import ExtractionRetryWorker
from ..agents.mode_switcher import ModeSwitcher
from ..agents.pruner import Pruner
from ..agents.rollback_monitor import RollbackMonitor
from ..agents.salience import SalienceWorker
from ..agents.schema_evolver import SchemaEvolver
from ..agents.temporal import TemporalWorker
from ..agents.tuner import Tuner
from ..substrate import observability, start_checkpoint_loop
from ..substrate.db import set_vault_key
from . import descriptions, handlers, landing, resources, schemas
from .auth import BearerTokenMiddleware, enforce_write_scope
from .blob_upload_route import blob_upload_endpoint
from .body_limit import BodySizeLimitMiddleware
from .context import ServerContext, connect_for_thread, set_context
from .correlation import CorrelationIdMiddleware
from .cors import preflight_endpoint as tokens_preflight_endpoint
from .export_async_routes import (
    export_download_endpoint,
    export_request_endpoint,
    export_status_endpoint,
)
from .export_route import export_endpoint
from .oauth import routes as oauth_routes
from .rate_limit import (
    InternalPathRateLimitMiddleware,
    RateLimitMiddleware,
    TokenBucketRateLimiter,
)
from .security_headers import SecurityHeadersMiddleware
from .signup_route import signup_endpoint
from .tokens_route import list_endpoint as tokens_list_endpoint
from .tokens_route import mint_endpoint as tokens_mint_endpoint
from .tokens_route import revoke_endpoint as tokens_revoke_endpoint

log = structlog.get_logger(__name__)

# Advertised outputSchema for the recall tool. Because recall returns a
# ``ToolResult`` (for null-free serialization, P1-2 §4.3), FastMCP can't infer
# the schema from the return annotation — we pass it explicitly so the wire
# contract is unchanged for clients that read outputSchema.
_RECALL_OUTPUT_SCHEMA = schemas.RecallResult.model_json_schema()

if TYPE_CHECKING:
    from starlette.requests import Request

    from ..settings import Settings


def build_server(settings: Settings) -> FastMCP:
    """Construct a FastMCP instance wired to the substrate.

    Production no longer holds a single shared sqlite3.Connection on
    ServerContext — handlers acquire per-thread connections via
    ``connect_for_thread()`` so SQLite WAL's concurrent-reader model
    is actually exercised when multiple AI clients hit the server.

    The server name "afair" is the codename surface visible to MCP
    clients (see CLAUDE.md §1 — renaming requires a coordinated update of
    every client's connection config).
    """
    # Install the vault encryption key first, before ANY open_db call
    # (set_context below itself does not open a connection, but the
    # warmup thread and cold-path scheduler will). Production refuses
    # to start without one — see settings._vault_key_required_in_prod.
    if settings.vault_key is not None:
        set_vault_key(settings.vault_key.get_secret_value().encode("utf-8"))

    set_context(
        ServerContext(
            vault_dir=settings.vault_dir,
            inline_text_max_bytes=settings.inline_text_max_bytes,
            extractor_model=settings.extractor_model,
            vision_model=settings.vision_model,
            transcription_model=settings.transcription_model,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
            gemini_api_key=settings.gemini_api_key,
            voyage_api_key=settings.voyage_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
            semantic_recall_enabled=settings.semantic_recall_enabled,
            cold_path_enabled=settings.cold_path_enabled,
            surprise_context_window=settings.surprise_context_window,
        )
    )

    # Phase 3 sleep swarm. Daemon thread runs Pruner + Conflict-Resolver
    # + Consolidator on their own intervals. Each worker is independently
    # tested + bounded so a single bad cycle can't crash the scheduler.
    # Retained so /health can read per-worker liveness via scheduler.status().
    # None when the cold path is disabled (self-hosters may run read-only).
    scheduler: ColdPathScheduler | None = None
    if settings.cold_path_enabled:
        scheduler = ColdPathScheduler(
            vault_dir=settings.vault_dir,
            embedding_dim=settings.embedding_dim,
            settings=settings,
            workers=[
                Pruner(),
                OrphanBlobSweeper(),
                # Bounded re-extraction of events whose latest extractor
                # interpretation is a transient failure (llm_timeout /
                # llm_rate_limit). Closes the silent-permanent-gap failure
                # mode where a timed-out extraction was never re-attempted.
                ExtractionRetryWorker(),
                # Phase 0.5 observability — detection-only. Counts silent
                # pipeline failures (event.written with no terminal
                # extraction stage, retry-exhausted, permanent failures)
                # into an append-only snapshot that /health surfaces.
                ExpectationChecker(),
                EntityAuditWorker(),
                ConflictResolver(),
                Consolidator(),
                EntityCanonicalizer(),
                EntityDeduplicator(),
                EdgeConfidenceScorer(),
                EntityArticleWorker(),
                TemporalWorker(),
                SalienceWorker(),
                # Schema-Evolver (ADR-0003 Phase 4) — propose-only. Mines
                # kind-usage + kind_observations signals daily and drafts
                # bounded ontology-revision proposals into the quarantine
                # queue; nothing is applied without the operator (Phase 5).
                SchemaEvolver(),
                ModeSwitcher(),
                # Self-improvement tuner — observation mode (Phase B
                # held at promote_enabled=False until a ground-truth
                # eval-set lands, per the audit pass on 2026-06-03).
                # Without an eval-set, the LLM judge is the only gate,
                # and judge-judging-judge is research-grade dubious.
                # In observe mode the tuner still generates hypotheses
                # and runs replay + invariant guards, writing
                # hypothesis/observation rows to tuner_state — but it
                # returns BEFORE the judge panel, so no judge verdicts
                # (and no 3-vendor LLM cost) accumulate, and no tuned
                # value is ever mutated in production. Flip to True
                # once the eval framework is wired in.
                # RollbackMonitor stays registered but is effectively
                # vestigial while no promotes happen.
                Tuner(promote_enabled=False),
                RollbackMonitor(),
            ],
        )
        scheduler.start()

    # Background WAL-checkpoint loop — folds back the WAL file every 5
    # minutes so it doesn't grow unbounded on long-running servers.
    start_checkpoint_loop(
        settings.vault_dir,
        embedding_dim=settings.embedding_dim,
        interval_seconds=300,
    )

    # Auto-purge expired async-export artifacts (a full plaintext-equivalent
    # vault dump must not linger past its 72h download window).
    from .export_job import start_purge_loop

    start_purge_loop(settings, interval_seconds=3600)

    # Pre-warm in a background thread so boot stays fast but the first
    # user request hits an already-open SQLite connection AND an already-
    # warm OpenAI HTTPS connection. Pays ~1-2s of cold-start cost upfront
    # so the first real recall isn't penalized.
    _spawn_warmup(settings)

    mcp: FastMCP = FastMCP("afair", instructions=descriptions.SERVER_INSTRUCTIONS)

    # ── tools — descriptions are AI-facing prompts, see descriptions.py ─────

    @mcp.tool(description=descriptions.REMEMBER, version="1")
    def remember(
        content: schemas.RememberContentInput,
        context: str | None = None,
        type_hint: str | None = None,
        parent_hashes: list[str] | None = None,
        invalidates: list[str] | None = None,
    ) -> schemas.RememberResult:
        enforce_write_scope()
        # Narrow the widened ``RememberContent | str`` alias back to the concrete
        # union for the handler (and mypy). No-op when the wrap validator already
        # produced a model; defense-in-depth if a future FastMCP bypasses it.
        content = schemas.ensure_remember_content(content)
        return handlers.remember(
            content=content,
            context=context,
            type_hint=type_hint,
            parent_hashes=parent_hashes,
            invalidates=invalidates,
        )

    @mcp.tool(description=descriptions.RECALL, version="1", output_schema=_RECALL_OUTPUT_SCHEMA)
    def recall(
        query: str | None = None,
        scope: str | None = None,
        depth: schemas.Depth = "auto",
        limit: int | None = None,
        by_id: str | None = None,
        by_content_hash: str | None = None,
        full_payload: bool = False,
        stats: bool = False,
        feedback: schemas.RecallFeedback | None = None,
        decide: schemas.CorrectionDecision | list[schemas.CorrectionDecision] | None = None,
        pending_limit: int | None = None,
        pending_offset: int = 0,
        verbosity: schemas.RecallVerbosity = "compact",
        cursor: str | None = None,
    ) -> ToolResult:
        # decide= applies corrections (entity merges/retractions, observe
        # events) — a write, so it needs write scope like remember/observe.
        # feedback= only writes a best-effort tuner_state telemetry row
        # (no user-facing state), so read-only tokens may still give it. An
        # empty batch (decide=[]) decides nothing, so it needs no write scope.
        if decide is not None and (not isinstance(decide, list) or decide):
            enforce_write_scope()
        result = handlers.recall(
            query=query,
            scope=scope,
            depth=depth,
            limit=limit,
            by_id=by_id,
            by_content_hash=by_content_hash,
            full_payload=full_payload,
            stats=stats,
            feedback=feedback,
            decide=decide,
            pending_limit=pending_limit,
            pending_offset=pending_offset,
            verbosity=verbosity,
            cursor=cursor,
        )
        # Null-free serialization (P1-2 §4.3). fastmcp double-ships a TextContent
        # body + structuredContent; pydantic's default serializer includes every
        # null field. Return a ToolResult so BOTH copies drop None values —
        # exclude_none only (never exclude_defaults, which would silently strip
        # semantically-meaningful zeros/false the clients read). Every field is
        # optional-with-default, so an exclude_none dump still validates against
        # the advertised outputSchema passed to the decorator.
        return ToolResult(
            content=[MCPTextContent(type="text", text=result.model_dump_json(exclude_none=True))],
            structured_content=result.model_dump(exclude_none=True),
        )

    @mcp.tool(description=descriptions.OBSERVE, version="1")
    def observe(event: schemas.ObserveEventInput) -> schemas.ObserveResult:
        enforce_write_scope()
        # Narrow the widened ``ObserveEvent | str`` alias back to the concrete
        # model for the handler (and mypy). No-op when already a model.
        event = schemas.ensure_observe_event(event)
        return handlers.observe(event=event)

    # ── resources — auto-fetched by clients at session-init ─────────────────

    @mcp.resource(
        resources.SESSION_START_URI,
        name=resources.SESSION_START_NAME,
        description=resources.SESSION_START_DESCRIPTION,
        mime_type="application/json",
    )
    def session_start() -> dict[str, Any]:
        """Compact snapshot of the user's vault state for cold-start
        context. Returns mode + top-salient events + open threads so
        every new conversation begins already-aware."""
        db = connect_for_thread()
        return resources.read_session_start(db)

    # ── /health — orchestrator-facing, never goes through MCP protocol ──────

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        try:
            db = connect_for_thread()
            db.execute("SELECT 1").fetchone()
        except Exception as e:
            # Log only the exception class — str(e) often includes the
            # vault file path (e.g. "unable to open /data/vault/afair.db")
            # and we don't want that on log aggregators or in any
            # shared-dashboard pivot path. The orchestrator acts on the
            # 503 status, not the body (Sec audit I6).
            log.warning("health.degraded", exc_type=type(e).__name__)
            return JSONResponse(
                {"status": "degraded"},
                status_code=503,
            )

        # ── enrichment (Phase 0.5) — strictly additive on the OK branch ──
        # A backlog is a provider/workload condition, not an unhealthy
        # machine, so we stay 200 (a 503 would make Fly restart and kill
        # the cold-path scheduler mid-drain). The whole block is
        # try/except-isolated: observability must NEVER turn a healthy
        # vault into a 503. Body carries counts/ages/booleans/version
        # only — no payloads, names, error strings, or paths.
        from .. import __version__

        body: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "checks": {"db": True},
            "pipeline": None,
            "workers": None,
        }
        try:
            snapshot = observability.read_latest_snapshot(db)
            if snapshot is not None:
                body["pipeline"] = _health_pipeline_block(snapshot)
            if scheduler is not None:
                body["workers"] = {
                    name: {"seconds_since_last_success": st["seconds_since_last_success"]}
                    for name, st in scheduler.status().items()
                }
        except Exception as e:
            # Same path-hygiene rule as the degraded branch: log the class
            # only, never str(e) (it can carry the vault path).
            log.warning("health.enrich_failed", exc_type=type(e).__name__)
            body["pipeline"] = None
            body["workers"] = None
        return JSONResponse(body)

    return mcp


def _health_pipeline_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the /health ``pipeline`` block from the latest snapshot.

    Counts/ages/booleans only — ``write_snapshot`` guarantees integer
    counters, so nothing content-shaped can reach the response here.
    ``pipeline_ok`` is a convenience boolean; ``None`` only if the stored
    snapshot somehow lacks the violation count (defensive).
    """
    counters = snapshot["counters"]
    age_seconds: int | None
    try:
        recorded = datetime.fromisoformat(snapshot["recorded_at"])
        age_seconds = int((datetime.now(UTC) - recorded).total_seconds())
    except (ValueError, TypeError):
        age_seconds = None
    violations = counters.get("expectation_violations")
    return {
        "snapshot_age_seconds": age_seconds,
        "pipeline_ok": (violations == 0) if violations is not None else None,
        "expectation_violations": violations,
        "stuck_extractions": counters.get("stuck_extractions"),
        "pending_extraction_backlog": counters.get("pending_extraction_backlog"),
        "retry_exhausted": counters.get("retry_exhausted"),
        "permanent_failures": counters.get("permanent_failures"),
        "oldest_stuck_age_seconds": counters.get("oldest_stuck_age_seconds"),
    }


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
        # Scoped signup endpoint — does its own auth against the
        # signup-only bearer token. NOT exempted from auth in absolute
        # terms; the route handler enforces its own narrower credential
        # so the general bearer doesn't gate it.
        "/internal/signup",
        # Scoped export endpoint — same pattern. Route handler enforces
        # its own AFAIR_EXPORT_TOKEN bearer; this exempt entry just
        # lets the request reach the handler without the main MCP
        # bearer-or-JWT check rejecting it first.
        "/internal/export",
        # Token-management endpoints — gated by their own master-bearer
        # check (so sub-tokens cannot mint more sub-tokens). Exempting
        # them from the main middleware avoids a chicken-and-egg with
        # the JWT/auth-rate-limit stack.
        "/internal/tokens",
    }
    # Prefix-exempt: the token sub-routes (/internal/tokens/<id>) and the
    # async-export sub-routes (/internal/export/{request,status,download}),
    # each of which enforces its own credential (master bearer, or the
    # capability token on download).
    exempt_prefixes_set = ("/internal/tokens/", "/internal/export/")
    exempt_prefixes = ("/oauth/",)

    # Per-identity rate limiter. Instance lives for process lifetime so
    # buckets aren't lost on every request. Settings stay defaults; if we
    # need per-deployment tuning later, add to Settings.
    rate_limiter = TokenBucketRateLimiter()

    # Per-IP limiter for the /internal/* routes (signup, export, tokens),
    # which carry their own scoped bearer and are exempt from both the auth
    # middleware and the identity-bucketed rate limiter above. 30/min/IP is
    # generous for the web app's legitimate calls and prohibitive for a
    # leaked-scoped-bearer flood. (Security L5.)
    internal_rate_limiter = TokenBucketRateLimiter(
        requests_per_minute=30,
        burst_multiplier=2.0,
        max_identities=8192,
    )
    internal_rate_limited_prefixes = (
        "/internal/signup",
        "/internal/export",
        "/internal/tokens",
    )

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
        # + JSON envelope overhead. /internal/blob/upload is exempted
        # because it has its own per-chunk cap and would never finish
        # streaming a 100 MB upload under the 12 MB limit.
        Middleware(
            BodySizeLimitMiddleware,
            exempt_paths=("/internal/blob/upload",),
        ),
        # Authentication — must come BEFORE rate limiting so we don't burn
        # bucket entries on random unauthenticated probes.
        Middleware(
            BearerTokenMiddleware,
            settings=settings,
            static_token=static_token,
            exempt_paths=exempt_paths,
            exempt_prefixes=(*exempt_prefixes, *exempt_prefixes_set),
        ),
        # Rate limiter — per-token bucket, deny-with-429 above the cap.
        # Authenticated traffic only (auth already rejected unauthed).
        Middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            exempt_paths=exempt_paths,
            exempt_prefixes=(*exempt_prefixes, *exempt_prefixes_set),
        ),
        # Per-IP limiter for /internal/* — these self-auth with a scoped
        # bearer and are exempt above, so this is their only throttle (L5).
        Middleware(
            InternalPathRateLimitMiddleware,
            limiter=internal_rate_limiter,
            environment=settings.environment,
            protected_prefixes=internal_rate_limited_prefixes,
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
        # Hub-federated identity (default for the managed fleet). In
        # hub mode the MCP server does not talk to GitHub directly:
        # afair.ai's identity hub verifies the user and sends a signed
        # JWT here. A self-hosted github-mode instance uses the
        # callback route below instead.
        Route(
            "/oauth/identity/accept",
            oauth_routes.oauth_identity_accept,
            methods=["GET"],
        ),
        # Direct-GitHub callback. Reachable when
        # ``identity_backend="github"`` (the supported self-host path
        # for web-client logins). In hub mode (managed-fleet default)
        # this route is unused: GitHub redirects to afair.ai's hub URL,
        # never here.
        Route(
            "/oauth/identity/github/callback",
            oauth_routes.oauth_identity_github_callback,
            methods=["GET"],
        ),
        Route("/oauth/token", oauth_routes.oauth_token, methods=["POST"]),
        Route("/oauth/revoke", oauth_routes.oauth_revoke, methods=["POST"]),
        Route("/internal/signup", signup_endpoint, methods=["POST"]),
        # Streaming blob upload — Route-based but reads the body via
        # request.stream() so it never materializes the whole payload.
        # Exempted from BodySizeLimitMiddleware so files past 12 MB go
        # through (cap enforced per-chunk inside the handler).
        Route("/internal/blob/upload", blob_upload_endpoint, methods=["POST"]),
        # Vault export. Stream JSONL of every event + interpretation +
        # entity-graph row. Scoped bearer (AFAIR_EXPORT_TOKEN) — narrow
        # surface independent of the main MCP auth. See
        # afair/mcp/export_route.py.
        Route("/internal/export", export_endpoint, methods=["GET"]),
        # OPTIONS preflight so the browser dashboard's cross-origin export
        # fetch (afair.ai → vanity host, with the master bearer) isn't
        # pre-failed. Same shared handler the token routes use.
        Route("/internal/export", tokens_preflight_endpoint, methods=["OPTIONS"]),
        # Async export: request a job, poll its status, download the artifact.
        # request/status are cross-origin (dashboard + master bearer) → CORS +
        # preflight; download is a capability-token link → plain navigation.
        Route("/internal/export/request", export_request_endpoint, methods=["POST"]),
        Route("/internal/export/request", tokens_preflight_endpoint, methods=["OPTIONS"]),
        Route("/internal/export/status", export_status_endpoint, methods=["GET"]),
        Route("/internal/export/status", tokens_preflight_endpoint, methods=["OPTIONS"]),
        Route("/internal/export/download", export_download_endpoint, methods=["GET"]),
        # API token management. GET=list, POST=mint, DELETE one by id.
        # Handler enforces master-bearer auth (AFAIR_AUTH_TOKEN only —
        # minted sub-tokens cannot self-escalate). See tokens_route.py.
        Route("/internal/tokens", tokens_list_endpoint, methods=["GET"]),
        Route("/internal/tokens", tokens_mint_endpoint, methods=["POST"]),
        Route(
            "/internal/tokens",
            tokens_preflight_endpoint,
            methods=["OPTIONS"],
        ),
        Route(
            "/internal/tokens/{token_id}",
            tokens_revoke_endpoint,
            methods=["DELETE"],
        ),
        Route(
            "/internal/tokens/{token_id}",
            tokens_preflight_endpoint,
            methods=["OPTIONS"],
        ),
        Mount("/", app=mcp_app),
    ]

    # CRITICAL: pass FastMCP's lifespan to the parent app so its
    # StreamableHTTPSessionManager initializes correctly. We wrap it so the
    # anyio thread-limiter cap is applied INSIDE the event loop at startup
    # (P2a — memory ceiling; see _apply_thread_limiter_cap) before delegating.
    @contextlib.asynccontextmanager
    async def lifespan(app_: Starlette) -> Any:
        _apply_thread_limiter_cap(settings)
        async with mcp_app.lifespan(app_):
            yield

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    # Make settings accessible to OAuth route handlers via request.app.state.
    app.state.settings = settings
    return app


def _apply_thread_limiter_cap(settings: Settings) -> None:
    """Cap anyio's default thread limiter at ``settings.max_tool_threads``.

    MUST run inside the running event loop (the limiter is a per-loop RunVar);
    the app lifespan is exactly that context. MCP tools are sync, so each runs
    in a worker thread governed by this limiter — and each thread caches its
    own SQLite connection + page cache. anyio's stock 40 tokens can push the
    aggregate page cache past a 512MB-1GB Fly VM; capping it bounds the ceiling
    (12 x 16MB ~= 192MB) while keeping ample single-tenant concurrency.
    """
    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = settings.max_tool_threads
        log.info("thread_limiter.capped", total_tokens=settings.max_tool_threads)
    except Exception as e:  # never let a limiter hiccup block startup
        log.warning("thread_limiter.cap_failed", error=str(e))


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
            # Use embed_text (uncached path) for warmup so we don't pin a
            # "warmup" entry in the query-embedding LRU cache (Perf I7).
            # We want the HTTPS connection + provider state warm — not
            # the cache pre-populated with a string nobody will ever
            # query for.
            embed_text(model=settings.embedding_model, text="warmup", api_key=api_key)
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

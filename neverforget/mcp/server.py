"""FastMCP server wiring.

Registers the four v1 tools with their AI-facing descriptions and exposes
a /health endpoint for the orchestrator (Fly) to probe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from starlette.responses import JSONResponse

from ..substrate import open_db
from . import descriptions, handlers, schemas
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


def run(settings: Settings) -> None:
    """Run the MCP server until interrupted. Uses Streamable HTTP transport."""
    mcp = build_server(settings)
    mcp.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        show_banner=False,
    )

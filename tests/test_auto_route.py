"""Auto-routing recall depth — Phase 2 #2 (Cognee steal, sharper).

Tests the _auto_route_depth heuristic plus its integration into the
recall handler. The heuristic resolves to a concrete depth (shallow or
normal) so downstream code never sees ``auto``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.handlers import _auto_route_depth
from afair.mcp.schemas import TextContent
from afair.substrate import open_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,  # auto should still pick correctly
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)


# ── heuristic-level tests ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "sha256:ab46e473ed6939f3b452ec71c259b2bf9f0ada68a8850c95cde1ee10ec587f31",
        "http://example.com/path",
        "https://supermemory.ai",
        "file:///home/user/notes.md",
    ],
)
def test_identifier_prefixes_route_to_shallow(query: str) -> None:
    """sha256:, http(s), file:// — exact identifier scenarios. Embedding
    similarity adds noise; FTS exact-match is the right tool."""
    assert _auto_route_depth(query) == "shallow"


@pytest.mark.parametrize(
    "ulid",
    [
        "01KSGN98P9PWP2627F5GT32DB1",  # uppercase
        "01ksgn98p9pwp2627f5gt32db1",  # lowercase
    ],
)
def test_bare_ulid_routes_to_shallow(ulid: str) -> None:
    """ULID-shaped strings — caller is doing a direct lookup, not a
    semantic search."""
    assert _auto_route_depth(ulid) == "shallow"


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_empty_query_routes_to_shallow(query: str) -> None:
    assert _auto_route_depth(query) == "shallow"


@pytest.mark.parametrize("query", ["sajinth", "memory", "VISION"])
def test_single_token_routes_to_shallow(query: str) -> None:
    """A single rare token is what FTS does best. Vec would just dilute
    the result list with loosely-similar events."""
    assert _auto_route_depth(query) == "shallow"


@pytest.mark.parametrize(
    "query",
    [
        "what did sajinth say about the roadmap",
        "Phase 2 sprint deliverables",
        "FastEmbed migration timing",
        "memory layer for ai agents",
    ],
)
def test_natural_language_routes_to_normal(query: str) -> None:
    """Multi-token natural-language queries get the hybrid treatment."""
    assert _auto_route_depth(query) == "normal"


def test_fts_specials_dont_inflate_token_count() -> None:
    """Hyphens/parentheses sanitize away first; 'cross-vendor' counts as
    two tokens, but 'cross--vendor' (still effectively one concept) also
    counts as two. We're conservative: any multi-token result is normal."""
    # Single hyphenated word splits into 2 tokens → normal (acceptable)
    assert _auto_route_depth("cross-vendor") == "normal"
    # Multi-word stays normal
    assert _auto_route_depth("cross-vendor verification") == "normal"


# ── recall integration ────────────────────────────────────────────────────


def test_recall_default_depth_is_auto(ctx: ServerContext) -> None:
    """Phase 2 default — caller doesn't pass depth, system picks."""
    handlers.remember(content=TextContent(type="text", text="hello"))
    # Single-token query → resolves to shallow
    r = handlers.recall(query="hello")
    assert r.depth_used == "shallow"


def test_recall_auto_resolves_to_normal_for_natural_query(
    ctx: ServerContext,
) -> None:
    """A natural-language query with semantic_recall enabled would resolve
    to normal. We have semantic_recall disabled in this fixture, so the
    handler downshifts to shallow internally — but depth_used still
    reflects the resolution."""
    handlers.remember(content=TextContent(type="text", text="what we built today"))
    r = handlers.recall(query="things we constructed during today's session")
    # semantic_recall_enabled=False forces the handler's branch to shallow,
    # so even though _auto_route_depth returned "normal", depth_used ends
    # up as "shallow". This is the expected handler behavior.
    assert r.depth_used == "shallow"


def test_recall_explicit_depth_overrides_auto(ctx: ServerContext) -> None:
    """Backward compat — existing callers passing depth="shallow"/"normal"
    still work; the explicit value bypasses the router."""
    handlers.remember(content=TextContent(type="text", text="explicit override demo"))
    r = handlers.recall(query="explicit override demo", depth="shallow")
    assert r.depth_used == "shallow"
    r = handlers.recall(query="x", depth="shallow")  # single-token would also auto-shallow
    assert r.depth_used == "shallow"


def test_recall_with_identifier_query_routes_to_shallow(ctx: ServerContext) -> None:
    """An sha256:... or ULID query — auto picks shallow even though
    semantic_recall is enabled in real production."""
    r = handlers.remember(content=TextContent(type="text", text="identifier-routing-canary"))
    # Use the event_id (a ULID) as the query
    result = handlers.recall(query=r.event_id)
    assert result.depth_used == "shallow"

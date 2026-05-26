"""Tests for the root-path JSON pointer at ``/``.

Pre-2026-05-26 this served a full-page HTML manifesto. As of the
substrate/landing decoupling (see landing.py docstring + VISION.md
phase-8 architecture notes), it returns a minimal JSON pointer instead.
Marketing content lives at afair.ai on a separate deployment.
"""

from __future__ import annotations

import json

import pytest

from afair.mcp import landing


def test_pointer_payload_identifies_server() -> None:
    """The pointer self-identifies as afair-mcp so curious-but-not-MCP
    clients hitting / see clearly what kind of server this is."""
    assert landing._POINTER["server"] == "afair-mcp"


def test_pointer_payload_includes_mcp_endpoint_path() -> None:
    """Real MCP traffic goes to /mcp, not /. The pointer makes that
    explicit so a hand-curl visitor can find the protocol endpoint."""
    assert landing._POINTER["mcp_endpoint"] == "/mcp"


def test_pointer_payload_includes_marketing_and_source() -> None:
    """Two outbound pointers: where to learn about the product
    (afair.ai), and where to read the source / self-host
    (gowry/afair). Keeps the substrate machine itself OUT of any
    marketing role."""
    assert landing._POINTER["marketing"] == "https://afair.ai"
    assert landing._POINTER["source"] == "https://github.com/gowry/afair"


@pytest.mark.asyncio
async def test_index_returns_json_with_cache_header() -> None:
    """Handler returns 200 JSON with a 1-hour browser cache hint
    (vs the prior HTML page's 5-min hint — pointer changes only on
    redeploy, much rarer than the old manifesto was expected to)."""
    response = await landing.index(None)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert response.media_type == "application/json"
    cache = response.headers.get("Cache-Control", "")
    assert "max-age=3600" in cache, cache
    body = json.loads(response.body.decode("utf-8"))
    assert body["server"] == "afair-mcp"
    assert body["mcp_endpoint"] == "/mcp"


@pytest.mark.asyncio
async def test_index_response_is_compact_not_marketing() -> None:
    """The whole point of decoupling: this should be a small machine-
    readable pointer, not a long-form HTML page. Body should fit in a
    single TCP packet."""
    response = await landing.index(None)  # type: ignore[arg-type]
    # Generous upper bound — real payload is ~250 bytes. If this ever
    # creeps past 1 KB, someone is reverting toward marketing content
    # and should add a separate route instead.
    assert len(response.body) < 1024, (
        f"pointer body grew to {len(response.body)} bytes; "
        "marketing content belongs on afair.ai, not here"
    )

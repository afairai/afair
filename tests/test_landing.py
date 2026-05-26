"""Tests for the static landing page at ``/``."""

from __future__ import annotations

import pytest

from afair.mcp import landing


def test_landing_html_contains_manifesto() -> None:
    """The page renders the verbatim VISION.md §1 thesis."""
    html = landing._LANDING_HTML
    assert "Every individual owns" in html
    assert "cognitive sovereignty layer" in html
    assert "password managers liberated" in html  # phrase split across lines in source
    assert "credentials" in html
    assert "Codename" in html


def test_landing_html_contains_foundations() -> None:
    """VISION.md §2's four non-negotiable principles must all be present."""
    html = landing._LANDING_HTML
    assert "Foundations" in html
    # Each principle's title line.
    assert "owns the substrate" in html
    assert "Single-tenant" in html
    assert "Cross-vendor by default" in html
    assert "Schema is emergent" in html
    # Numbered list 01..04 in the source order.
    for n in ("01", "02", "03", "04"):
        assert f">{n}<" in html, f"missing principle number {n}"


def test_landing_html_contains_architecture_three_layers() -> None:
    """VISION.md §6 — the three architecture layers + invariant references."""
    html = landing._LANDING_HTML
    assert "Architecture" in html
    assert "MCP Surface" in html
    assert "Society of Mind" in html
    assert "Substrate" in html
    # Invariant references tie the layers to the constitution.
    for invariant in ("I1", "I2", "I3"):
        assert invariant in html, f"missing invariant reference {invariant}"


def test_landing_html_is_noindex() -> None:
    """Pre-launch — search engines should not surface this page yet."""
    assert 'name="robots" content="noindex"' in landing._LANDING_HTML


def test_landing_html_is_mobile_ready() -> None:
    """Viewport meta + a small-screen media query so iPhone/Android render
    the manifesto without horizontal scrolling. New sections also need
    responsive overrides."""
    html = landing._LANDING_HTML
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert "@media (max-width: 480px)" in html
    # Architecture rows collapse to a single column on small screens.
    assert ".layers > div {" in html


def test_landing_html_has_no_external_assets() -> None:
    """Privacy + performance — no third-party fonts, scripts, images, or
    analytics tags. System fonts only. The whole page in one document."""
    html = landing._LANDING_HTML
    for forbidden in (
        "googleapis.com",
        "gstatic.com",
        "cloudfront",
        "google-analytics",
        "googletagmanager",
        "<script",
        "<iframe",
        "<img",
    ):
        assert forbidden not in html, f"unexpected external asset: {forbidden!r}"


@pytest.mark.asyncio
async def test_landing_handler_returns_html_with_cache_header() -> None:
    """The handler returns 200 HTML with a short browser cache hint."""
    response = await landing.index(None)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert response.media_type == "text/html"
    cache = response.headers.get("Cache-Control", "")
    assert "max-age=300" in cache, cache
    body = response.body.decode("utf-8")
    assert "<title>afair — vision</title>" in body


def test_landing_html_has_no_outbound_links() -> None:
    """Manifesto stands alone — no /health diagnostic link, no external
    links, no internal nav. Reduces surface for indexing + analytics +
    visitor curiosity dead-ends."""
    html = landing._LANDING_HTML
    assert "<a " not in html, "manifesto should have zero outbound or internal links"


def test_landing_html_uses_semantic_section_landmarks() -> None:
    """Each non-hero block is a <section> with aria-labelledby pointing at
    its eyebrow heading. Helps screen readers + lighthouse a11y score."""
    html = landing._LANDING_HTML
    assert 'aria-labelledby="foundations"' in html
    assert 'aria-labelledby="architecture"' in html
    assert 'id="foundations"' in html
    assert 'id="architecture"' in html

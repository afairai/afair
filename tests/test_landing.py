"""Tests for the static landing page at ``/``."""

from __future__ import annotations

import pytest

from neverforget.mcp import landing


def test_landing_html_contains_manifesto() -> None:
    """The page renders the verbatim VISION.md §1 thesis."""
    html = landing._LANDING_HTML
    assert "Every individual owns" in html
    assert "cognitive sovereignty layer" in html
    assert "1Password liberated" in html  # phrase split across lines in source
    assert "credentials" in html
    assert "Codename" in html


def test_landing_html_is_noindex() -> None:
    """Pre-launch — search engines should not surface this page yet."""
    assert 'name="robots" content="noindex"' in landing._LANDING_HTML


def test_landing_html_is_mobile_ready() -> None:
    """Viewport meta + a small-screen media query so iPhone/Android render
    the manifesto without horizontal scrolling."""
    html = landing._LANDING_HTML
    assert 'name="viewport"' in html
    assert "width=device-width" in html
    assert "@media (max-width: 480px)" in html


def test_landing_html_has_no_external_assets() -> None:
    """Privacy + performance — no third-party fonts, scripts, images, or
    analytics tags. System fonts only. The whole page in one document."""
    html = landing._LANDING_HTML
    # No third-party hosts; no analytics; no remote anything.
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
    assert "<title>neverforget — vision</title>" in body


def test_landing_html_links_to_health() -> None:
    """Footer points at /health so a visitor with curiosity gets a 200 JSON
    rather than a dead end. Cheap diagnostic surface for the public."""
    assert 'href="/health"' in landing._LANDING_HTML

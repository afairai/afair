"""Security-header middleware coverage (P2d).

The SecurityHeadersMiddleware injects a fixed header set onto every response.
Untested before P2d; one refactor could silently drop the headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.context import clear_context
from afair.mcp.server import build_app
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    clear_context()
    try:
        yield
    finally:
        clear_context()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token="test-token",  # type: ignore[arg-type]
    )


def test_security_headers_present_on_response(tmp_path: Path) -> None:
    with TestClient(build_app(_settings(tmp_path))) as client:
        # The landing page is public (no auth) → a clean 200 to inspect.
        resp = client.get("/")
    assert resp.status_code == 200
    h = resp.headers
    assert h["strict-transport-security"] == "max-age=63072000; includeSubDomains; preload"
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in h["permissions-policy"]
    assert h["x-robots-tag"] == "noindex"
    assert "content-security-policy" in h


def test_security_headers_present_on_4xx(tmp_path: Path) -> None:
    """Headers apply even to rejections from inner middlewares (belt-and-
    suspenders) — an unauthenticated MCP POST is rejected but still carries
    them."""
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "x"})
    assert resp.status_code in (401, 403, 400, 406)
    assert resp.headers["x-content-type-options"] == "nosniff"

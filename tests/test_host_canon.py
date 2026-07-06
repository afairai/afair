"""Host-canonicalization middleware tests (P2d).

The fly.dev-vs-vanity connect footgun: a client that dials the raw Fly alias
instead of the vanity issuer gets an aud/resource mismatch. The middleware
redirects browser/discovery GETs to the canonical host and 421s a mis-hosted
MCP POST, WITHOUT widening the token audience.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp.host_canon import HostCanonicalizationMiddleware

CANONICAL = "https://alice.mcp.afair.ai"


async def _ok(_request: object) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _app(*, environment: str = "fly", issuer: str | None = CANONICAL) -> Starlette:
    return Starlette(
        routes=[
            Route("/", _ok, methods=["GET", "HEAD"]),
            Route("/.well-known/oauth-protected-resource", _ok, methods=["GET"]),
            Route("/oauth/authorize", _ok, methods=["GET"]),
            Route("/mcp", _ok, methods=["GET", "POST"]),
            Route("/health", _ok, methods=["GET"]),
            Route("/internal/blob/upload", _ok, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                HostCanonicalizationMiddleware,
                environment=environment,
                issuer=issuer,
            )
        ],
    )


def _client(base_url: str, **kw: object) -> TestClient:
    return TestClient(_app(**kw), base_url=base_url)  # type: ignore[arg-type]


# ── non-canonical host ──────────────────────────────────────────────────────


def test_discovery_get_redirects_308_to_canonical() -> None:
    with _client("https://vault-abc.fly.dev") as client:
        r = client.get(
            "/.well-known/oauth-protected-resource",
            follow_redirects=False,
        )
    assert r.status_code == 308
    assert r.headers["location"] == f"{CANONICAL}/.well-known/oauth-protected-resource"


def test_root_get_redirect_preserves_query() -> None:
    with _client("https://vault-abc.fly.dev") as client:
        r = client.get("/?foo=bar", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == f"{CANONICAL}/?foo=bar"


def test_mcp_post_on_wrong_host_returns_421() -> None:
    with _client("https://vault-abc.fly.dev") as client:
        r = client.post("/mcp", json={"jsonrpc": "2.0"})
    assert r.status_code == 421
    body = r.json()
    assert body["error"] == "misdirected_request"
    assert f"{CANONICAL}/mcp" in body["detail"]


def test_health_never_redirected_on_wrong_host() -> None:
    """Fly probes the internal address; /health must pass through."""
    with _client("https://vault-abc.fly.dev") as client:
        r = client.get("/health", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "ok"


def test_internal_path_not_redirected_on_wrong_host() -> None:
    """Control-plane may call the .fly.dev internal name — /internal/* passes."""
    with _client("https://vault-abc.fly.dev") as client:
        r = client.post("/internal/blob/upload", content=b"x", follow_redirects=False)
    assert r.status_code == 200


# ── canonical host passes through ───────────────────────────────────────────


def test_canonical_host_passes_through() -> None:
    with _client(CANONICAL) as client:
        assert client.get("/.well-known/oauth-protected-resource").text == "ok"
        assert client.post("/mcp", json={}).text == "ok"


def test_canonical_host_ignores_default_port() -> None:
    """Host with an explicit :443 still matches the canonical netloc."""
    with TestClient(
        _app(), base_url="https://alice.mcp.afair.ai", headers={"host": "alice.mcp.afair.ai:443"}
    ) as client:
        r = client.get("/.well-known/oauth-protected-resource", follow_redirects=False)
    assert r.status_code == 200


# ── disabled (no-op) cases ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("environment", "issuer"),
    [("local", CANONICAL), ("fly", None)],
)
def test_middleware_noop_when_disabled(environment: str, issuer: str | None) -> None:
    """Not fly, or no explicit issuer → transparent pass-through even on a
    completely different Host (this is what keeps every local HTTP test green)."""
    with _client("https://anything.example.com", environment=environment, issuer=issuer) as client:
        r = client.get("/.well-known/oauth-protected-resource", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "ok"

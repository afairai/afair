"""OAuth DCR endpoint hardening tests (Sec audit I1).

Covers /oauth/register's per-IP rate limit, body-size cap, and the
redirect_uris allowlist (scheme, length, count). These are the
unauthenticated public surface; without these caps an attacker can
enumerate or DoS the endpoint without any credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.context import clear_context
from afair.mcp.oauth import routes as oauth_routes
from afair.mcp.server import build_app
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    """Each test gets a fresh DCR rate-limit bucket map."""
    oauth_routes._DCR_RATE_LIMITER.reset()
    clear_context()
    try:
        yield
    finally:
        oauth_routes._DCR_RATE_LIMITER.reset()
        clear_context()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token="test-token",  # type: ignore[arg-type]
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(_settings(tmp_path)))


# ── happy path ─────────────────────────────────────────────────────────────


def test_dcr_accepts_standard_registration(tmp_path: Path) -> None:
    """Baseline: real-world MCP-client registration shape succeeds."""
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://claude.ai/oauth/callback"],
                "client_name": "Claude.ai",
                "token_endpoint_auth_method": "none",
            },
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload["client_id"], str) and payload["client_id"]
    assert payload["redirect_uris"] == ["https://claude.ai/oauth/callback"]


def test_dcr_accepts_loopback_http(tmp_path: Path) -> None:
    """Native-client testing pattern: http://localhost is allowed."""
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://localhost:8765/cb"]},
        )
    assert response.status_code == 201, response.text


def test_dcr_accepts_custom_reverse_dns_scheme(tmp_path: Path) -> None:
    """Mobile/native apps use reverse-DNS custom schemes — allowed."""
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["com.example.app://oauth-callback"]},
        )
    assert response.status_code == 201, response.text


# ── scheme allowlist ───────────────────────────────────────────────────────


def test_dcr_rejects_non_loopback_http(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://example.com/cb"]},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_dcr_rejects_javascript_scheme(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["javascript:alert(1)"]},
        )
    assert response.status_code == 400
    assert "javascript" in response.json()["error_description"]


def test_dcr_rejects_file_scheme(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["file:///etc/passwd"]},
        )
    assert response.status_code == 400


def test_dcr_rejects_single_segment_custom_scheme(tmp_path: Path) -> None:
    """Custom schemes must look like reverse-DNS to limit name squatting."""
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": ["weirdscheme://callback"]},
        )
    assert response.status_code == 400
    assert "reverse-DNS" in response.json()["error_description"]


# ── size + count caps ──────────────────────────────────────────────────────


def test_dcr_rejects_oversized_redirect_uri(tmp_path: Path) -> None:
    """A redirect_uri > 2048 chars is rejected."""
    long_uri = "https://example.com/cb?" + "x" * 3000
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": [long_uri]},
        )
    assert response.status_code == 400
    assert "2048" in response.json()["error_description"]


def test_dcr_rejects_too_many_redirect_uris(tmp_path: Path) -> None:
    """More than 8 entries: rejected."""
    uris = [f"https://example.com/cb{i}" for i in range(20)]
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={"redirect_uris": uris},
        )
    assert response.status_code == 400


def test_dcr_rejects_oversized_client_name(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://x.com/cb"],
                "client_name": "x" * 500,
            },
        )
    assert response.status_code == 400
    assert "client_name" in response.json()["error_description"]


def test_dcr_rejects_oversized_body(tmp_path: Path) -> None:
    """A body bigger than 16 KB is rejected."""
    big = {
        "redirect_uris": ["https://x.com/cb"],
        "filler": "x" * 20_000,
    }
    with _client(tmp_path) as client:
        response = client.post("/oauth/register", json=big)
    assert response.status_code == 413


# ── per-IP rate limit ──────────────────────────────────────────────────────


def test_dcr_rate_limit_caps_registrations_per_ip(tmp_path: Path) -> None:
    """After bucket capacity (10) the IP gets 429.

    TestClient defaults to the loopback address; we override Fly-Client-IP
    so the limiter sees a stable, attacker-controlled key.
    """
    with _client(tmp_path) as client:
        body = {"redirect_uris": ["https://example.com/cb"]}
        # Burn the bucket — 10 registrations are allowed; the 11th must
        # 429 from the same IP.
        for i in range(10):
            r = client.post(
                "/oauth/register",
                json=body,
                headers={"Fly-Client-IP": "203.0.113.5"},
            )
            assert r.status_code == 201, f"call {i} unexpectedly rejected: {r.text}"

        denied = client.post(
            "/oauth/register",
            json=body,
            headers={"Fly-Client-IP": "203.0.113.5"},
        )
        assert denied.status_code == 429
        assert denied.headers.get("Retry-After")
        assert denied.json()["error"] == "rate_limited"


def test_dcr_rate_limit_is_per_ip(tmp_path: Path) -> None:
    """Two different source IPs each have their own bucket."""
    with _client(tmp_path) as client:
        body = {"redirect_uris": ["https://example.com/cb"]}
        for _ in range(10):
            assert (
                client.post(
                    "/oauth/register",
                    json=body,
                    headers={"Fly-Client-IP": "203.0.113.1"},
                ).status_code
                == 201
            )
        # First IP is now drained — second IP still has a fresh bucket.
        r = client.post(
            "/oauth/register",
            json=body,
            headers={"Fly-Client-IP": "203.0.113.2"},
        )
        assert r.status_code == 201


# ── /oauth/revoke rate limit (Sec audit M2) ────────────────────────────────


def test_revoke_rate_limit_per_ip(tmp_path: Path) -> None:
    """Unauthenticated revoke needs DoS protection — burst beyond the cap
    returns 429 with Retry-After."""
    with _client(tmp_path) as client:
        for _ in range(10):
            r = client.post(
                "/oauth/revoke",
                data={"token": "anything-nonsense"},
                headers={"Fly-Client-IP": "203.0.113.7"},
            )
            assert r.status_code == 200
        denied = client.post(
            "/oauth/revoke",
            data={"token": "anything-nonsense"},
            headers={"Fly-Client-IP": "203.0.113.7"},
        )
        assert denied.status_code == 429
        assert denied.headers.get("Retry-After")


def test_revoke_and_register_buckets_are_separate(tmp_path: Path) -> None:
    """register and revoke use distinct identity prefixes — burning one
    doesn't leak into the other."""
    with _client(tmp_path) as client:
        # Burn the revoke bucket for IP X.
        for _ in range(10):
            client.post(
                "/oauth/revoke",
                data={"token": "t"},
                headers={"Fly-Client-IP": "203.0.113.8"},
            )
        # The 11th revoke should be 429.
        assert (
            client.post(
                "/oauth/revoke",
                data={"token": "t"},
                headers={"Fly-Client-IP": "203.0.113.8"},
            ).status_code
            == 429
        )
        # Same IP can still register — separate bucket prefix.
        assert (
            client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://example.com/cb"]},
                headers={"Fly-Client-IP": "203.0.113.8"},
            ).status_code
            == 201
        )


def test_redirect_uri_registered_loopback_port_agnostic() -> None:
    """RFC 8252 §7.3: a registered loopback redirect matches any port (native
    clients use an ephemeral port that differs between register + authorize).
    Non-loopback redirects stay strict exact-match."""
    from afair.mcp.oauth.routes import _redirect_uri_registered as m

    reg = ["http://localhost:51160/callback"]
    # Exact match.
    assert m("http://localhost:51160/callback", reg) is True
    # Different port on the same loopback host + path → accepted.
    assert m("http://localhost:49222/callback", reg) is True
    # Different loopback host (127.0.0.1), same path → accepted.
    assert m("http://127.0.0.1:8080/callback", reg) is True
    # Different path → rejected.
    assert m("http://localhost:51160/other", reg) is False
    # Non-loopback presented → strict (must be exact in the list).
    assert m("https://evil.example/callback", reg) is False
    # Non-loopback registered (web client) stays exact — different port fails.
    web = ["https://claude.ai/api/mcp/auth_callback"]
    assert m("https://claude.ai/api/mcp/auth_callback", web) is True
    assert m("https://claude.ai:8443/api/mcp/auth_callback", web) is False

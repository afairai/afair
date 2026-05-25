"""HTTP bearer-token authentication tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from neverforget.mcp.context import clear_context
from neverforget.mcp.server import build_app
from neverforget.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


SAMPLE_TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test gets a clean context and a no-op extractor."""
    monkeypatch.setattr(
        "neverforget.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )
    clear_context()
    try:
        yield
    finally:
        clear_context()


def _settings(tmp_path: Path, *, token: str | None = SAMPLE_TOKEN) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token=token,  # type: ignore[arg-type]
    )


# ── boot validator ─────────────────────────────────────────────────────────


def test_production_boot_requires_auth_token(tmp_path: Path) -> None:
    """ENVIRONMENT=fly without a token must refuse to boot."""
    with pytest.raises(ValidationError, match="NEVERFORGET_AUTH_TOKEN"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment="fly",
            vault_dir=tmp_path,
            auth_token=None,
        )


def test_local_boot_without_token_allowed(tmp_path: Path) -> None:
    """Local dev may omit the token (loopback binding contains the risk)."""
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        auth_token=None,
    )
    assert s.auth_token is None


# ── HTTP middleware ────────────────────────────────────────────────────────


def test_health_does_not_require_auth(tmp_path: Path) -> None:
    """Fly's orchestrator probes /health without the token."""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_mcp_request_without_token_returns_401(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_mcp_request_with_wrong_token_returns_401(tmp_path: Path) -> None:
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={
                "Authorization": "Bearer wrong-token",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 401


def test_mcp_request_with_correct_token_passes_middleware(tmp_path: Path) -> None:
    """Correct token passes the middleware. We don't assert MCP-protocol
    semantics here (that's covered by test_mcp_server.py); we just verify
    the gate opens."""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )
    # Anything other than 401 confirms the middleware let it through.
    assert response.status_code != 401


def test_loopback_dev_no_token_passes_through(tmp_path: Path) -> None:
    """When auth_token is None (local dev), middleware does not block."""
    app = build_app(_settings(tmp_path, token=None))
    with TestClient(app) as client:
        response = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    # No 401 — middleware is a pass-through in this mode.
    assert response.status_code != 401


def test_token_comparison_is_constant_time(tmp_path: Path) -> None:
    """Smoke that we accept the right token and reject any other, regardless
    of length. Doesn't measure timing directly — that would be flaky in CI —
    but exercises the code path. The constant-time guarantee comes from
    hmac.compare_digest itself."""
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        for bad in ("", "x", SAMPLE_TOKEN[:-1], SAMPLE_TOKEN + "x"):
            response = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    "Authorization": f"Bearer {bad}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 401, f"expected 401 for {bad!r}"

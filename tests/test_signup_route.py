"""Scoped /internal/signup endpoint.

Closes Security audit I7. The afair-web Next.js landing page used to
hold the full ``AFAIR_AUTH_TOKEN`` so it could call MCP's ``remember``.
Compromise of the web app meant total vault access. The new
``/internal/signup`` endpoint accepts a separate, narrower bearer
(``AFAIR_SIGNUP_TOKEN``) that opens ONE specific door — writing a
single kind of remember-event — and nothing else.

These tests pin the boundary: the right token writes one event with
the standard shape, every other auth state is rejected, and the
general bearer token does NOT work on this endpoint either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

from afair.mcp.signup_route import SIGNUP_CONTEXT, SIGNUP_ORIGIN, SIGNUP_TYPE_HINT
from afair.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


SIGNUP_TOKEN = "scoped-test-token-only-for-signups"
OTHER_TOKEN = "general-bearer-must-not-work-here"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Build the full Starlette app with both tokens configured."""
    from afair.mcp.context import clear_context
    from afair.mcp.server import build_app

    clear_context()
    settings = Settings(
        vault_dir=tmp_path,
        auth_token=SecretStr(OTHER_TOKEN),
        signup_token=SecretStr(SIGNUP_TOKEN),
        cold_path_enabled=False,  # no background workers in tests
    )
    app = build_app(settings)
    with TestClient(app) as c:
        yield c
    clear_context()


# ── auth boundary ──────────────────────────────────────────────────────────


def test_rejects_missing_authorization(client: TestClient) -> None:
    r = client.post("/internal/signup", json={"email": "a@example.com"})
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_rejects_wrong_bearer(client: TestClient) -> None:
    r = client.post(
        "/internal/signup",
        json={"email": "a@example.com"},
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert r.status_code == 401


def test_rejects_general_bearer_token(client: TestClient) -> None:
    """The general AFAIR_AUTH_TOKEN must NOT open the signup endpoint."""
    r = client.post(
        "/internal/signup",
        json={"email": "a@example.com"},
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )
    assert r.status_code == 401


def test_accepts_signup_bearer(client: TestClient) -> None:
    r = client.post(
        "/internal/signup",
        json={"email": "founder@example.com"},
        headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["event_id"].startswith("01")  # ULID
    assert body["content_hash"].startswith("sha256:")


# ── disabled when no signup token configured ───────────────────────────────


def test_endpoint_503_when_signup_token_unset(tmp_path: Path) -> None:
    from afair.mcp.context import clear_context
    from afair.mcp.server import build_app

    clear_context()
    settings = Settings(
        vault_dir=tmp_path,
        auth_token=SecretStr(OTHER_TOKEN),
        signup_token=None,  # not configured
        cold_path_enabled=False,
    )
    with TestClient(build_app(settings)) as client:
        r = client.post(
            "/internal/signup",
            json={"email": "a@example.com"},
            headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
        )
    clear_context()
    assert r.status_code == 503
    assert r.json()["error"] == "signup_endpoint_disabled"


# ── request validation ─────────────────────────────────────────────────────


def test_rejects_invalid_email(client: TestClient) -> None:
    r = client.post(
        "/internal/signup",
        json={"email": "not-an-email"},
        headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
    )
    assert r.status_code == 400


def test_rejects_invalid_json(client: TestClient) -> None:
    r = client.post(
        "/internal/signup",
        content=b"not json at all",
        headers={
            "Authorization": f"Bearer {SIGNUP_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 400


def test_caps_source_field_length(client: TestClient) -> None:
    """Pydantic Field(max_length=80) blocks attempts to flood the payload."""
    r = client.post(
        "/internal/signup",
        json={"email": "a@example.com", "source": "X" * 200},
        headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
    )
    assert r.status_code == 400


# ── event written has the agreed shape ─────────────────────────────────────


def test_event_payload_carries_signup_context_and_type_hint(
    client: TestClient, tmp_path: Path
) -> None:
    """The event written must use the standard signup shape so existing
    recall filters on context/type_hint still surface these."""
    r = client.post(
        "/internal/signup",
        json={"email": "early-user@example.com", "source": "twitter"},
        headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
    )
    assert r.status_code == 200, r.text

    # Verify on disk — read the event back through the substrate.
    from afair.substrate import open_db, read_event_by_hash

    body = r.json()
    db = open_db(tmp_path)
    try:
        event = read_event_by_hash(db, body["content_hash"])
        assert event is not None
        assert event.kind == "remember"
        assert event.origin == SIGNUP_ORIGIN
        assert event.payload["context"] == SIGNUP_CONTEXT
        assert event.payload["type_hint"] == SIGNUP_TYPE_HINT
        assert event.payload["text"] == "early-user@example.com"
        assert event.payload["source"] == "twitter"
    finally:
        db.close()


# ── method discipline ──────────────────────────────────────────────────────


def test_get_not_allowed(client: TestClient) -> None:
    """Only POST is wired. GET must NOT execute the signup handler — either
    405 (route exists POST-only) or 404 (route falls through to MCP mount,
    which doesn't match either) is acceptable; both block the handler."""
    r = client.get(
        "/internal/signup",
        headers={"Authorization": f"Bearer {SIGNUP_TOKEN}"},
    )
    assert r.status_code in (404, 405)
    # Whatever the status, no event was written — body should not contain ok:true.
    assert b'"ok":true' not in r.content

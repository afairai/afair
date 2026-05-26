"""Tests for the OAuth SQLite storage layer + JWT issuance."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from afair.mcp.oauth import jwt as jwt_mod
from afair.mcp.oauth import storage
from afair.settings import Settings
from afair.substrate import open_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        jwt_secret="unit-test-secret-do-not-use-in-prod",  # type: ignore[arg-type]
        oauth_issuer="https://test.local/",
    )


# ── client registration (DCR) ───────────────────────────────────────────────


def test_register_public_client_returns_no_secret(db: sqlite3.Connection) -> None:
    client, secret = storage.register_client(
        db,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        client_name="claude.ai",
        confidential=False,
    )
    assert client.client_id.startswith("nf_")
    assert client.redirect_uris == ("https://claude.ai/api/mcp/auth_callback",)
    assert not client.has_secret
    assert secret is None


def test_register_confidential_client_returns_secret(db: sqlite3.Connection) -> None:
    client, secret = storage.register_client(
        db,
        redirect_uris=["https://example.test/cb"],
        client_name="test",
        confidential=True,
    )
    assert client.has_secret
    assert secret is not None
    # Verify the secret round-trips
    assert storage.verify_client_secret(db, client.client_id, secret) is True
    assert storage.verify_client_secret(db, client.client_id, "wrong") is False


def test_get_client_returns_none_for_unknown(db: sqlite3.Connection) -> None:
    assert storage.get_client(db, "nf_does_not_exist") is None


# ── authorization codes ─────────────────────────────────────────────────────


def test_save_then_consume_authorization_code(db: sqlite3.Connection) -> None:
    saved = storage.save_authorization_code(
        db,
        client_id="nf_test",
        redirect_uri="https://example.test/cb",
        scope="mcp",
        code_challenge="abc123",
        code_challenge_method="S256",
        user_sub="gowrynath",
        user_email="gowrynath@example.com",
    )
    assert saved.code.startswith("nfac_")

    consumed = storage.consume_authorization_code(db, saved.code)
    assert consumed is not None
    assert consumed.user_sub == "gowrynath"
    assert consumed.code_challenge == "abc123"

    # Single-use — second consume returns None
    again = storage.consume_authorization_code(db, saved.code)
    assert again is None


def test_consume_unknown_code_returns_none(db: sqlite3.Connection) -> None:
    assert storage.consume_authorization_code(db, "nfac_nope") is None


# ── login state (the identity-backend dance) ───────────────────────────────


def test_save_then_consume_login_state(db: sqlite3.Connection) -> None:
    saved = storage.save_login_state(
        db,
        client_id="nf_test",
        redirect_uri="https://example.test/cb",
        scope="mcp",
        code_challenge="abc",
        code_challenge_method="S256",
        client_state="opaque-from-mcp-client",
    )
    consumed = storage.consume_login_state(db, saved.state)
    assert consumed is not None
    assert consumed.client_state == "opaque-from-mcp-client"

    # Single-use
    assert storage.consume_login_state(db, saved.state) is None


# ── refresh tokens ─────────────────────────────────────────────────────────


def test_refresh_token_round_trip(db: sqlite3.Connection) -> None:
    token = storage.issue_refresh_token(
        db, client_id="nf_test", user_sub="gowrynath", scope=None, ttl_seconds=60
    )
    assert token.startswith("nfrt_")

    found = storage.lookup_refresh_token(db, token)
    assert found is not None
    assert found.user_sub == "gowrynath"

    # Revoke; subsequent lookup fails
    assert storage.revoke_refresh_token(db, token) is True
    assert storage.lookup_refresh_token(db, token) is None
    # Idempotent — revoking again is a no-op
    assert storage.revoke_refresh_token(db, token) is False


def test_expired_refresh_token_is_rejected(db: sqlite3.Connection) -> None:
    token = storage.issue_refresh_token(
        db, client_id="nf_test", user_sub="gowrynath", scope=None, ttl_seconds=-1
    )
    # Already expired (ttl_seconds=-1)
    assert storage.lookup_refresh_token(db, token) is None


# ── JWT issuance + validation ──────────────────────────────────────────────


def test_jwt_round_trip(settings: Settings) -> None:
    issued = jwt_mod.issue_access_token(
        settings=settings, subject="gowrynath", email="gowrynath@example.com"
    )
    assert issued.subject == "gowrynath"
    assert issued.expires_at > time.time()

    claims = jwt_mod.validate(issued.token, settings=settings)
    assert claims.sub == "gowrynath"
    assert claims.email == "gowrynath@example.com"
    assert claims.iss == "https://test.local"  # trailing slash stripped


def test_jwt_invalid_signature_rejected(settings: Settings, tmp_path: Path) -> None:
    issued = jwt_mod.issue_access_token(settings=settings, subject="gowrynath", email=None)
    # Different secret → invalid signature
    other = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        jwt_secret="different-secret",  # type: ignore[arg-type]
        oauth_issuer="https://test.local/",
    )
    with pytest.raises(jwt_mod.JWTInvalid):
        jwt_mod.validate(issued.token, settings=other)


def test_jwt_wrong_audience_rejected(settings: Settings) -> None:
    issued = jwt_mod.issue_access_token(
        settings=settings, subject="gowrynath", email=None, audience="https://other.example/"
    )
    with pytest.raises(jwt_mod.JWTInvalid):
        jwt_mod.validate(issued.token, settings=settings)


def test_jwt_no_secret_raises(tmp_path: Path) -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        jwt_secret=None,
    )
    with pytest.raises(jwt_mod.JWTError):
        jwt_mod.issue_access_token(settings=s, subject="x", email=None)


def test_generate_secret_is_random_and_url_safe() -> None:
    s1 = jwt_mod.generate_secret()
    s2 = jwt_mod.generate_secret()
    assert s1 != s2
    assert len(s1) >= 32
    # token_urlsafe characters only
    assert all(c.isalnum() or c in "-_" for c in s1)

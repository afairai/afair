"""Refresh-token grant hardening (P2d): client binding, rotation, reuse.

Before P2d, `_grant_refresh_token` never bound the token to the presenting
client, never rotated, and had no reuse detection — a leaked refresh token
was replayable forever. These tests pin the auth-code-grade behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.context import clear_context
from afair.mcp.oauth import storage
from afair.mcp.server import build_app
from afair.settings import Settings
from afair.substrate import open_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


USER = "operator"


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
        identity_allowlist=USER,
        jwt_secret="test-jwt-secret-not-for-production-use",  # type: ignore[arg-type]
    )


def _seed_public_client(tmp_path: Path) -> str:
    conn = open_db(tmp_path)
    try:
        client, _ = storage.register_client(
            conn,
            redirect_uris=["https://claude.ai/cb"],
            client_name="claude.ai",
            confidential=False,
        )
    finally:
        conn.close()
    return client.client_id


def _issue_refresh(tmp_path: Path, *, client_id: str) -> str:
    conn = open_db(tmp_path)
    try:
        return storage.issue_refresh_token(
            conn, client_id=client_id, user_sub=USER, scope="read", ttl_seconds=3600
        )
    finally:
        conn.close()


def _refresh(client: TestClient, *, token: str, client_id: str | None) -> object:
    data = {"grant_type": "refresh_token", "refresh_token": token}
    if client_id is not None:
        data["client_id"] = client_id
    return client.post("/oauth/token", data=data)


def test_refresh_success_rotates_token(tmp_path: Path) -> None:
    client_id = _seed_public_client(tmp_path)
    token = _issue_refresh(tmp_path, client_id=client_id)
    with TestClient(build_app(_settings(tmp_path))) as http:
        resp = _refresh(http, token=token, client_id=client_id)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"]
        new_token = body["refresh_token"]
        assert new_token and new_token != token

        # The rotated token is usable and chains to a further rotation. (The
        # old token's single-use death is asserted in the reuse test, which
        # also triggers family invalidation — so it can't be checked here
        # without killing new_token.)
        chained = _refresh(http, token=new_token, client_id=client_id)
        assert chained.status_code == 200, chained.text
        assert chained.json()["refresh_token"] not in (token, new_token)


def test_refresh_missing_client_id_rejected(tmp_path: Path) -> None:
    client_id = _seed_public_client(tmp_path)
    token = _issue_refresh(tmp_path, client_id=client_id)
    with TestClient(build_app(_settings(tmp_path))) as http:
        resp = _refresh(http, token=token, client_id=None)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_refresh_wrong_client_id_rejected(tmp_path: Path) -> None:
    """A token issued to client A presented with client B's id → invalid_grant."""
    client_a = _seed_public_client(tmp_path)
    client_b = _seed_public_client(tmp_path)
    token = _issue_refresh(tmp_path, client_id=client_a)
    with TestClient(build_app(_settings(tmp_path))) as http:
        resp = _refresh(http, token=token, client_id=client_b)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_refresh_reuse_invalidates_family(tmp_path: Path) -> None:
    """Replaying a rotated-out token revokes the whole family for that
    client+user — so the token that replaced it also stops working."""
    client_id = _seed_public_client(tmp_path)
    token = _issue_refresh(tmp_path, client_id=client_id)
    with TestClient(build_app(_settings(tmp_path))) as http:
        first = _refresh(http, token=token, client_id=client_id)
        new_token = first.json()["refresh_token"]

        # Replay the rotated-out (revoked) original → reuse detected.
        replay = _refresh(http, token=token, client_id=client_id)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

        # Family invalidation: the legitimately-rotated token is now dead too.
        after = _refresh(http, token=new_token, client_id=client_id)
        assert after.status_code == 400
        assert after.json()["error"] == "invalid_grant"

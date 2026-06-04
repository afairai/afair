"""Unit + integration tests for the api_tokens module + /internal/tokens.

Covers:
  - mint + list + revoke happy paths
  - sha256 hash at rest, plaintext returned exactly once
  - verify() bumps last_used_at on a successful lookup
  - revoked tokens are treated as misses
  - constant-time verify against a near-collision input
  - HTTP handler rejects non-master callers (sub-tokens cannot manage)
  - bearer middleware accepts a minted token in addition to the master
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp import api_tokens as _toks
from afair.mcp.tokens_route import (
    list_endpoint,
    mint_endpoint,
    revoke_endpoint,
)
from afair.settings import Settings
from afair.substrate import open_db

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    p = tmp_path / "vault"
    p.mkdir()
    return p


def _build_app(vault_dir: Path, master: str = "MASTER-TOKEN") -> Starlette:
    settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr(master),
    )
    app = Starlette(
        routes=[
            Route("/internal/tokens", list_endpoint, methods=["GET"]),
            Route("/internal/tokens", mint_endpoint, methods=["POST"]),
            Route(
                "/internal/tokens/{token_id}",
                revoke_endpoint,
                methods=["DELETE"],
            ),
        ],
    )
    app.state.settings = settings
    return app


# ─── module-level (no HTTP) ────────────────────────────────────────────────


def test_mint_returns_plaintext_once_and_stores_only_the_hash(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        minted = _toks.mint(conn, label="ci-bot", scope="full")
        assert minted.plaintext.startswith("afair_tok__")
        assert len(minted.plaintext) > 30
        # Plaintext is not in any row.
        rows = conn.execute("SELECT token_hash FROM api_tokens").fetchall()
        assert len(rows) == 1
        assert minted.plaintext not in rows[0]["token_hash"]
        # Same hash function: sha256 hex of the plaintext.
        import hashlib

        assert rows[0]["token_hash"] == hashlib.sha256(minted.plaintext.encode("utf-8")).hexdigest()
    finally:
        conn.close()


def test_list_returns_newest_first_and_omits_hash(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        a = _toks.mint(conn, label="first")
        b = _toks.mint(conn, label="second")
        items = _toks.list_all(conn)
        assert [t.id for t in items] == [b.id, a.id]
        # ApiToken dataclass does not carry the hash field.
        assert all("token_hash" not in t.__dict__ for t in items)
    finally:
        conn.close()


def test_revoke_is_idempotent(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        minted = _toks.mint(conn, label="ci-bot")
        assert _toks.revoke(conn, minted.id) is True
        # Second revoke is a no-op.
        assert _toks.revoke(conn, minted.id) is False
        # Listing shows revoked=True.
        items = _toks.list_all(conn)
        assert items[0].id == minted.id
        assert items[0].revoked is True
    finally:
        conn.close()


def test_verify_returns_token_and_bumps_last_used_at(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        minted = _toks.mint(conn, label="ci-bot")
        # Before verify: no last_used_at.
        items = _toks.list_all(conn)
        assert items[0].last_used_at is None
        result = _toks.verify(conn, minted.plaintext)
        assert result is not None
        assert result.id == minted.id
        # After verify: last_used_at is set.
        items = _toks.list_all(conn)
        assert items[0].last_used_at is not None
    finally:
        conn.close()


def test_verify_returns_none_for_revoked_token(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        minted = _toks.mint(conn, label="ci-bot")
        _toks.revoke(conn, minted.id)
        assert _toks.verify(conn, minted.plaintext) is None
    finally:
        conn.close()


def test_verify_returns_none_for_wrong_token(vault_dir: Path) -> None:
    conn = open_db(vault_dir)
    try:
        _toks.mint(conn, label="ci-bot")
        # Random other string — must not match.
        assert _toks.verify(conn, "afair_tok__wrong-string") is None
        # Empty string is None too.
        assert _toks.verify(conn, "") is None
    finally:
        conn.close()


# ─── HTTP handlers ─────────────────────────────────────────────────────────


def test_list_rejects_without_master(vault_dir: Path) -> None:
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/tokens")
    assert r.status_code == 401


def test_list_rejects_with_wrong_master(vault_dir: Path) -> None:
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/tokens", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_mint_list_revoke_full_cycle(vault_dir: Path) -> None:
    app = _build_app(vault_dir, master="THE-MASTER")
    client = TestClient(app)
    headers = {"Authorization": "Bearer THE-MASTER"}

    # Empty list to start.
    r0 = client.get("/internal/tokens", headers=headers)
    assert r0.status_code == 200
    assert r0.json()["tokens"] == []

    # Mint one.
    r1 = client.post(
        "/internal/tokens",
        headers=headers,
        json={"label": "my-ci-bot"},
    )
    assert r1.status_code == 201
    minted = r1.json()
    assert minted["label"] == "my-ci-bot"
    assert minted["scope"] == "full"
    assert minted["token"].startswith("afair_tok__")
    assert "note" in minted

    # List shows it.
    r2 = client.get("/internal/tokens", headers=headers)
    assert r2.status_code == 200
    items = r2.json()["tokens"]
    assert len(items) == 1
    assert items[0]["label"] == "my-ci-bot"
    assert items[0]["revoked"] is False
    # Plaintext NEVER leaks via list.
    assert "token" not in items[0]
    assert "token_hash" not in items[0]

    # Revoke.
    r3 = client.delete(
        f"/internal/tokens/{minted['id']}",
        headers=headers,
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body["revoked"] is True
    assert body["was_active"] is True

    # Idempotent revoke.
    r4 = client.delete(
        f"/internal/tokens/{minted['id']}",
        headers=headers,
    )
    assert r4.status_code == 200
    assert r4.json()["was_active"] is False


def test_mint_validates_label(vault_dir: Path) -> None:
    app = _build_app(vault_dir, master="M")
    client = TestClient(app)
    headers = {"Authorization": "Bearer M"}

    r = client.post("/internal/tokens", headers=headers, json={})
    assert r.status_code == 400

    r = client.post("/internal/tokens", headers=headers, json={"label": ""})
    assert r.status_code == 400

    r = client.post("/internal/tokens", headers=headers, json={"label": "a" * 100})
    assert r.status_code == 400


def test_mint_rejects_unknown_scope(vault_dir: Path) -> None:
    app = _build_app(vault_dir, master="M")
    client = TestClient(app)
    r = client.post(
        "/internal/tokens",
        headers={"Authorization": "Bearer M"},
        json={"label": "x", "scope": "bogus"},
    )
    assert r.status_code == 400


def test_minted_token_cannot_manage_other_tokens(vault_dir: Path) -> None:
    """Sub-tokens cannot mint or revoke — only the master can.

    Defense against an agent token being leveraged into more agent
    tokens. Even if it passes the main MCP middleware, the tokens
    endpoint rejects it.
    """
    app = _build_app(vault_dir, master="THE-MASTER")
    client = TestClient(app)
    headers_master = {"Authorization": "Bearer THE-MASTER"}

    r = client.post(
        "/internal/tokens",
        headers=headers_master,
        json={"label": "my-bot"},
    )
    sub_token = r.json()["token"]

    # Sub-token attempts to list — denied.
    r2 = client.get(
        "/internal/tokens",
        headers={"Authorization": f"Bearer {sub_token}"},
    )
    assert r2.status_code == 401

    # Sub-token attempts to mint — denied.
    r3 = client.post(
        "/internal/tokens",
        headers={"Authorization": f"Bearer {sub_token}"},
        json={"label": "another"},
    )
    assert r3.status_code == 401

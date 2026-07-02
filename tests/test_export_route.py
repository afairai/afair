"""Tests for the /internal/export endpoint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp.cors import preflight_endpoint
from afair.mcp.export_route import export_endpoint
from afair.settings import Settings
from afair.substrate import open_db
from afair.substrate.events import write_event_with_status

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path):
    p = tmp_path / "vault"
    p.mkdir()
    return p


def _build_app(vault_dir: Path, export_token: str = "test-token") -> Starlette:
    settings = Settings(
        vault_dir=vault_dir,
        export_token=SecretStr(export_token),
        # required by Settings.__init__ — minimal viable
        afair_auth_token=SecretStr("auth-not-used-here"),
    )
    app = Starlette(routes=[Route("/internal/export", export_endpoint, methods=["GET"])])
    app.state.settings = settings
    return app


def _seed_events(vault_dir: Path, n: int = 3) -> None:
    """Drop N varied events into the substrate fixture."""
    conn = open_db(vault_dir)
    try:
        for i in range(n):
            write_event_with_status(
                conn,
                kind="remember",
                origin="agent",
                payload={"content_type": "text", "text": f"event-{i}"},
            )
    finally:
        conn.close()


# ─── auth ────────────────────────────────────────────────────────────────


def test_export_rejects_missing_bearer(vault_dir) -> None:
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export")
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("WWW-Authenticate", "")


def test_export_rejects_wrong_bearer(vault_dir) -> None:
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_export_cors_on_allowed_origin(vault_dir) -> None:
    """A GET from the dashboard origin carries CORS headers so the browser
    lets the cross-origin download through, plus exposes Content-Disposition
    so the client JS can read the suggested filename."""
    _seed_events(vault_dir, 2)
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get(
        "/internal/export",
        headers={"Authorization": "Bearer test-token", "Origin": "https://afair.ai"},
    )
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") == "https://afair.ai"
    assert "Content-Disposition" in r.headers.get("Access-Control-Expose-Headers", "")


def test_export_cors_localhost_rejected_in_prod(vault_dir) -> None:
    """In prod (environment=fly) the localhost dev origin is NOT reflected —
    only https://afair.ai. Guards against a future refactor re-admitting
    http://localhost:3000 on the production server."""
    settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr("master"),
        export_token=SecretStr("test-token"),
        environment="fly",
        oauth_issuer="https://memory.example.com",
        vault_key=SecretStr("k" * 64),
    )
    app = Starlette(routes=[Route("/internal/export", export_endpoint, methods=["GET"])])
    app.state.settings = settings
    client = TestClient(app)
    r = client.get(
        "/internal/export",
        headers={"Authorization": "Bearer test-token", "Origin": "http://localhost:3000"},
    )
    assert r.status_code == 200
    assert "Access-Control-Allow-Origin" not in r.headers
    # afair.ai is still allowed in prod.
    r2 = client.get(
        "/internal/export",
        headers={"Authorization": "Bearer test-token", "Origin": "https://afair.ai"},
    )
    assert r2.headers.get("Access-Control-Allow-Origin") == "https://afair.ai"


def test_export_cors_absent_for_unknown_origin(vault_dir) -> None:
    """A non-allow-listed origin gets no CORS headers — the browser blocks
    the read, so the master bearer can't be exfiltrated to arbitrary sites."""
    _seed_events(vault_dir, 1)
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get(
        "/internal/export",
        headers={"Authorization": "Bearer test-token", "Origin": "https://evil.example"},
    )
    assert r.status_code == 200
    assert "Access-Control-Allow-Origin" not in r.headers


def test_export_preflight_options(vault_dir) -> None:
    """OPTIONS preflight from the dashboard origin returns the CORS grant."""
    app = _build_app(vault_dir)
    app.router.routes.append(Route("/internal/export", preflight_endpoint, methods=["OPTIONS"]))
    client = TestClient(app)
    r = client.options("/internal/export", headers={"Origin": "https://afair.ai"})
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") == "https://afair.ai"
    assert "GET" in r.headers.get("Access-Control-Allow-Methods", "")


def test_export_rejects_when_both_tokens_unset(vault_dir) -> None:
    """If neither AFAIR_AUTH_TOKEN nor AFAIR_EXPORT_TOKEN is set, every
    call is 401 — fail-closed semantics.
    """
    settings = Settings(vault_dir=vault_dir)
    app = Starlette(routes=[Route("/internal/export", export_endpoint, methods=["GET"])])
    app.state.settings = settings
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401


def test_export_accepts_main_mcp_auth_token(vault_dir) -> None:
    """The user's regular MCP bearer should unlock /internal/export.

    This is the path users hit with the credential from their
    onboarding email. Without this, the bus-factor export promise
    on /datenschutz can't be fulfilled.
    """
    settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr("main-mcp-token"),
        # export_token deliberately NOT set
    )
    app = Starlette(routes=[Route("/internal/export", export_endpoint, methods=["GET"])])
    app.state.settings = settings
    client = TestClient(app)
    r = client.get(
        "/internal/export",
        headers={"Authorization": "Bearer main-mcp-token"},
    )
    assert r.status_code == 200


def test_export_accepts_either_token_when_both_set(vault_dir) -> None:
    """Both the scoped export token and the main MCP token unlock the
    endpoint when both are configured. Either credential is enough.
    """
    settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr("main-mcp-token"),
        export_token=SecretStr("scoped-export-token"),
    )
    app = Starlette(routes=[Route("/internal/export", export_endpoint, methods=["GET"])])
    app.state.settings = settings
    client = TestClient(app)
    r1 = client.get("/internal/export", headers={"Authorization": "Bearer main-mcp-token"})
    r2 = client.get("/internal/export", headers={"Authorization": "Bearer scoped-export-token"})
    r3 = client.get("/internal/export", headers={"Authorization": "Bearer something-else"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 401


# ─── shape ───────────────────────────────────────────────────────────────


def test_export_empty_vault_returns_bootstrap_kinds_and_manifest(vault_dir) -> None:
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    # An "empty" vault still carries substrate: opening it seeds the seven
    # bootstrap ontology kinds (ADR-0003), and the export includes the kind
    # registry for I4 fidelity. So: 7 kind_registry records + the manifest.
    assert [rec["kind"] for rec in lines] == ["kind_registry"] * 7 + ["manifest"]
    manifest = lines[-1]
    assert manifest["kind"] == "manifest"
    assert manifest["format_version"] == 1


def test_export_streams_events_and_manifest(vault_dir) -> None:
    _seed_events(vault_dir, n=3)
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200

    lines = [json.loads(line) for line in r.text.split("\n") if line]
    event_lines = [line for line in lines if line["kind"] == "event"]
    manifest_lines = [line for line in lines if line["kind"] == "manifest"]

    assert len(event_lines) == 3
    assert len(manifest_lines) == 1
    # Events ordered chronologically.
    timestamps = [e["created_at"] for e in event_lines]
    assert timestamps == sorted(timestamps)
    # Payloads parsed.
    for e in event_lines:
        assert isinstance(e["payload"], dict)
        assert e["payload"]["content_type"] == "text"
    # Manifest is the LAST record.
    assert lines[-1]["kind"] == "manifest"


def test_export_includes_blob_references_without_inline(vault_dir) -> None:
    """Blob references appear in events but content_b64 is omitted by default."""
    from afair.substrate.objects import write_object

    conn = open_db(vault_dir)
    try:
        # Store a blob and reference it via an event payload.
        blob_hash = write_object(vault_dir, b"hello world")
        write_event_with_status(
            conn,
            kind="remember",
            origin="agent",
            payload={
                "content_type": "binary",
                "blob_hash": blob_hash,
                "size_bytes": 11,
                "mime": "text/plain",
            },
        )
    finally:
        conn.close()

    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer test-token"})
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    blob_lines = [line for line in lines if line["kind"] == "blob"]
    assert len(blob_lines) == 1
    assert blob_lines[0]["blob_hash"] == blob_hash
    assert "content_b64" not in blob_lines[0]


def test_export_inlines_blobs_when_requested(vault_dir) -> None:
    """?blobs=inline base64-encodes blob bytes into the stream."""
    from base64 import b64decode

    from afair.substrate.objects import write_object

    conn = open_db(vault_dir)
    try:
        blob_hash = write_object(vault_dir, b"hello world")
        write_event_with_status(
            conn,
            kind="remember",
            origin="agent",
            payload={
                "content_type": "binary",
                "blob_hash": blob_hash,
                "size_bytes": 11,
                "mime": "text/plain",
            },
        )
    finally:
        conn.close()

    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get(
        "/internal/export?blobs=inline",
        headers={"Authorization": "Bearer test-token"},
    )
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    blob_lines = [line for line in lines if line["kind"] == "blob"]
    assert len(blob_lines) == 1
    assert "content_b64" in blob_lines[0]
    assert b64decode(blob_lines[0]["content_b64"]) == b"hello world"


def test_export_streams_entity_graph_rows_without_iteration_bug(vault_dir) -> None:
    """Regression: the entity-graph branch iterates sqlite3.Row by KEYS,
    not by indices. The naïve ``for k in row`` raises IndexError once
    real rows exist. Insert an entity row and assert the stream completes
    with the manifest as the final record.
    """
    import json
    from datetime import UTC, datetime

    # First insert an event so the FK in entities (source_event_id) resolves.
    conn = open_db(vault_dir)
    try:
        event, _written = write_event_with_status(
            conn,
            kind="remember",
            origin="agent",
            payload={"content_type": "text", "text": "entity host"},
        )
        # Insert one minimal entity row matching the substrate schema.
        # Column list mirrors afair/substrate/schema.py — keep in sync if
        # the schema changes.
        conn.execute(
            """INSERT INTO entities
                 (id, canonical_name, kind, created_at, created_by,
                  confidence, source_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "entity:test-id",
                "afair",
                "project",
                datetime.now(UTC).isoformat(),
                "test",
                0.95,
                event.id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    lines = [json.loads(line) for line in r.text.split("\n") if line]
    entity_lines = [line for line in lines if line["kind"] == "entity"]
    assert len(entity_lines) == 1, "entity row should have streamed"
    assert entity_lines[0]["canonical_name"] == "afair"
    assert lines[-1]["kind"] == "manifest", "manifest must remain the final record"


def test_export_has_streaming_response_headers(vault_dir) -> None:
    _seed_events(vault_dir, n=1)
    app = _build_app(vault_dir)
    client = TestClient(app)
    r = client.get("/internal/export", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "afair-export-" in cd
    assert cd.endswith('.jsonl"')
    assert r.headers.get("x-format-version") == "1"
    assert "no-store" in r.headers.get("cache-control", "")

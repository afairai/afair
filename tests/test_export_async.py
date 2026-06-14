"""Tests for the async vault-export job (substrate state + routes + artifact)."""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp import export_async_routes as routes
from afair.mcp import export_job
from afair.settings import Settings
from afair.substrate import export_jobs, open_db
from afair.substrate.events import write_event_with_status

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path):
    p = tmp_path / "vault"
    p.mkdir()
    conn = open_db(p)
    try:
        for i in range(3):
            write_event_with_status(
                conn,
                kind="remember",
                origin="agent",
                payload={"content_type": "text", "text": f"event-{i}"},
            )
    finally:
        conn.close()
    return p


def _settings(vault_dir: Path) -> Settings:
    return Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr("master"),
    )


def _build_app(vault_dir: Path) -> Starlette:
    app = Starlette(
        routes=[
            Route("/internal/export/request", routes.export_request_endpoint, methods=["POST"]),
            Route("/internal/export/status", routes.export_status_endpoint, methods=["GET"]),
            Route("/internal/export/download", routes.export_download_endpoint, methods=["GET"]),
        ]
    )
    app.state.settings = _settings(vault_dir)
    return app


# ── substrate state ────────────────────────────────────────────────────────


def test_create_job_stores_hash_not_plaintext(vault_dir) -> None:
    conn = open_db(vault_dir)
    try:
        job_id, token = export_jobs.create_job(conn)
        job = export_jobs.latest_job(conn)
        assert job is not None
        assert job.id == job_id
        assert job.status == "pending"
        # The plaintext token must never be persisted; only its hash.
        row = conn.execute(
            "SELECT download_token_hash FROM export_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row[0] == export_jobs.hash_token(token)
        assert token not in row[0]
        # Lookup by token hash round-trips.
        found = export_jobs.job_by_token_hash(conn, export_jobs.hash_token(token))
        assert found is not None and found.id == job_id
    finally:
        conn.close()


def test_pending_guard_blocks_double_request(vault_dir) -> None:
    conn = open_db(vault_dir)
    try:
        export_jobs.create_job(conn)
        assert export_jobs.has_active_pending(conn) is not None
    finally:
        conn.close()


# ── artifact generate / read round-trip ──────────────────────────────────────


def test_generate_then_read_roundtrip(vault_dir) -> None:
    filename, size = export_job.generate_artifact(vault_dir, "exp_test", include_blobs=True)
    assert (export_job.exports_dir(vault_dir) / filename).is_file()
    assert size > 0
    gz_bytes = export_job.read_artifact(vault_dir, filename)
    text = gzip.decompress(gz_bytes).decode("utf-8")
    lines = [json.loads(line) for line in text.splitlines() if line]
    kinds = {rec["kind"] for rec in lines}
    assert "event" in kinds
    # The manifest terminator proves the stream finished.
    assert lines[-1]["kind"] == "manifest"


def test_run_job_marks_ready_and_writes_artifact(vault_dir) -> None:
    settings = _settings(vault_dir)
    conn = open_db(vault_dir)
    try:
        job_id, token = export_jobs.create_job(conn)
    finally:
        conn.close()
    # No callback secret set → notify is skipped; runs synchronously here.
    export_job.run_job(settings, job_id, include_blobs=True, download_token=token)
    conn = open_db(vault_dir)
    try:
        job = export_jobs.latest_job(conn)
        assert job is not None and job.status == "ready"
        assert job.artifact_filename and job.size_bytes
    finally:
        conn.close()


# ── routes ───────────────────────────────────────────────────────────────────


def test_request_requires_master(vault_dir) -> None:
    client = TestClient(_build_app(vault_dir))
    r = client.post("/internal/export/request")
    assert r.status_code == 401
    r = client.post("/internal/export/request", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_request_starts_job(vault_dir, monkeypatch) -> None:
    # Stub the worker thread so the test doesn't race the generation.
    started: list[str] = []

    class _FakeThread:
        def __init__(self, *a, **k):
            self._job = k.get("args", (None, None))[1]

        def start(self):
            started.append(self._job)

    monkeypatch.setattr(routes.threading, "Thread", _FakeThread)
    client = TestClient(_build_app(vault_dir))
    r = client.post("/internal/export/request", headers={"Authorization": "Bearer master"})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["job"]["status"] == "pending"
    assert body["download_token"]  # returned once so the dashboard can link
    assert started  # the generation thread was kicked


def test_status_reflects_latest(vault_dir) -> None:
    conn = open_db(vault_dir)
    try:
        export_jobs.create_job(conn)
    finally:
        conn.close()
    client = TestClient(_build_app(vault_dir))
    r = client.get("/internal/export/status", headers={"Authorization": "Bearer master"})
    assert r.status_code == 200
    assert r.json()["job"]["status"] == "pending"


def test_download_streams_artifact_for_valid_token(vault_dir) -> None:
    settings = _settings(vault_dir)
    conn = open_db(vault_dir)
    try:
        job_id, token = export_jobs.create_job(conn)
    finally:
        conn.close()
    export_job.run_job(settings, job_id, include_blobs=True, download_token=token)

    client = TestClient(_build_app(vault_dir))
    r = client.get(f"/internal/export/download?token={token}")
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    # Body is the gzip'd JSONL; decompresses to records ending in the manifest.
    text = gzip.decompress(r.content).decode("utf-8")
    assert text.strip().splitlines()[-1].find("manifest") != -1


def test_download_rejects_unknown_token(vault_dir) -> None:
    client = TestClient(_build_app(vault_dir))
    r = client.get("/internal/export/download?token=nope")
    assert r.status_code == 404


def test_download_rejects_expired(vault_dir) -> None:
    conn = open_db(vault_dir)
    try:
        # Settings clamps retention to >= 1h, so set expires_at into the
        # past directly to simulate an expired link.
        job_id, token = export_jobs.create_job(conn)
        export_jobs.mark_ready(conn, job_id, artifact_filename="x.bin", size_bytes=1)
        conn.execute(
            "UPDATE export_jobs SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()
    client = TestClient(_build_app(vault_dir))
    r = client.get(f"/internal/export/download?token={token}")
    assert r.status_code == 410


# ── purge ────────────────────────────────────────────────────────────────────


def test_purge_expired_removes_artifact(vault_dir) -> None:
    settings = _settings(vault_dir)
    conn = open_db(vault_dir)
    try:
        job_id, token = export_jobs.create_job(conn)
    finally:
        conn.close()
    export_job.run_job(settings, job_id, include_blobs=True, download_token=token)
    conn = open_db(vault_dir)
    try:
        job = export_jobs.latest_job(conn)
        artifact = export_job.exports_dir(vault_dir) / job.artifact_filename
        assert artifact.is_file()
        # Force-expire then purge.
        conn.execute(
            "UPDATE export_jobs SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        purged = export_job.purge_expired(vault_dir, conn)
        assert purged == 1
        assert not artifact.exists()
        assert export_jobs.latest_job(conn).status == "expired"
    finally:
        conn.close()

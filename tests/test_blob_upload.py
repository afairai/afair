"""Streaming blob-upload + blob-ref remember-content tests.

Exercises the /internal/blob/upload route + the new BlobRefContent
content_type. Together they replace the base64-in-JSON path for any
file too large to comfortably fit in the body-size middleware's cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.context import clear_context
from afair.mcp.schemas import BlobRefContent
from afair.mcp.server import build_app
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


SAMPLE_TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )
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
        auth_token=SAMPLE_TOKEN,  # type: ignore[arg-type]
    )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(_settings(tmp_path)))


# ── streaming upload — happy path + size cap ───────────────────────────────


def test_streaming_upload_returns_blob_hash(tmp_path: Path) -> None:
    payload = b"hello afair streaming world" * 1024  # ~26 KB
    with _client(tmp_path) as client:
        r = client.post(
            "/internal/blob/upload",
            content=payload,
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["blob_hash"].startswith("sha256:")
    assert body["size_bytes"] == len(payload)


def test_streaming_upload_dedups_existing_blob(tmp_path: Path) -> None:
    payload = b"identical bytes"
    with _client(tmp_path) as client:
        a = client.post(
            "/internal/blob/upload",
            content=payload,
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
        b = client.post(
            "/internal/blob/upload",
            content=payload,
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["blob_hash"] == b.json()["blob_hash"]


def test_streaming_upload_rejects_oversize_content_length(tmp_path: Path) -> None:
    """If Content-Length advertises > max_bytes, reject upfront."""
    # Default cap is 1 GB — declare 2 GB.
    with _client(tmp_path) as client:
        r = client.post(
            "/internal/blob/upload",
            content=b"x",  # actual body is tiny — but the header lies
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(2 * 1024 * 1024 * 1024),
            },
        )
    assert r.status_code == 413


def test_streaming_upload_rejects_wrong_method(tmp_path: Path) -> None:
    """GET against the POST-only route falls through to the FastMCP
    catch-all mount and returns 404, not 405 — but the important thing
    is that it does NOT accept the upload."""
    with _client(tmp_path) as client:
        r = client.get(
            "/internal/blob/upload",
            headers={"Authorization": f"Bearer {SAMPLE_TOKEN}"},
        )
    assert r.status_code in {404, 405}


def test_streaming_upload_rejects_json_content_type(tmp_path: Path) -> None:
    """JSON bodies belong on the remember tool, not on the streaming
    endpoint — fail loudly so misconfigured clients learn fast."""
    with _client(tmp_path) as client:
        r = client.post(
            "/internal/blob/upload",
            json={"oops": "should be raw bytes"},
            headers={"Authorization": f"Bearer {SAMPLE_TOKEN}"},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_content_type"


def test_streaming_upload_requires_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        r = client.post(
            "/internal/blob/upload",
            content=b"bytes",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 401


# ── blob-ref content_type — wires the uploaded blob to an event ────────────


def test_remember_with_blob_ref_creates_event(tmp_path: Path) -> None:
    """End-to-end: upload streaming, then remember with blob-ref."""
    bytes_ = b"This is a test PDF body" * 100

    with _client(tmp_path) as client:
        upload = client.post(
            "/internal/blob/upload",
            content=bytes_,
            headers={
                "Authorization": f"Bearer {SAMPLE_TOKEN}",
                "Content-Type": "application/octet-stream",
            },
        )
    blob_hash = upload.json()["blob_hash"]

    # Now call the handler directly with the blob-ref content.
    from afair.mcp import handlers

    result = handlers.remember(
        content=BlobRefContent(
            type="blob-ref",
            blob_hash=blob_hash,
            mime="application/pdf",
            filename_hint="report.pdf",
        ),
        context="from a streamed upload",
    )
    assert result.ok is True

    # The event row should reference the same blob_hash.
    from afair.mcp.context import connect_for_thread

    db = connect_for_thread()
    row = db.execute("SELECT payload FROM events WHERE id = ?", (result.event_id,)).fetchone()
    import json as _json

    payload = _json.loads(row["payload"])
    assert payload["content_type"] == "binary"
    assert payload["blob_hash"] == blob_hash
    assert payload["mime"] == "application/pdf"
    assert payload["size_bytes"] == len(bytes_)


def test_remember_with_unknown_blob_ref_rejects(tmp_path: Path) -> None:
    from afair.mcp import handlers
    from afair.mcp.handlers import InvalidateTargetError

    with _client(tmp_path), pytest.raises(InvalidateTargetError, match="not found"):
        handlers.remember(
            content=BlobRefContent(
                type="blob-ref",
                blob_hash="sha256:" + "0" * 64,
                mime="application/pdf",
            ),
            context="dangling reference",
        )


def test_blob_ref_validates_hash_shape() -> None:
    """Pydantic rejects malformed blob_hash before the handler sees it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BlobRefContent(
            type="blob-ref",
            blob_hash="not-a-sha256-prefix",  # length mismatch
            mime="application/pdf",
        )

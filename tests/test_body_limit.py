"""Body-size-limit middleware coverage (P2d).

BodySizeLimitMiddleware rejects a request whose Content-Length exceeds the
12 MB cap BEFORE the body is read into memory, and exempts the streaming
blob-upload path. Untested before P2d.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from afair.mcp.body_limit import DEFAULT_MAX_BODY_BYTES
from afair.mcp.context import clear_context
from afair.mcp.server import build_app
from afair.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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
    )


def test_oversized_content_length_rejected_413(tmp_path: Path) -> None:
    """A Content-Length over the cap is rejected 413 without reading the body."""
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "Content-Length": str(DEFAULT_MAX_BODY_BYTES + 1),
            },
            # Body content isn't actually sent past the header check.
            content=b"{}",
        )
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"


def test_blob_upload_exempt_from_cap(tmp_path: Path) -> None:
    """The streaming-upload path is exempt: an over-cap Content-Length there is
    NOT 413'd by the body middleware (its own per-chunk cap governs)."""
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.post(
            "/internal/blob/upload",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(DEFAULT_MAX_BODY_BYTES + 1),
            },
            content=b"small actual body",
        )
    # Whatever the endpoint decides, it must NOT be the body-middleware's 413.
    assert resp.status_code != 413


def test_within_cap_passes(tmp_path: Path) -> None:
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.get("/")
    assert resp.status_code == 200

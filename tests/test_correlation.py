"""Correlation-id middleware coverage (P2d).

CorrelationIdMiddleware mints or accepts an X-Request-ID, caps an incoming
id at 128 chars, and echoes it on the response. Untested before P2d.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

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


def test_echoes_incoming_request_id(tmp_path: Path) -> None:
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.get("/", headers={"X-Request-ID": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "trace-abc-123"


def test_mints_request_id_when_absent(tmp_path: Path) -> None:
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.get("/")
    minted = resp.headers.get("x-request-id")
    assert minted and len(minted) > 0


def test_overlong_incoming_id_is_bounded(tmp_path: Path) -> None:
    """An incoming id over 128 chars is rejected and a fresh one minted."""
    overlong = "x" * 500
    with TestClient(build_app(_settings(tmp_path))) as client:
        resp = client.get("/", headers={"X-Request-ID": overlong})
    echoed = resp.headers["x-request-id"]
    assert echoed != overlong
    assert len(echoed) <= 128

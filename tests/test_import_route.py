"""Direct-to-vault import route tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.mcp.import_route import import_endpoint
from afair.settings import Settings
from afair.substrate import open_db

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _app(vault_dir: Path) -> Starlette:
    app = Starlette(routes=[Route("/internal/import", import_endpoint, methods=["POST"])])
    app.state.settings = Settings(vault_dir=vault_dir, afair_auth_token=SecretStr("MASTER"))
    return app


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer MASTER", "Origin": "https://afair.ai"}


def test_import_requires_authorization(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/import",
        json={"source": "files", "items": [{"text": "hello"}]},
    )
    assert response.status_code == 401


def test_import_writes_ordinary_append_only_memory_events(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/import",
        headers=_headers(),
        json={
            "source": "obsidian",
            "items": [
                {
                    "text": "Atlas moved into prototyping.",
                    "title": "Atlas",
                    "path": "Projects/Atlas.md",
                    "created_at": "2025-05-01T12:00:00Z",
                }
            ],
        },
    )

    assert response.status_code == 201
    assert response.headers["Access-Control-Allow-Origin"] == "https://afair.ai"
    assert response.json()["inserted"] == 1
    conn = open_db(vault_dir)
    try:
        row = conn.execute("SELECT * FROM events WHERE kind='remember'").fetchone()
        payload = json.loads(row["payload"])
        assert row["origin"] == "user"
        assert payload["text"] == "Atlas moved into prototyping."
        assert payload["import_source"] == "obsidian"
        assert payload["import_path"] == "Projects/Atlas.md"
        assert row["created_at"].startswith("2025-05-01T12:00:00")
    finally:
        conn.close()


def test_import_is_idempotent_for_same_normalized_item(vault_dir: Path) -> None:
    client = TestClient(_app(vault_dir))
    body = {
        "source": "chatgpt",
        "items": [{"text": "A durable conversation", "external_id": "conversation-1"}],
    }
    first = client.post("/internal/import", headers=_headers(), json=body)
    second = client.post("/internal/import", headers=_headers(), json=body)

    assert first.json()["inserted"] == 1
    assert second.json()["deduplicated"] == 1


@pytest.mark.parametrize("source", ["chatgpt", "claude", "obsidian", "notion", "files"])
def test_all_supported_sources_are_accepted(vault_dir: Path, source: str) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/import",
        headers=_headers(),
        json={"source": source, "items": [{"text": f"memory from {source}"}]},
    )
    assert response.status_code == 201


def test_import_rejects_oversized_or_malformed_batches(vault_dir: Path) -> None:
    client = TestClient(_app(vault_dir))
    unsupported = client.post(
        "/internal/import",
        headers=_headers(),
        json={"source": "unknown", "items": [{"text": "x"}]},
    )
    empty = client.post(
        "/internal/import",
        headers=_headers(),
        json={"source": "files", "items": []},
    )
    too_large = client.post(
        "/internal/import",
        headers=_headers(),
        json={"source": "files", "items": [{"text": "x" * 100_001}]},
    )

    assert unsupported.status_code == 400
    assert empty.status_code == 400
    assert too_large.status_code == 413


def test_future_created_at_is_clamped_to_now() -> None:
    """A user-supplied future created_at is clamped to now so it can't pin the
    newest-events discovery window; genuine past dates are preserved."""
    from datetime import UTC, datetime

    from afair.mcp.import_route import _valid_created_at

    clamped = _valid_created_at("2999-01-01T00:00:00Z")
    assert clamped is not None
    assert datetime.fromisoformat(clamped) <= datetime.now(UTC)

    past = _valid_created_at("2020-01-01T00:00:00Z")
    assert past is not None and past.startswith("2020-01-01T00:00:00")

    assert _valid_created_at("not a date") is None

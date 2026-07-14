"""Read-only Memory Mirror route tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.agents.invalidation import write_invalidation
from afair.agents.living_syntheses import LIVING_SYNTHESIS_KIND
from afair.mcp.memory_mirror_route import memory_mirror_endpoint
from afair.settings import Settings
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _app(vault_dir: Path, master: str = "MASTER") -> Starlette:
    app = Starlette(
        routes=[Route("/internal/memory-mirror", memory_mirror_endpoint, methods=["GET"])]
    )
    app.state.settings = Settings(
        vault_dir=vault_dir,
        afair_auth_token=SecretStr(master),
    )
    return app


def _seed(vault_dir: Path) -> tuple[str, str]:
    conn = open_db(vault_dir)
    try:
        first = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": "Atlas started as research."},
        )
        second = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": "Atlas now has a prototype."},
        )
        synthesis = write_event(
            conn,
            origin="agent",
            kind=LIVING_SYNTHESIS_KIND,
            payload={
                "content_type": "text",
                "text": "Atlas moved from research to a working prototype.",
                "title": "Project Atlas",
                "cluster_id": "cluster:atlas",
                "citations": [first.content_hash, second.content_hash],
                "member_hashes": [first.content_hash, second.content_hash],
                "signals": ["semantic_proximity"],
                "key_points": [{"point": "A prototype exists", "citations": [second.content_hash]}],
                "open_questions": ["Who tests it first?"],
                "citation_coverage": 1.0,
                "thin_evidence": True,
                "previous_synthesis_hashes": [],
                "ancestor_cluster_ids": [],
            },
            parent_hashes=[first.content_hash, second.content_hash],
        )
        return synthesis.content_hash, first.content_hash
    finally:
        conn.close()


def test_route_requires_internal_authorization(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).get("/internal/memory-mirror")
    assert response.status_code == 401


def test_route_projects_current_synthesis_and_sources(vault_dir: Path) -> None:
    _seed(vault_dir)
    response = TestClient(_app(vault_dir)).get(
        "/internal/memory-mirror",
        headers={"Authorization": "Bearer MASTER", "Origin": "https://afair.ai"},
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://afair.ai"
    body = response.json()
    assert body["stats"]["current_syntheses"] == 1
    item = body["syntheses"][0]
    assert item["title"] == "Project Atlas"
    assert item["cluster_id"] == "cluster:atlas"
    assert item["evidence_count"] == 2
    assert item["sources"][0]["preview"]
    assert item["sources"][0]["current"] is True


def test_route_exposes_stale_source_without_hiding_history(vault_dir: Path) -> None:
    _, source_hash = _seed(vault_dir)
    conn = open_db(vault_dir)
    try:
        write_invalidation(
            conn,
            target_hash=source_hash,
            reason="superseded",
            origin="user",
        )
    finally:
        conn.close()

    response = TestClient(_app(vault_dir)).get(
        "/internal/memory-mirror",
        headers={"Authorization": "Bearer MASTER"},
    )

    body = response.json()
    assert body["stats"]["stale_sources"] == 1
    sources = body["syntheses"][0]["sources"]
    assert any(
        source["content_hash"] == source_hash and not source["current"] for source in sources
    )

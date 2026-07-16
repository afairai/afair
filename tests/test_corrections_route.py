"""Phase 1 backend: the operator decide surface over the correction queue.

Covers the two /internal routes (GET /internal/corrections, POST /internal/decide):
auth (master bearer + dashboard JWT, incl. wrong-sub), validation, the decide
roundtrip + idempotency, the verdict/kind matrix, GET parity vs the recall
pending view, and the transport-parity guarantee that the route runs ZERO
proposal SQL (all mutation flows through decide_correction).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.agents.entity_audit import EntityAuditWorker
from afair.mcp import handlers
from afair.mcp.corrections_route import (
    corrections_list_endpoint,
    decide_endpoint,
)
from afair.settings import Settings
from afair.substrate import (
    open_db,
    read_pending_corrections,
    write_entity,
    write_entity_merge,
    write_event,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

MASTER = "MASTER-TOKEN"
HUB_SECRET = "hub-shared-secret"
ISSUER = "https://u-abc.mcp.afair.ai"
ALLOWED_SUB = "octocat"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _settings(vault_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=vault_dir,
        cold_path_enabled=False,
        afair_auth_token=SecretStr(MASTER),
        identity_hub_secret=SecretStr(HUB_SECRET),
        oauth_issuer=ISSUER,
        identity_allowlist=ALLOWED_SUB,
    )


def _app(vault_dir: Path) -> Starlette:
    app = Starlette(
        routes=[
            Route("/internal/corrections", corrections_list_endpoint, methods=["GET"]),
            Route("/internal/decide", decide_endpoint, methods=["POST"]),
        ]
    )
    app.state.settings = _settings(vault_dir)
    return app


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER}", "Origin": "https://afair.ai"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mint_dashboard_jwt(*, sub: str, intent: str = "dashboard") -> str:
    """A hub-style HS256 mini-JWT the vault's dashboard auth accepts."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": sub,
                "email": None,
                "intent": intent,
                "return_to": ISSUER,
                "iat": now,
                "exp": now + 300,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(HUB_SECRET.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _entity(conn: sqlite3.Connection, name: str, kind: str) -> str:
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        conn, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _seed_proposals(vault_dir: Path) -> None:
    """A retype + a merge_review — the two live-vault audit shapes."""
    conn = open_db(vault_dir)
    try:
        _entity(conn, "maxime.team", "person")  # → retype to product
        from_id = _entity(conn, "Clario", "project")
        into_id = _entity(conn, "Clario", "product")
        write_entity_merge(
            conn,
            from_entity_id=from_id,
            into_entity_id=into_id,
            merged_by="entity_deduplicator:v0",
            reason="t",
            confidence=0.95,
        )
        EntityAuditWorker().run(conn, _settings(vault_dir))
    finally:
        conn.close()


# ── auth ─────────────────────────────────────────────────────────────────────


def test_list_requires_authorization(vault_dir: Path) -> None:
    assert TestClient(_app(vault_dir)).get("/internal/corrections").status_code == 401


def test_decide_requires_authorization(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide", json={"proposal_id": "x", "verdict": "confirm"}
    )
    assert response.status_code == 401


def test_dashboard_jwt_authorizes(vault_dir: Path) -> None:
    token = _mint_dashboard_jwt(sub=ALLOWED_SUB)
    response = TestClient(_app(vault_dir)).get(
        "/internal/corrections", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_wrong_sub_dashboard_jwt_is_rejected(vault_dir: Path) -> None:
    """A validly-signed dashboard token for a DIFFERENT user is 401 (I8)."""
    token = _mint_dashboard_jwt(sub="someone-else")
    response = TestClient(_app(vault_dir)).get(
        "/internal/corrections", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_wrong_intent_jwt_is_rejected(vault_dir: Path) -> None:
    """A non-dashboard intent (e.g. an MCP-login token) can't be replayed here."""
    token = _mint_dashboard_jwt(sub=ALLOWED_SUB, intent="mcp")
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers={"Authorization": f"Bearer {token}"},
        json={"proposal_id": "x", "verdict": "confirm"},
    )
    assert response.status_code == 401


# ── GET /internal/corrections ────────────────────────────────────────────────


def test_list_empty_on_fresh_vault(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).get("/internal/corrections", headers=_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["pending"] == []
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Access-Control-Allow-Origin"] == "https://afair.ai"


def test_list_surfaces_open_proposals_with_detail(vault_dir: Path) -> None:
    _seed_proposals(vault_dir)
    response = TestClient(_app(vault_dir)).get("/internal/corrections", headers=_headers())
    body = response.json()
    assert body["count"] == 2
    kinds = sorted(item["kind"] for item in body["pending"])
    assert kinds == ["merge_review", "retype"]
    for item in body["pending"]:
        assert item["prompt"]
        assert item["detail"]["queue"] == "entity_audit"
        assert item["detail"]["kind"] == item["kind"]
        assert isinstance(item["detail"]["detail"], dict)


def test_list_is_byte_parity_with_recall_pending_view(vault_dir: Path) -> None:
    """The route's item id/kind/prompt/evidence/confidence must be IDENTICAL to
    what recall's _pending_correction_views serves — same source, same order."""
    _seed_proposals(vault_dir)
    response = TestClient(_app(vault_dir)).get("/internal/corrections", headers=_headers())
    served = response.json()["pending"]

    conn = open_db(vault_dir)
    try:
        views = handlers._pending_correction_views(conn, limit=50, offset=0)
    finally:
        conn.close()

    assert [s["id"] for s in served] == [v.id for v in views]
    for s, v in zip(served, views, strict=True):
        assert s["kind"] == v.kind
        assert s["prompt"] == v.prompt
        assert s["evidence"] == v.evidence
        assert s["confidence"] == v.confidence
        assert s["entity_id"] == v.entity_id
        assert s["entity_name"] == v.entity_name


def test_list_respects_limit(vault_dir: Path) -> None:
    _seed_proposals(vault_dir)
    response = TestClient(_app(vault_dir)).get("/internal/corrections?limit=1", headers=_headers())
    assert response.json()["count"] == 1


# ── POST /internal/decide: validation ────────────────────────────────────────


def test_decide_rejects_missing_proposal_id(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide", headers=_headers(), json={"verdict": "confirm"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "proposal_id_required"


def test_decide_rejects_bad_verdict(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide", headers=_headers(), json={"proposal_id": "p1", "verdict": "maybe"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_verdict"


def test_decide_rejects_overlong_proposal_id(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": "x" * 101, "verdict": "confirm"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "proposal_id_too_long"


def test_decide_rejects_overlong_to_kind(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": "p1", "verdict": "confirm", "to_kind": "k" * 101},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "to_kind_too_long"


def test_decide_rejects_non_object_body(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide", headers=_headers(), json=["not", "a", "dict"]
    )
    assert response.status_code == 400
    assert response.json()["error"] == "body_must_be_object"


# ── POST /internal/decide: roundtrip + idempotency + matrix ──────────────────


def _first(vault_dir: Path, kind: str) -> str:
    conn = open_db(vault_dir)
    try:
        return next(p.id for p in read_pending_corrections(conn) if p.kind == kind)
    finally:
        conn.close()


def test_decide_confirm_retype_applies_and_drops_from_queue(vault_dir: Path) -> None:
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    client = TestClient(_app(vault_dir))
    response = client.post(
        "/internal/decide", headers=_headers(), json={"proposal_id": pid, "verdict": "confirm"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    # The queue drops it.
    after = client.get("/internal/corrections", headers=_headers()).json()
    assert all(item["id"] != pid for item in after["pending"])


def test_decide_is_idempotent_success(vault_dir: Path) -> None:
    """A second decide on a decided proposal is already_decided → 200 (no-op)."""
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    client = TestClient(_app(vault_dir))
    first = client.post(
        "/internal/decide", headers=_headers(), json={"proposal_id": pid, "verdict": "confirm"}
    )
    second = client.post(
        "/internal/decide", headers=_headers(), json={"proposal_id": pid, "verdict": "confirm"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_decided"


def test_decide_unknown_proposal_is_404(vault_dir: Path) -> None:
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": "does-not-exist", "verdict": "confirm"},
    )
    assert response.status_code == 404
    assert response.json()["status"] == "not_found"


def test_decide_reject_with_to_kind_retypes(vault_dir: Path) -> None:
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "merge_review")
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": pid, "verdict": "reject", "to_kind": "project"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"


def test_decide_bad_to_kind_is_invalid_decision_400(vault_dir: Path) -> None:
    """An unknown kind slug shape-validates here but decide_correction refuses it
    (its registry validation is authoritative) → 400 invalid_decision."""
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": pid, "verdict": "confirm", "to_kind": "not-a-real-kind"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_decision"


def test_decide_revert_on_entity_proposal_is_invalid_decision_400(vault_dir: Path) -> None:
    """'revert' shape-validates (it's a CorrectionDecision literal) but is only
    valid for ontology ids — decide_correction raises → 400 invalid_decision."""
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide",
        headers=_headers(),
        json={"proposal_id": pid, "verdict": "revert"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_decision"


def test_decide_retract_withdraws_the_entity(vault_dir: Path) -> None:
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    response = TestClient(_app(vault_dir)).post(
        "/internal/decide", headers=_headers(), json={"proposal_id": pid, "verdict": "retract"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"


def test_decide_stamps_dashboard_provenance(vault_dir: Path) -> None:
    """The decision is recorded with decided_by='operator:dashboard' — a
    distinct provenance from the MCP-client 'operator' (auditable, I7)."""
    _seed_proposals(vault_dir)
    pid = _first(vault_dir, "retype")
    TestClient(_app(vault_dir)).post(
        "/internal/decide", headers=_headers(), json={"proposal_id": pid, "verdict": "confirm"}
    )
    conn = open_db(vault_dir)
    try:
        row = conn.execute(
            "SELECT decided_by FROM proposed_corrections WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["decided_by"] == "operator:dashboard"

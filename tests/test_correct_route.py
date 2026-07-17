"""Operator-initiated content correction route tests (ADR-0009).

Covers Flavor A (source-wrong), Flavor B-b1 (whole-synthesis-wrong via the same
event path), and Flavor B-b2 (key-point suppress/restore). Every assertion holds
the append-only line: a corrected target row is byte-identical before and after,
the only physical deletion is the events_fts index row for a superseded
synthesis, and a key-point review is a NEW interpretation row (never a mutation).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from afair.agents.invalidation import INVALIDATE_KIND, write_invalidation
from afair.agents.living_syntheses import LIVING_SYNTHESIS_KIND
from afair.mcp.correct_route import correct_endpoint
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.content_corrections import point_digest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


def _app(vault_dir: Path) -> Starlette:
    app = Starlette(routes=[Route("/internal/correct", correct_endpoint, methods=["POST"])])
    app.state.settings = Settings(vault_dir=vault_dir, afair_auth_token=SecretStr("MASTER"))
    return app


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer MASTER", "Origin": "https://afair.ai"}


def _seed_source(vault_dir: Path, text: str = "Atlas launched in March.") -> str:
    conn = open_db(vault_dir)
    try:
        event = write_event(
            conn,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": text},
        )
        return event.content_hash
    finally:
        conn.close()


def _seed_synthesis(vault_dir: Path) -> tuple[str, str, str]:
    """Return (synthesis_hash, source_hash, key_point_text)."""
    conn = open_db(vault_dir)
    try:
        source = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": "Atlas has a prototype."},
        )
        key_point_text = "A prototype exists"
        synthesis = write_event(
            conn,
            origin="agent",
            kind=LIVING_SYNTHESIS_KIND,
            payload={
                "content_type": "text",
                "text": "Atlas moved to a prototype.",
                "title": "Project Atlas",
                "cluster_id": "cluster:atlas",
                "citations": [source.content_hash],
                "member_hashes": [source.content_hash],
                "key_points": [
                    {"point": key_point_text, "mode": "fact", "citations": [source.content_hash]}
                ],
            },
            parent_hashes=[source.content_hash],
        )
        return synthesis.content_hash, source.content_hash, key_point_text
    finally:
        conn.close()


def _row_snapshot(vault_dir: Path, content_hash: str) -> dict:
    conn = open_db(vault_dir)
    try:
        row = conn.execute(
            "SELECT id, content_hash, created_at, origin, kind, payload, parent_hashes "
            "FROM events WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


# ── auth ──────────────────────────────────────────────────────────────────
def test_correct_requires_authorization(vault_dir: Path) -> None:
    target = _seed_source(vault_dir)
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct", json={"kind": "event", "target_hash": target}
    )
    assert response.status_code == 401


# ── Flavor A: source-wrong roundtrip ────────────────────────────────────────
def test_correct_source_writes_three_events_target_byte_identical(vault_dir: Path) -> None:
    target = _seed_source(vault_dir)
    before = _row_snapshot(vault_dir, target)

    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "event",
            "target_hash": target,
            "correction_text": "Atlas actually launched in April.",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True
    assert body["already_invalidated"] is False
    assert body["invalidation_event_id"]
    assert body["correction_event_id"]
    assert body["correction_content_hash"]
    assert body["deduplicated"] is False
    assert body["resynthesis"]["expected_within_seconds"] == 21_600
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Access-Control-Allow-Origin"] == "https://afair.ai"

    # Target row is byte-identical after the correction (I2 — never mutated).
    assert _row_snapshot(vault_dir, target) == before

    conn = open_db(vault_dir)
    try:
        # (1) invalidation event references the target.
        inv = conn.execute(
            "SELECT payload, parent_hashes FROM events WHERE kind = ? "
            "AND json_extract(payload, '$.target_hash') = ?",
            (INVALIDATE_KIND, target),
        ).fetchone()
        assert inv is not None
        assert json.loads(inv["parent_hashes"]) == [target]
        # (2) correction remember event is parent-linked + carries provenance.
        corr = conn.execute(
            "SELECT payload, parent_hashes FROM events "
            "WHERE json_extract(payload, '$.corrects') = ?",
            (target,),
        ).fetchone()
        assert corr is not None
        corr_payload = json.loads(corr["payload"])
        assert corr_payload["type_hint"] == "operator_correction"
        assert corr_payload["corrected_by"] == "operator:dashboard"
        assert json.loads(corr["parent_hashes"]) == [target]
        # (3) observe event records the operator action (I7).
        obs = conn.execute(
            "SELECT payload FROM events WHERE kind = 'observe' "
            "AND json_extract(payload, '$.subject') = ?",
            (target,),
        ).fetchone()
        assert obs is not None
        assert json.loads(obs["payload"])["action"] == "correct_content"
    finally:
        conn.close()


def test_correct_source_without_text_only_invalidates(vault_dir: Path) -> None:
    target = _seed_source(vault_dir)
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={"kind": "event", "target_hash": target, "reason": "outdated"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["correction_event_id"] is None
    assert body["invalidation_event_id"]
    conn = open_db(vault_dir)
    try:
        # No correction remember event was written.
        corr = conn.execute(
            "SELECT 1 FROM events WHERE json_extract(payload, '$.corrects') = ?",
            (target,),
        ).fetchone()
        assert corr is None
        # The invalidation reason falls back to the supplied reason.
        inv = conn.execute(
            "SELECT payload FROM events WHERE kind = ? "
            "AND json_extract(payload, '$.target_hash') = ?",
            (INVALIDATE_KIND, target),
        ).fetchone()
        assert json.loads(inv["payload"])["reason"] == "outdated"
    finally:
        conn.close()


def test_correct_accepts_bare_and_prefixed_target(vault_dir: Path) -> None:
    """The route accepts both the bare 64-hex and the sha256:-prefixed form."""
    client = TestClient(_app(vault_dir))
    prefixed_target = _seed_source(vault_dir, "First fact.")  # "sha256:<64hex>"
    bare_target = _seed_source(vault_dir, "Second fact.")
    assert prefixed_target.startswith("sha256:")

    prefixed = client.post(
        "/internal/correct",
        headers=_headers(),
        json={"kind": "event", "target_hash": prefixed_target},
    )
    bare = client.post(
        "/internal/correct",
        headers=_headers(),
        json={"kind": "event", "target_hash": bare_target[len("sha256:") :]},
    )
    assert prefixed.status_code == 201
    assert bare.status_code == 201


# ── Flavor A: idempotency / dedup ───────────────────────────────────────────
def test_correct_is_idempotent_retry_is_noop(vault_dir: Path) -> None:
    target = _seed_source(vault_dir)
    client = TestClient(_app(vault_dir))
    body = {
        "kind": "event",
        "target_hash": target,
        "correction_text": "Atlas launched in April.",
    }
    first = client.post("/internal/correct", headers=_headers(), json=body)
    second = client.post("/internal/correct", headers=_headers(), json=body)

    assert first.status_code == 201
    assert first.json()["already_invalidated"] is False
    assert first.json()["deduplicated"] is False

    assert second.status_code == 200
    assert second.json()["already_invalidated"] is True
    assert second.json()["deduplicated"] is True

    conn = open_db(vault_dir)
    try:
        # Exactly one invalidation and one correction event despite two calls.
        inv_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? "
            "AND json_extract(payload, '$.target_hash') = ?",
            (INVALIDATE_KIND, target),
        ).fetchone()[0]
        assert inv_count == 1
        corr_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE json_extract(payload, '$.corrects') = ?",
            (target,),
        ).fetchone()[0]
        assert corr_count == 1
    finally:
        conn.close()


# ── Flavor A: typed errors ──────────────────────────────────────────────────
def test_correct_typed_errors(vault_dir: Path) -> None:
    client = TestClient(_app(vault_dir))
    h = _headers()

    assert (
        client.post("/internal/correct", headers=h, content=b"{").status_code == 400
    )  # invalid_json
    assert (
        client.post("/internal/correct", headers=h, json=[1, 2]).json()["error"]
        == "body_must_be_object"
    )
    assert (
        client.post("/internal/correct", headers=h, json={"kind": "wat"}).json()["error"]
        == "unknown_kind"
    )
    assert (
        client.post("/internal/correct", headers=h, json={"kind": "event"}).json()["error"]
        == "target_hash_required"
    )
    assert (
        client.post(
            "/internal/correct", headers=h, json={"kind": "event", "target_hash": "nothex"}
        ).json()["error"]
        == "target_hash_malformed"
    )
    # target_not_found: well-formed 64-hex that names no event.
    missing = client.post(
        "/internal/correct", headers=h, json={"kind": "event", "target_hash": "a" * 64}
    )
    assert missing.status_code == 404
    assert missing.json()["error"] == "target_not_found"

    # correction_text_too_large → 413.
    target = _seed_source(vault_dir)
    big = client.post(
        "/internal/correct",
        headers=h,
        json={"kind": "event", "target_hash": target, "correction_text": "x" * 20_001},
    )
    assert big.status_code == 413
    assert big.json()["error"] == "correction_text_too_large"

    # reason_too_long → 400.
    long_reason = client.post(
        "/internal/correct",
        headers=h,
        json={"kind": "event", "target_hash": target, "reason": "x" * 501},
    )
    assert long_reason.status_code == 400
    assert long_reason.json()["error"] == "reason_too_long"


def test_correct_target_is_invalidation_rejected(vault_dir: Path) -> None:
    target = _seed_source(vault_dir)
    conn = open_db(vault_dir)
    try:
        inv = write_invalidation(conn, target_hash=target, reason="x", origin="user")
        inv_hash = inv.content_hash
    finally:
        conn.close()
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={"kind": "event", "target_hash": inv_hash},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "target_is_invalidation"


# ── Flavor B-b1: whole-synthesis-wrong deletes the FTS row ──────────────────
def test_reject_synthesis_supersedes_and_deletes_fts_row(vault_dir: Path) -> None:
    synthesis_hash, _source, _kp = _seed_synthesis(vault_dir)
    before = _row_snapshot(vault_dir, synthesis_hash)

    conn = open_db(vault_dir)
    try:
        fts_before = conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE content_hash = ?", (synthesis_hash,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert fts_before == 1

    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={"kind": "event", "target_hash": synthesis_hash},
    )
    assert response.status_code == 201

    # Synthesis substrate row byte-identical; only its FTS index row is gone.
    assert _row_snapshot(vault_dir, synthesis_hash) == before
    conn = open_db(vault_dir)
    try:
        fts_after = conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE content_hash = ?", (synthesis_hash,)
        ).fetchone()[0]
        assert fts_after == 0
        inv = conn.execute(
            "SELECT 1 FROM events WHERE kind = ? AND json_extract(payload, '$.target_hash') = ?",
            (INVALIDATE_KIND, synthesis_hash),
        ).fetchone()
        assert inv is not None
    finally:
        conn.close()


# ── Flavor B-b2: key-point suppress / restore ───────────────────────────────
def test_suppress_key_point_writes_one_interpretation(vault_dir: Path) -> None:
    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    synthesis_before = _row_snapshot(vault_dir, synthesis_hash)

    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": kp},
            "verdict": "suppress",
            "note": "This never shipped.",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "suppressed"
    assert body["point_digest"] == point_digest(kp)
    assert body["version"] == 1

    # Synthesis payload NEVER rewritten (I2).
    assert _row_snapshot(vault_dir, synthesis_hash) == synthesis_before

    conn = open_db(vault_dir)
    try:
        rows = conn.execute(
            "SELECT produced_by, extraction FROM interpretations "
            "WHERE event_hash = ? AND produced_by LIKE 'key_point_review:v1:%'",
            (synthesis_hash,),
        ).fetchall()
        assert len(rows) == 1
        extraction = json.loads(rows[0]["extraction"])
        assert extraction["verdict"] == "suppress"
        assert extraction["note"] == "This never shipped."
        assert extraction["cluster_id"] == "cluster:atlas"
        # observe event recorded (I7).
        obs = conn.execute(
            "SELECT payload FROM events WHERE kind = 'observe' "
            "AND json_extract(payload, '$.action') = 'suppress_key_point'"
        ).fetchone()
        assert obs is not None
    finally:
        conn.close()


def test_restore_key_point_is_latest_wins_new_row(vault_dir: Path) -> None:
    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    client = TestClient(_app(vault_dir))

    suppress = client.post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": kp},
            "verdict": "suppress",
        },
    )
    restore = client.post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": kp},
            "verdict": "restore",
        },
    )
    assert suppress.json()["version"] == 1
    assert restore.status_code == 201
    assert restore.json()["status"] == "restored"
    assert restore.json()["version"] == 2

    conn = open_db(vault_dir)
    try:
        # Two rows (suppress v1, restore v2) — restore is a NEW row, not a mutation.
        rows = conn.execute(
            "SELECT version, extraction FROM interpretations "
            "WHERE event_hash = ? AND produced_by LIKE 'key_point_review:v1:%' "
            "ORDER BY version",
            (synthesis_hash,),
        ).fetchall()
        assert [r["version"] for r in rows] == [1, 2]
        assert json.loads(rows[0]["extraction"])["verdict"] == "suppress"
        assert json.loads(rows[1]["extraction"])["verdict"] == "restore"
    finally:
        conn.close()


def test_suppress_key_point_idempotent(vault_dir: Path) -> None:
    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    client = TestClient(_app(vault_dir))
    body = {
        "kind": "key_point",
        "synthesis_hash": synthesis_hash,
        "point": {"text": kp},
        "verdict": "suppress",
    }
    first = client.post("/internal/correct", headers=_headers(), json=body)
    second = client.post("/internal/correct", headers=_headers(), json=body)

    assert first.status_code == 201
    assert first.json()["status"] == "suppressed"
    assert second.status_code == 200
    assert second.json()["status"] == "already_suppressed"
    assert second.json()["version"] == 1  # no new row

    conn = open_db(vault_dir)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM interpretations "
            "WHERE event_hash = ? AND produced_by LIKE 'key_point_review:v1:%'",
            (synthesis_hash,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_suppress_key_point_matches_normalized_whitespace(vault_dir: Path) -> None:
    """The operator can echo the point with incidental whitespace/case diffs."""
    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": f"  {kp.upper()}  "},
            "verdict": "suppress",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "suppressed"


def test_suppress_key_point_unknown_text_is_404(vault_dir: Path) -> None:
    synthesis_hash, _source, _kp = _seed_synthesis(vault_dir)
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": "A claim that was never made"},
            "verdict": "suppress",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"] == "key_point_not_found"


def test_key_point_on_non_synthesis_is_400(vault_dir: Path) -> None:
    source = _seed_source(vault_dir)
    response = TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": source,
            "point": {"text": "whatever"},
            "verdict": "suppress",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "not_a_synthesis"


def test_key_point_review_row_is_append_only_protected(vault_dir: Path) -> None:
    """The key_point_review:v1: interpretation is protected by the append-only
    triggers (no_update unconditional; no_delete for NOT LIKE 'extractor:%')."""
    import sqlite3

    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    TestClient(_app(vault_dir)).post(
        "/internal/correct",
        headers=_headers(),
        json={
            "kind": "key_point",
            "synthesis_hash": synthesis_hash,
            "point": {"text": kp},
            "verdict": "suppress",
        },
    )
    conn = open_db(vault_dir)
    try:
        row = conn.execute(
            "SELECT id FROM interpretations WHERE produced_by LIKE 'key_point_review:v1:%'"
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE interpretations SET extraction = '{}' WHERE id = ?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM interpretations WHERE id = ?", (row["id"],))
    finally:
        conn.close()


def test_key_point_typed_validation_errors(vault_dir: Path) -> None:
    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    client = TestClient(_app(vault_dir))
    h = _headers()

    assert (
        client.post(
            "/internal/correct",
            headers=h,
            json={"kind": "key_point", "point": {"text": kp}, "verdict": "suppress"},
        ).json()["error"]
        == "synthesis_hash_required"
    )
    assert (
        client.post(
            "/internal/correct",
            headers=h,
            json={
                "kind": "key_point",
                "synthesis_hash": synthesis_hash,
                "point": {"text": kp},
                "verdict": "delete",
            },
        ).json()["error"]
        == "invalid_verdict"
    )
    assert (
        client.post(
            "/internal/correct",
            headers=h,
            json={"kind": "key_point", "synthesis_hash": synthesis_hash, "verdict": "suppress"},
        ).json()["error"]
        == "point_required"
    )


# ── Flavor B-b2: carry-forward across re-synthesis (cluster fallback) ────────
def _seed_second_synthesis(vault_dir: Path, *, cluster_id: str, point_text: str) -> str:
    """A NEW synthesis event (new content_hash) of the given cluster carrying the
    given key point verbatim — simulating the cold path re-deriving the cluster."""
    conn = open_db(vault_dir)
    try:
        source = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": "A later source about the same cluster."},
        )
        synthesis = write_event(
            conn,
            origin="agent",
            kind=LIVING_SYNTHESIS_KIND,
            payload={
                "content_type": "text",
                "text": "Re-derived synthesis of the same cluster.",
                "title": "Project Atlas (re-derived)",
                "cluster_id": cluster_id,
                "citations": [source.content_hash],
                "member_hashes": [source.content_hash],
                "key_points": [
                    {"point": point_text, "mode": "fact", "citations": [source.content_hash]}
                ],
                "previous_synthesis_hashes": [],
            },
            parent_hashes=[source.content_hash],
        )
        return synthesis.content_hash
    finally:
        conn.close()


def test_suppression_carries_forward_to_re_derived_synthesis(vault_dir: Path) -> None:
    """A suppression on S1(cluster C) still marks the verbatim point on a later
    re-derived S2(cluster C) with a NEW content_hash (the cluster-fallback lane).
    A reworded point on S2 is NOT suppressed (documented digest limitation)."""
    from afair.mcp.memory_mirror_route import _read_syntheses
    from afair.substrate.content_corrections import review_key_point

    s1_hash, _source, kp = _seed_synthesis(vault_dir)  # cluster:atlas
    # Suppress the key point on S1.
    conn = open_db(vault_dir)
    try:
        review_key_point(
            conn,
            synthesis_hash=s1_hash,
            point_text=kp,
            verdict="suppress",
            cluster_id="cluster:atlas",
            note="Wrong claim.",
        )
    finally:
        conn.close()

    # A later re-derived synthesis of the SAME cluster carrying the verbatim
    # point (new content_hash) — this is what the cold path produces.
    s2_verbatim = _seed_second_synthesis(vault_dir, cluster_id="cluster:atlas", point_text=kp)
    # And a DIFFERENT cluster's synthesis with the verbatim point — must NOT match.
    s_other = _seed_second_synthesis(vault_dir, cluster_id="cluster:other", point_text=kp)
    # And a reworded point on the same cluster — must NOT match (digest differs).
    s_reworded = _seed_second_synthesis(
        vault_dir, cluster_id="cluster:atlas", point_text="A working prototype now exists"
    )

    conn = open_db(vault_dir)
    try:
        served = {item["content_hash"]: item for item in _read_syntheses(conn, limit=50)}
    finally:
        conn.close()

    def _point(h: str) -> dict:
        return served[h]["key_points"][0]

    # S1 (exact) suppressed; S2 verbatim same-cluster carried forward.
    assert _point(s1_hash)["suppressed"] is True
    assert _point(s2_verbatim)["suppressed"] is True
    assert _point(s2_verbatim)["suppression"]["note"] == "Wrong claim."
    # Different cluster: not suppressed.
    assert _point(s_other)["suppressed"] is False
    # Reworded point on the same cluster: digest differs → not suppressed.
    assert _point(s_reworded)["suppressed"] is False


def test_exact_review_overrides_cluster_fallback(vault_dir: Path) -> None:
    """A per-synthesis decision on the re-derived S2 overrides the carried-forward
    cluster suppression (exact lane wins)."""
    from afair.mcp.memory_mirror_route import _read_syntheses
    from afair.substrate.content_corrections import review_key_point

    s1_hash, _source, kp = _seed_synthesis(vault_dir)
    s2_hash = _seed_second_synthesis(vault_dir, cluster_id="cluster:atlas", point_text=kp)

    conn = open_db(vault_dir)
    try:
        # Suppress on S1 → carries forward to S2 via cluster fallback.
        review_key_point(
            conn,
            synthesis_hash=s1_hash,
            point_text=kp,
            verdict="suppress",
            cluster_id="cluster:atlas",
            note=None,
        )
        # But an explicit restore ON S2 must override the cluster fallback.
        review_key_point(
            conn,
            synthesis_hash=s2_hash,
            point_text=kp,
            verdict="restore",
            cluster_id="cluster:atlas",
            note=None,
        )
    finally:
        conn.close()

    conn = open_db(vault_dir)
    try:
        served = {item["content_hash"]: item for item in _read_syntheses(conn, limit=50)}
    finally:
        conn.close()
    # S1 still suppressed; S2 restored (exact decision overrides cluster carry).
    assert served[s1_hash]["key_points"][0]["suppressed"] is True
    assert served[s2_hash]["key_points"][0]["suppressed"] is False


# ── Flavor B-b2: concurrent suppress-vs-restore serialization ────────────────
def test_concurrent_suppress_restore_no_lost_decision_no_500(vault_dir: Path) -> None:
    """Two concurrent decides on the same key point (separate connections):
    exactly one verdict is the served latest, the returned status + the observe
    event match what actually persisted, and neither path 500s."""
    import threading

    from afair.substrate.content_corrections import review_key_point

    synthesis_hash, _source, kp = _seed_synthesis(vault_dir)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def _decide(name: str, verdict: str) -> None:
        conn = open_db(vault_dir)
        try:
            barrier.wait(timeout=5)
            results[name] = review_key_point(
                conn,
                synthesis_hash=synthesis_hash,
                point_text=kp,
                verdict=verdict,
                cluster_id="cluster:atlas",
                note=None,
            )
        except BaseException as exc:  # record any 500-shaped failure
            errors.append(exc)
        finally:
            conn.close()

    t1 = threading.Thread(target=_decide, args=("A", "suppress"))
    t2 = threading.Thread(target=_decide, args=("B", "restore"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"a concurrent decide raised (would be a 500): {errors!r}"
    assert len(results) == 2

    # The served latest is exactly ONE verdict; the observe events never claim a
    # verdict that isn't the persisted truth.
    conn = open_db(vault_dir)
    try:
        rows = conn.execute(
            "SELECT version, json_extract(extraction, '$.verdict') AS verdict "
            "FROM interpretations WHERE event_hash = ? "
            "AND produced_by LIKE 'key_point_review:v1:%' ORDER BY version DESC",
            (synthesis_hash,),
        ).fetchall()
        assert rows, "at least one review row must have persisted"
        served_verdict = rows[0]["verdict"]
        assert served_verdict in ("suppress", "restore")

        # Every observe event's result must correspond to a review row that
        # actually landed — never an audit for a decision that didn't persist.
        observes = conn.execute(
            "SELECT json_extract(payload, '$.result') AS result FROM events "
            "WHERE kind = 'observe' "
            "AND json_extract(payload, '$.action') = 'suppress_key_point'"
        ).fetchall()
        persisted_verdicts = {r["verdict"] for r in rows}
        for obs in observes:
            assert obs["result"] in persisted_verdicts
    finally:
        conn.close()


# ── Flavor A: honest re-clustering mechanism (fix-3) ─────────────────────────
def test_correction_event_is_eligible_but_invalidated_target_is_not(vault_dir: Path) -> None:
    """The honest re-clustering claim: after correct_event, the NEW correction
    remember event is eligible for clustering (so it re-clusters via
    entity/semantic signals after extraction), while the invalidated target is
    excluded from _eligible_events — so synthesis LINEAGE cannot carry the
    correction through the target (the comment's claim, made verifiable)."""
    from afair.agents.living_syntheses import _eligible_events
    from afair.substrate.content_corrections import correct_event

    target = _seed_source(vault_dir, "Atlas launched in March.")
    conn = open_db(vault_dir)
    try:
        result = correct_event(
            conn,
            target_hash=target,
            correction_text="Atlas actually launched in April.",
            reason=None,
        )
        eligible = _eligible_events(conn)
    finally:
        conn.close()

    # The invalidated target is NOT eligible (lineage can't bridge on it).
    assert target not in eligible
    # The correction event IS eligible — it participates in clustering like any
    # other remember event (entity/semantic path after extraction).
    assert result.correction_content_hash in eligible

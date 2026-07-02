"""Export → import round-trip fidelity (I4).

Two audits (privacy + the ADR-0003 build) found the vault export omitted
the correction ledger (merge_invalidations, entity_retractions,
edge_reviews) and the ontology tables (kind_registry, kind_revisions,
entity_kind_assignments, entity_identities, kind_observations), so a
round-trip silently RESURRECTED deleted/retyped entities and LOST the
ontology. These tests build a vault carrying every category of
non-regenerable operator/agent state, export it, reconstruct it with the
dependency-free ``scripts/import_export.py`` reader, and assert the state
survived: no resurrection, no lost ontology.

The resolution semantics re-implemented over the imported DB (latest
merge that has no invalidation; latest kind assignment else the entity's
own kind; latest kind revision wins) deliberately mirror
``afair/substrate/entities.py`` / ``kinds.py`` — the point of the export
is that a stranger with only the JSONL and this documented shape can
reproduce afair's answers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from afair.mcp.export_route import _iter_export
from afair.substrate import (
    open_db,
    record_edge_review,
    resolve_canonical,
    resolve_entity_kind,
    retract_entity,
    retracted_entity_ids,
    write_entity,
    write_entity_edge,
    write_entity_merge,
    write_merge_invalidation,
)
from afair.substrate import tuner_state as tuner_state_mod
from afair.substrate.entities import assign_entity_kind
from afair.substrate.events import write_event_with_status
from afair.substrate.kinds import (
    register_kind,
    resolve_kind_slug,
    write_kind_observation,
    write_kind_revision,
)
from scripts.import_export import import_jsonl

if TYPE_CHECKING:
    from pathlib import Path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@pytest.fixture()
def vault_dir(tmp_path):
    p = tmp_path / "vault"
    p.mkdir()
    return p


def _build_vault_with_corrections_and_ontology(vault_dir: Path) -> dict[str, Any]:
    """Seed a vault with one instance of every non-regenerable decision.

    Returns the ids + source-vault resolution results the round-trip
    assertions compare against.
    """
    conn = open_db(vault_dir)
    try:
        event, _written = write_event_with_status(
            conn,
            kind="remember",
            origin="agent",
            payload={"content_type": "text", "text": "Sajinth published a survey"},
        )

        # Entities: one to keep + retype, one that is extraction noise,
        # two that got wrongly auto-merged.
        keep = write_entity(
            conn,
            canonical_name="Continual Learning Survey",
            kind="concept",
            created_by="canonicalizer:test",
            source_event_id=event.id,
            confidence=0.9,
        )
        noise = write_entity(
            conn,
            canonical_name="scripts/smoke_mcp.py",
            kind="other",
            created_by="canonicalizer:test",
            source_event_id=event.id,
            confidence=0.5,
        )
        dup_a = write_entity(
            conn,
            canonical_name="Sajinth",
            kind="person",
            created_by="canonicalizer:test",
            source_event_id=event.id,
            confidence=0.8,
        )
        dup_b = write_entity(
            conn,
            canonical_name="Sajinth Kumar",
            kind="person",
            created_by="canonicalizer:test",
            source_event_id=event.id,
            confidence=0.8,
        )

        # Correction ledger (ADR-0002): a rejected merge, a retraction,
        # a rejected edge.
        merge = write_entity_merge(
            conn,
            from_entity_id=dup_a.id,
            into_entity_id=dup_b.id,
            merged_by="dedup:test",
            reason="looked like the same person",
            confidence=0.7,
        )
        assert write_merge_invalidation(
            conn,
            merge_id=merge.id,
            invalidated_by="operator",
            reason="different people — elvah vs Athara",
        )
        assert retract_entity(
            conn,
            entity_id=noise.id,
            retracted_by="operator",
            reason="a file path, not an entity",
        )
        edge = write_entity_edge(
            conn,
            subject_id=keep.id,
            predicate="authored_by",
            object_id=dup_a.id,
            source_event_id=event.id,
            discovered_by="canonicalizer:test",
            confidence=0.6,
        )
        assert edge is not None
        record_edge_review(
            conn,
            edge_id=edge.id,
            verdict="reject",
            reviewed_by="operator",
            reason="co-occurrence, not authorship",
        )

        # Ontology (ADR-0003): a kind addition, a rename, a retype
        # through an assignment, and a preserved raw proposal.
        assert register_kind(
            conn,
            slug="research_paper",
            label="Research paper",
            created_by="operator",
        )
        write_kind_revision(
            conn,
            action="add",
            to_slug="research_paper",
            revised_by="operator",
            reason="promoted from usage signal",
        )
        assert register_kind(conn, slug="paper", label="Paper", created_by="operator")
        write_kind_revision(
            conn,
            action="rename",
            from_slug="research_paper",
            to_slug="paper",
            revised_by="operator",
            reason="shorter slug",
        )
        assign_entity_kind(
            conn,
            entity_id=keep.id,
            kind_slug="paper",
            assigned_by="operator",
            reason="it is a paper, not a bare concept",
            confidence=1.0,
        )
        write_kind_observation(
            conn,
            raw_kind="whitepaper",
            normalized_slug="concept",
            entity_id=keep.id,
            event_id=event.id,
            observed_by="extractor:test",
        )

        # Decided suggestion-queue rows: operator verdicts recorded
        # nowhere else. Direct INSERTs — both tables are mutable queues
        # with no dedicated substrate writer for a decided fixture.
        conn.execute(
            """INSERT INTO proposed_ontology_revisions
               (id, action, subject_slug, detail, evidence, confidence,
                detected_by, detected_at, status, decided_at, decided_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "ont_test_1",
                "deprecate",
                "concept",
                json.dumps({"reason": "low usage"}),
                json.dumps({"count": 2}),
                0.4,
                "schema_evolver:test",
                _now_iso(),
                "rejected",
                _now_iso(),
                "operator",
            ),
        )
        conn.execute(
            """INSERT INTO proposed_corrections
               (id, kind, entity_id, detail, evidence, confidence, tier,
                detected_by, detected_at, status, decided_at, decided_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "corr_test_1",
                "retype",
                dup_a.id,
                json.dumps({"to_kind": "organization"}),
                json.dumps({"signal": "weak"}),
                0.5,
                "review",
                "entity_audit:test",
                _now_iso(),
                "rejected",
                _now_iso(),
                "operator",
            ),
        )
        conn.commit()

        # Self-improvement log (I7).
        tuner_state_mod.write(
            conn,
            kind="promote",
            worker="extractor",
            tunable="temperature",
            old_value=0.2,
            new_value=0.1,
            rationale="round-trip fixture",
        )

        # Source-vault ground truth the imported copy must reproduce.
        return {
            "event_id": event.id,
            "keep": keep,
            "noise": noise,
            "dup_a": dup_a,
            "dup_b": dup_b,
            "merge_id": merge.id,
            "edge_id": edge.id,
            "src_retracted": retracted_entity_ids(conn),
            "src_canonical_dup_a": resolve_canonical(conn, dup_a.id),
            "src_kind_slug": resolve_kind_slug(conn, "research_paper"),
            "src_entity_kinds": {
                e.id: resolve_entity_kind(conn, e.id) for e in (keep, noise, dup_a, dup_b)
            },
        }
    finally:
        conn.close()


def _export_to_file(vault_dir: Path, dest: Path) -> list[dict[str, Any]]:
    lines = list(_iter_export(vault_dir, include_blobs=False))
    dest.write_text("".join(lines), encoding="utf-8")
    return [json.loads(line) for line in lines]


# ── re-implementations of the resolution semantics over the imported DB ──


def _imported_retracted(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT payload FROM entity_retractions").fetchall()
    return {json.loads(p)["entity_id"] for (p,) in rows}


def _imported_canonical(conn: sqlite3.Connection, eid: str) -> str:
    """Latest non-invalidated merge wins — mirrors resolve_canonical."""
    invalidated = {
        json.loads(p)["merge_id"]
        for (p,) in conn.execute("SELECT payload FROM merge_invalidations").fetchall()
    }
    current = eid
    for _ in range(64):  # cycle guard
        merges = [
            json.loads(p) for (p,) in conn.execute("SELECT payload FROM entity_merges").fetchall()
        ]
        live = [m for m in merges if m["from_entity_id"] == current and m["id"] not in invalidated]
        if not live:
            return current
        live.sort(key=lambda m: m["merged_at"])
        current = live[-1]["into_entity_id"]
    return current


def _imported_kind_slug(conn: sqlite3.Connection, slug: str) -> str:
    """Latest revision row wins — mirrors resolve_kind_slug."""
    revisions = [
        json.loads(p) for (p,) in conn.execute("SELECT payload FROM kind_revisions").fetchall()
    ]
    current = slug
    for _ in range(64):
        applicable = [r for r in revisions if r["from_slug"] == current]
        if not applicable:
            return current
        applicable.sort(key=lambda r: (r["revised_at"], r["id"]))
        latest = applicable[-1]
        if latest["action"] in ("rename", "merge") and latest["to_slug"]:
            current = latest["to_slug"]
        else:
            return current
    return current


def _imported_entity_kind(conn: sqlite3.Connection, eid: str) -> str:
    """Latest assignment, else the entity's own kind — mirrors
    entity_current_kind_v1 + the registry revision chain."""
    assignments = [
        json.loads(p)
        for (p,) in conn.execute("SELECT payload FROM entity_kind_assignments").fetchall()
        if json.loads(p)["entity_id"] == eid
    ]
    if assignments:
        assignments.sort(key=lambda a: (a["assigned_at"], a["id"]))
        return _imported_kind_slug(conn, assignments[-1]["kind_slug"])
    row = conn.execute("SELECT payload FROM entities WHERE id = ?", (eid,)).fetchone()
    assert row is not None, f"entity {eid} missing from import"
    return _imported_kind_slug(conn, json.loads(row[0])["entity_kind"])


# ── the round-trip ────────────────────────────────────────────────────────


def test_roundtrip_preserves_corrections_and_ontology(vault_dir, tmp_path) -> None:
    truth = _build_vault_with_corrections_and_ontology(vault_dir)
    export_file = tmp_path / "export.jsonl"
    records = _export_to_file(vault_dir, export_file)

    # Import via the dependency-free reader (the I4 bus-factor tool).
    imported_db = tmp_path / "recovered.db"
    counts = import_jsonl(export_file, imported_db)
    assert counts["manifest"] == 1
    assert counts["merge_invalidation"] == 1
    assert counts["entity_retraction"] == 1
    assert counts["edge_review"] == 1
    assert counts["kind_revision"] == 2
    assert counts["entity_kind_assignment"] == 1
    assert counts["kind_observation"] == 1
    assert counts["entity_identity"] == 4
    assert counts["proposed_ontology_revision"] == 1
    assert counts["proposed_correction"] == 1
    assert counts["tuner_state"] == 1
    # 7 bootstrap kinds + research_paper + paper.
    assert counts["kind_registry"] == 9

    conn = sqlite3.connect(imported_db)
    try:
        # 1. The retracted entity stays retracted — NO resurrection.
        assert _imported_retracted(conn) == truth["src_retracted"]
        assert truth["noise"].id in _imported_retracted(conn)

        # 2. The rejected merge stays rejected: dup_a resolves to ITSELF,
        #    exactly as the source vault resolves it — not into dup_b.
        assert _imported_canonical(conn, truth["dup_a"].id) == truth["src_canonical_dup_a"]
        assert _imported_canonical(conn, truth["dup_a"].id) == truth["dup_a"].id

        # 3. The edge verdict survived: the reject review AND the
        #    invalidation it cascaded are both present.
        reviews = [
            json.loads(p) for (p,) in conn.execute("SELECT payload FROM edge_reviews").fetchall()
        ]
        assert [(r["edge_id"], r["verdict"]) for r in reviews] == [(truth["edge_id"], "reject")]
        edge_invalidations = [
            json.loads(p)
            for (p,) in conn.execute("SELECT payload FROM edge_invalidations").fetchall()
        ]
        assert any(inv["edge_id"] == truth["edge_id"] for inv in edge_invalidations)

        # 4. The renamed/added kind resolves the same: research_paper → paper.
        assert _imported_kind_slug(conn, "research_paper") == truth["src_kind_slug"] == "paper"
        registry_slugs = {
            json.loads(p)["slug"]
            for (p,) in conn.execute("SELECT payload FROM kind_registry").fetchall()
        }
        assert {"research_paper", "paper", "person", "concept"} <= registry_slugs

        # 5. Entity kinds resolve identically for EVERY entity — the
        #    retyped one through its assignment, the untouched ones through
        #    the (previously clobbered) entity_kind fallback.
        for eid, src_kind in truth["src_entity_kinds"].items():
            assert _imported_entity_kind(conn, eid) == src_kind, eid
        assert _imported_entity_kind(conn, truth["keep"].id) == "paper"
        assert _imported_entity_kind(conn, truth["dup_a"].id) == "person"

        # 6. The v2 identity ledger survived with its ordinals.
        identities = {
            json.loads(p)["entity_id"]: json.loads(p)
            for (p,) in conn.execute("SELECT payload FROM entity_identities").fetchall()
        }
        assert set(identities) == {
            truth["keep"].id,
            truth["noise"].id,
            truth["dup_a"].id,
            truth["dup_b"].id,
        }
        for ident in identities.values():
            assert ident["id_scheme"]
            assert ident["disambiguator"] == "0"

        # 7. The preserved raw kind proposal + the decided queue rows.
        observations = [
            json.loads(p)
            for (p,) in conn.execute("SELECT payload FROM kind_observations").fetchall()
        ]
        assert [(o["raw_kind"], o["normalized_slug"]) for o in observations] == [
            ("whitepaper", "concept")
        ]
        ont = json.loads(
            conn.execute("SELECT payload FROM proposed_ontology_revisions").fetchone()[0]
        )
        assert (ont["status"], ont["decided_by"]) == ("rejected", "operator")
        corr = json.loads(conn.execute("SELECT payload FROM proposed_corrections").fetchone()[0])
        assert (corr["status"], corr["proposed_correction_kind"]) == ("rejected", "retype")

        # 8. The tuner's self-modification record (I7).
        tuner = json.loads(conn.execute("SELECT payload FROM tuner_state").fetchone()[0])
        assert tuner["tuner_state_kind"] == "promote"
        assert (tuner["worker"], tuner["tunable"]) == ("extractor", "temperature")
    finally:
        conn.close()

    # The manifest is still the terminator, and the derived indexes stay out.
    assert records[-1]["kind"] == "manifest"


def test_export_entity_record_carries_its_own_kind(vault_dir) -> None:
    """Regression: the record discriminator used to CLOBBER entities.kind,
    so the entity's type was silently destroyed in every export. It now
    travels as ``entity_kind`` (additive — nothing could have depended on
    the old, destroyed field)."""
    _build_vault_with_corrections_and_ontology(vault_dir)
    records = [json.loads(line) for line in _iter_export(vault_dir, include_blobs=False)]
    entities = {r["canonical_name"]: r for r in records if r["kind"] == "entity"}
    assert entities["Continual Learning Survey"]["entity_kind"] == "concept"
    assert entities["Sajinth"]["entity_kind"] == "person"


def test_export_excludes_regenerable_and_credential_tables(vault_dir) -> None:
    """The derived indexes (events_fts / events_vec), the re-derivable
    event_temporal, the pipeline diagnostics, and the credential/job tables
    stay OUT of the export — unchanged, by design."""
    truth = _build_vault_with_corrections_and_ontology(vault_dir)

    # Seed rows in the excluded-by-design tables so absence is meaningful.
    conn = open_db(vault_dir)
    try:
        conn.execute(
            """INSERT INTO event_temporal
               (id, event_id, event_hash, temporal_class, confidence,
                computed_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "temporal_test_1",
                truth["event_id"],
                conn.execute(
                    "SELECT content_hash FROM events WHERE id = ?", (truth["event_id"],)
                ).fetchone()["content_hash"],
                "durable",
                0.9,
                "temporal:test",
                _now_iso(),
            ),
        )
        conn.execute(
            """INSERT INTO pipeline_events
               (id, event_id, stage, status, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("pipe_test_1", truth["event_id"], "event.written", "ok", _now_iso()),
        )
        conn.execute(
            """INSERT INTO api_tokens (id, label, token_hash, created_at)
               VALUES (?, ?, ?, ?)""",
            ("tok_test_1", "test", "a" * 64, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    records = [json.loads(line) for line in _iter_export(vault_dir, include_blobs=False)]
    kinds = {r["kind"] for r in records}
    # Exact kind set for this fixture (it writes no mentions and no blobs).
    assert kinds == {
        "event",
        "entity",
        "entity_edge",
        "entity_merge",
        "edge_invalidation",
        "merge_invalidation",
        "entity_retraction",
        "edge_review",
        "entity_identity",
        "entity_kind_assignment",
        "kind_registry",
        "kind_revision",
        "kind_observation",
        "proposed_correction",
        "proposed_ontology_revision",
        "tuner_state",
        "manifest",
    }
    text = "".join(json.dumps(r) for r in records)
    assert "temporal_test_1" not in text
    assert "pipe_test_1" not in text
    assert "tok_test_1" not in text
    assert "a" * 64 not in text  # no token hash leaks into the dump

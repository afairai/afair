"""ADR-0003 Phase 4 — Schema-Evolver tests (propose-only mode).

Covers the deterministic signal detectors against seeded vaults, the
LLM-drafting backstops (mocked ``call_tool`` — no real LLM calls), the
per-cycle caps, the 30-day cooldown, dedup on re-runs, the propose-only
guarantee (nothing outside the quarantine queue is written), the queue's
deliberate mutability next to the protected substrate tables, and the
SCHEMA_EVOLVER_MODEL settings plumbing.

Like test_entity_audit.py, nothing below the LLM boundary is mocked —
real SQLite, real triggers, real view resolution.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from ulid import ULID

from afair.agents import schema_evolver as se
from afair.agents.llm import LLMResult
from afair.agents.schema_evolver import (
    MAX_LLM_CALLS_PER_CYCLE,
    MAX_PROPOSALS_PER_CYCLE,
    SchemaEvolver,
    detect_near_duplicate_kinds,
    detect_overbroad_other,
    detect_promotable_raw_kinds,
    detect_unused_kinds,
    kind_usage_distribution,
    slugify_raw_kind,
    validate_drafted_add,
)
from afair.settings import Settings
from afair.substrate import open_db, write_entity, write_event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = datetime.now(UTC)


# ── fixtures + helpers ───────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


@pytest.fixture(autouse=True)
def _no_llm_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 3s inter-call sleep is rate-limit hygiene, not behavior — zero it."""
    monkeypatch.setattr(se, "INTER_CALL_SLEEP_SECONDS", 0.0)


def _entity(db: sqlite3.Connection, name: str, kind: str) -> tuple[str, str]:
    """One entity + its anchoring event. Returns (entity_id, event_id)."""
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": name}
    )
    ent = write_entity(
        db, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    )
    return ent.id, ev.id


def _seed_entities(db: sqlite3.Connection, kind: str, n: int, *, prefix: str = "") -> list[str]:
    return [_entity(db, f"{prefix}{kind}-{i}", kind)[0] for i in range(n)]


def _observe(
    db: sqlite3.Connection,
    *,
    entity_id: str,
    event_id: str,
    raw_kind: str,
    normalized_slug: str = "other",
    observed_at: str,
) -> None:
    """Direct kind_observations insert — the ledger writer stamps now(),
    and the span-based tests need explicit timestamps."""
    with db:
        db.execute(
            """
            INSERT INTO kind_observations (
                id, raw_kind, normalized_slug, entity_id, event_id, observed_at, observed_by
            ) VALUES (?, ?, ?, ?, ?, ?, 'test')
            """,
            (str(ULID()), raw_kind, normalized_slug, entity_id, event_id, observed_at),
        )


def _register_kind(db: sqlite3.Connection, slug: str, *, created_at: str) -> None:
    with db:
        db.execute(
            """
            INSERT INTO kind_registry (
                id, slug, label, description, created_at, created_by, source_event_id
            ) VALUES (?, ?, ?, NULL, ?, 'test', NULL)
            """,
            (f"kind:{slug}", slug, slug.title(), created_at),
        )


def _revise(
    db: sqlite3.Connection,
    *,
    action: str,
    from_slug: str | None,
    to_slug: str | None = None,
    revised_at: str,
) -> None:
    with db:
        db.execute(
            """
            INSERT INTO kind_revisions (
                id, action, from_slug, to_slug, detail, revised_at, revised_by,
                reason, source_event_id
            ) VALUES (?, ?, ?, ?, NULL, ?, 'test', 'test', NULL)
            """,
            (str(ULID()), action, from_slug, to_slug, revised_at),
        )


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).isoformat()


def _mock_llm(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace call_tool with a fake returning ``payload``; returns the
    list of captured call kwargs so tests can assert call counts."""
    calls: list[dict[str, Any]] = []

    def fake_call_tool(**kwargs: Any) -> LLMResult:
        calls.append(kwargs)
        return LLMResult(data=dict(payload), model="test/mock", raw=json.dumps(payload))

    monkeypatch.setattr(se, "call_tool", fake_call_tool)
    return calls


def _forbid_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**kwargs: Any) -> LLMResult:
        raise AssertionError("call_tool must not be called in this test")

    monkeypatch.setattr(se, "call_tool", explode)


def _queue_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM proposed_ontology_revisions ORDER BY detected_at, id"
    ).fetchall()


_SUBSTRATE_TABLES = (
    "kind_registry",
    "kind_revisions",
    "kind_observations",
    "entities",
    "entity_kind_assignments",
    "entity_merges",
    "events",
)


def _substrate_counts(db: sqlite3.Connection) -> dict[str, int]:
    return {
        t: db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in _SUBSTRATE_TABLES
    }


# ── signal mining: usage distribution ────────────────────────────────────────


def test_usage_distribution_counts_live_entities_per_resolved_kind(
    db: sqlite3.Connection,
) -> None:
    _seed_entities(db, "person", 3)
    ids = _seed_entities(db, "product", 2)
    # Retype one product to project via an assignment row — the distribution
    # must read the CURRENT kind through the resolution view.
    with db:
        db.execute(
            """
            INSERT INTO entity_kind_assignments (
                id, entity_id, kind_slug, assigned_at, assigned_by, confidence,
                reason, source_event_id
            ) VALUES (?, ?, 'project', ?, 'test', 1.0, 'test', NULL)
            """,
            (str(ULID()), ids[0], NOW.isoformat()),
        )
    usage = kind_usage_distribution(db)
    assert usage == {"person": 3, "product": 1, "project": 1}


def test_usage_distribution_follows_registry_revision_chain(
    db: sqlite3.Connection,
) -> None:
    """A registry-level rename re-buckets every affected entity at read time."""
    _seed_entities(db, "product", 2)
    _register_kind(db, "tool", created_at=NOW.isoformat())
    _revise(db, action="rename", from_slug="product", to_slug="tool", revised_at=NOW.isoformat())
    usage = kind_usage_distribution(db)
    assert usage == {"tool": 2}


def test_usage_distribution_excludes_merged_and_retracted(db: sqlite3.Connection) -> None:
    keep, _ = _entity(db, "Keep", "person")
    gone_merge, _ = _entity(db, "Gone-merge", "person")
    gone_retract, _ = _entity(db, "Gone-retract", "person")
    with db:
        db.execute(
            """
            INSERT INTO entity_merges (id, from_entity_id, into_entity_id, merged_at,
                                       merged_by, reason, confidence)
            VALUES (?, ?, ?, ?, 'test', 'test', 0.9)
            """,
            (str(ULID()), gone_merge, keep, NOW.isoformat()),
        )
        db.execute(
            """
            INSERT INTO entity_retractions (id, entity_id, retracted_at, retracted_by,
                                            reason, source_event_id)
            VALUES (?, ?, ?, 'test', 'test', NULL)
            """,
            (str(ULID()), gone_retract, NOW.isoformat()),
        )
    assert kind_usage_distribution(db) == {"person": 1}


# ── signal mining: over-broad 'other' ────────────────────────────────────────


def test_overbroad_other_detected(db: sqlite3.Connection) -> None:
    _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    usage = kind_usage_distribution(db)
    cand = detect_overbroad_other(db, usage, share_threshold=0.20, min_entities=10)
    assert cand is not None
    assert cand.action == "add"
    assert cand.needs_llm
    assert cand.detail["source_slug"] == "other"
    assert "other" in cand.touched_slugs
    assert len(cand.sample) == 12  # every 'other' entity fits under SAMPLE_SIZE


def test_overbroad_other_requires_min_entity_count(db: sqlite3.Connection) -> None:
    """66% share but only 2 rows — a tiny vault never triggers a carve."""
    _seed_entities(db, "other", 2)
    _seed_entities(db, "person", 1)
    usage = kind_usage_distribution(db)
    assert detect_overbroad_other(db, usage, share_threshold=0.20, min_entities=10) is None


def test_overbroad_other_requires_share_above_threshold(db: sqlite3.Connection) -> None:
    _seed_entities(db, "other", 10)
    _seed_entities(db, "person", 90)
    usage = kind_usage_distribution(db)
    assert detect_overbroad_other(db, usage, share_threshold=0.20, min_entities=10) is None


# ── signal mining: promotable raw kinds ──────────────────────────────────────


def _seed_raw_kind(
    db: sqlite3.Connection,
    raw_kind: str,
    *,
    n_entities: int,
    span_days: int,
    prefix: str = "",
) -> list[str]:
    """``n_entities`` distinct entities each observed once under ``raw_kind``,
    with observation timestamps spread across ``span_days``."""
    ids: list[str] = []
    for i in range(n_entities):
        eid, evid = _entity(db, f"{prefix}{raw_kind}-{i}", "other")
        # Spread from span_days ago (i=0) to now (i=n-1).
        elapsed = (span_days * i) // max(1, n_entities - 1)
        _observe(
            db,
            entity_id=eid,
            event_id=evid,
            raw_kind=raw_kind,
            observed_at=_days_ago(span_days - elapsed),
        )
        ids.append(eid)
    return ids


def test_promotable_raw_kind_detected(db: sqlite3.Connection) -> None:
    _seed_raw_kind(db, "Research Paper", n_entities=10, span_days=20)
    cands = detect_promotable_raw_kinds(db, min_entities=10)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.action == "add"
    assert cand.subject_slug == "research_paper"
    assert cand.detail["proposed_slug"] == "research_paper"
    assert cand.needs_llm
    assert len(cand.sample) == 10
    assert "10 distinct entities" in cand.evidence


def test_promotable_raw_kind_requires_entity_count(db: sqlite3.Connection) -> None:
    _seed_raw_kind(db, "Research Paper", n_entities=4, span_days=20)
    assert detect_promotable_raw_kinds(db, min_entities=10) == []


def test_promotable_raw_kind_requires_time_span(db: sqlite3.Connection) -> None:
    """A single-day burst (one big document) is not sustained usage."""
    _seed_raw_kind(db, "Research Paper", n_entities=12, span_days=0)
    assert detect_promotable_raw_kinds(db, min_entities=10) == []


def test_promotable_raw_kind_skips_live_kinds(db: sqlite3.Connection) -> None:
    """Defensive: a raw kind whose slug is already live is never re-proposed."""
    _seed_raw_kind(db, "Person", n_entities=10, span_days=20)
    assert detect_promotable_raw_kinds(db, min_entities=10) == []


def test_slugify_raw_kind() -> None:
    assert slugify_raw_kind("Research Paper") == "research_paper"
    assert slugify_raw_kind("  API-Endpoint  ") == "api_endpoint"
    assert slugify_raw_kind("研究") is None  # nothing slug-able survives
    assert slugify_raw_kind("42nd_thing") is None  # must start with a letter
    assert slugify_raw_kind("x" * 40) is None  # too long


# ── signal mining: near-duplicate kinds ──────────────────────────────────────


def test_near_duplicate_by_co_occurrence(db: sqlite3.Connection) -> None:
    """Same entities observed under raw kinds landing in two live kinds."""
    for i in range(5):
        eid, evid = _entity(db, f"straddler-{i}", "product")
        _observe(
            db,
            entity_id=eid,
            event_id=evid,
            raw_kind="tooling",
            normalized_slug="product",
            observed_at=_days_ago(5),
        )
        _observe(
            db,
            entity_id=eid,
            event_id=evid,
            raw_kind="initiative",
            normalized_slug="project",
            observed_at=_days_ago(3),
        )
    usage = {"product": 10, "project": 2}
    cands = detect_near_duplicate_kinds(db, usage, min_co_entities=5)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.action == "merge"
    # Smaller-usage kind merges into the larger.
    assert cand.detail["from_slug"] == "project"
    assert cand.detail["to_slug"] == "product"
    assert cand.detail["co_occurring_entities"] == 5
    assert not cand.needs_llm


def test_near_duplicate_below_co_occurrence_threshold(db: sqlite3.Connection) -> None:
    eid, evid = _entity(db, "solo", "product")
    _observe(
        db,
        entity_id=eid,
        event_id=evid,
        raw_kind="a",
        normalized_slug="product",
        observed_at=_days_ago(2),
    )
    _observe(
        db,
        entity_id=eid,
        event_id=evid,
        raw_kind="b",
        normalized_slug="project",
        observed_at=_days_ago(1),
    )
    assert detect_near_duplicate_kinds(db, {"product": 1, "project": 1}, min_co_entities=5) == []


def test_near_duplicate_by_lexical_plural(db: sqlite3.Connection) -> None:
    _register_kind(db, "tool", created_at=NOW.isoformat())
    _register_kind(db, "tools", created_at=NOW.isoformat())
    cands = detect_near_duplicate_kinds(db, {"tool": 7, "tools": 2})
    assert len(cands) == 1
    assert cands[0].action == "merge"
    assert cands[0].detail == {"from_slug": "tools", "to_slug": "tool", "signal": "lexical_variant"}


# ── signal mining: unused kinds ──────────────────────────────────────────────


def test_unused_kind_detected_after_min_age(db: sqlite3.Connection) -> None:
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _seed_entities(db, "person", 1)
    usage = kind_usage_distribution(db)
    cands = detect_unused_kinds(db, usage, now=NOW)
    assert [c.subject_slug for c in cands] == ["gadget"]
    assert cands[0].action == "deprecate"
    assert not cands[0].needs_llm


def test_unused_kind_grace_for_young_kinds_and_other(db: sqlite3.Connection) -> None:
    _register_kind(db, "fresh", created_at=_days_ago(5))
    _seed_entities(db, "person", 1)
    usage = kind_usage_distribution(db)  # bootstrap kinds incl. 'other' have 0 usage
    cands = detect_unused_kinds(db, usage, now=NOW)
    slugs = {c.subject_slug for c in cands}
    assert "fresh" not in slugs  # too young
    assert "other" not in slugs  # normalization fallback is exempt
    # Bootstrap kinds were seeded at vault open (now) — also inside the grace.
    assert slugs == set()


# ── the worker: full cycles ──────────────────────────────────────────────────


def test_run_writes_deterministic_proposal_without_llm(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _seed_entities(db, "person", 1)

    stats = SchemaEvolver().run(db, settings)

    assert stats["proposals"] == 1
    assert stats["llm_calls"] == 0
    rows = _queue_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"].startswith("ont_")
    assert row["action"] == "deprecate"
    assert row["subject_slug"] == "gadget"
    assert row["status"] == "proposed"
    assert row["detected_by"] == "schema_evolver:v0"
    assert row["decided_at"] is None
    assert "zero live entities" in row["evidence"]
    assert 0.0 <= row["confidence"] <= 1.0
    # The cycle marker landed.
    marker = db.execute(
        "SELECT 1 FROM pipeline_events WHERE stage = 'schema_evolver.cycle'"
    ).fetchone()
    assert marker is not None


def test_run_llm_add_proposal_lands(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_ids = _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    # One of the 'other' entities carries a raw-kind observation — the
    # naming signal the drafting prompt must surface.
    eid, evid = _entity(db, "some whitepaper", "other")
    _observe(db, entity_id=eid, event_id=evid, raw_kind="whitepaper", observed_at=_days_ago(1))
    reassign = sorted(other_ids[:3])
    calls = _mock_llm(
        monkeypatch,
        {
            "slug": "research_paper",
            "label": "Research paper",
            "description": "Published academic papers the user reads or cites.",
            "rationale": "The vault keeps filing papers under 'other'.",
            "reassign_entity_ids": reassign,
            "confidence": 0.8,
        },
    )

    stats = SchemaEvolver().run(db, settings)

    assert stats["proposals"] == 1
    assert len(calls) == 1
    rows = _queue_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "add"
    assert row["subject_slug"] == "research_paper"
    assert row["status"] == "proposed"
    assert row["confidence"] == 0.8
    detail = json.loads(row["detail"])
    assert detail["new_slug"] == "research_paper"
    assert detail["label"] == "Research paper"
    assert detail["source_slug"] == "other"
    assert detail["reassign_entity_ids"] == reassign
    # Untrusted wrapping made it into the prompt sent to the model,
    # and the observed raw kinds are rendered as the naming signal.
    assert "<event_content>" in calls[0]["user"]
    assert "whitepaper" in calls[0]["user"]


@pytest.mark.parametrize(
    ("draft_overrides", "reason"),
    [
        ({"slug": "Research Paper!"}, "bad slug format"),
        ({"slug": "person"}, "collides with a registry kind"),
        ({"reassign_entity_ids": ["entity:v2:hallucinated"]}, "out-of-sample entity id"),
        ({"reassign_entity_ids": [f"fake-{i}" for i in range(51)]}, "reassign list over cap"),
    ],
)
def test_run_rejects_backstop_violations(
    db: sqlite3.Connection,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    draft_overrides: dict[str, Any],
    reason: str,
) -> None:
    _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    draft = {
        "slug": "research_paper",
        "label": "Research paper",
        "description": "d",
        "rationale": "r",
        "reassign_entity_ids": [],
        "confidence": 0.8,
        **draft_overrides,
    }
    _mock_llm(monkeypatch, draft)

    stats = SchemaEvolver().run(db, settings)

    assert stats["rejected_backstop"] == 1, reason
    assert stats["proposals"] == 0, reason
    assert _queue_rows(db) == [], reason


def test_drafted_slug_must_match_signal_slug(db: sqlite3.Connection) -> None:
    """A raw-kind promotion's slug is pre-determined; a model that renames
    it has drifted from the evidence."""
    _seed_raw_kind(db, "Research Paper", n_entities=10, span_days=20)
    cand = detect_promotable_raw_kinds(db, min_entities=10)[0]
    draft = {
        "slug": "papers_and_stuff",
        "label": "L",
        "description": "D",
        "rationale": "R",
        "reassign_entity_ids": [],
        "confidence": 0.9,
    }
    assert validate_drafted_add(db, cand, draft) is None


def test_per_cycle_proposal_cap_holds(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    for slug in ("gadget_a", "gadget_b", "gadget_c"):
        _register_kind(db, slug, created_at=_days_ago(100))
    _seed_entities(db, "person", 1)

    stats = SchemaEvolver().run(db, settings)

    assert stats["proposals"] == MAX_PROPOSALS_PER_CYCLE
    assert len(_queue_rows(db)) == MAX_PROPOSALS_PER_CYCLE


def test_per_cycle_llm_call_cap_holds(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five promotable raw kinds, every draft rejected by the slug backstop:
    the LLM budget (not the proposal cap) governs, and stops at 4 calls."""
    for i, raw in enumerate(("alpha kind", "beta kind", "gamma kind", "delta kind", "eps kind")):
        _seed_raw_kind(db, raw, n_entities=10, span_days=20, prefix=f"rk{i}-")
    calls = _mock_llm(monkeypatch, {"slug": "NOT A SLUG"})

    stats = SchemaEvolver().run(db, settings)

    assert stats["llm_calls"] == MAX_LLM_CALLS_PER_CYCLE
    assert len(calls) == MAX_LLM_CALLS_PER_CYCLE
    assert stats["proposals"] == 0
    assert _queue_rows(db) == []


def test_cooldown_after_recent_revision(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _revise(db, action="restore", from_slug="gadget", revised_at=_days_ago(3))
    _seed_entities(db, "person", 1)

    stats = SchemaEvolver().run(db, settings)

    assert stats["skipped_cooldown"] == 1
    assert stats["proposals"] == 0
    assert _queue_rows(db) == []


def test_cooldown_after_recent_rejected_proposal(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _seed_entities(db, "person", 1)
    # The operator rejected a (different-action) proposal touching 'gadget'
    # two days ago — the slug is in cooldown for any new churn.
    with db:
        db.execute(
            """
            INSERT INTO proposed_ontology_revisions (
                id, action, subject_slug, detail, evidence, confidence,
                detected_by, detected_at, status, decided_at, decided_by
            ) VALUES (?, 'merge', 'gadget', '{}', 'e', 0.5, 'test', ?, 'rejected', ?, 'operator')
            """,
            (f"ont_{ULID()!s}", _days_ago(10), _days_ago(2)),
        )

    stats = SchemaEvolver().run(db, settings)

    assert stats["skipped_cooldown"] == 1
    assert stats["proposals"] == 0
    assert len(_queue_rows(db)) == 1  # only the pre-existing rejected row


def test_rerun_is_a_noop_dedup(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _seed_entities(db, "person", 1)

    first = SchemaEvolver().run(db, settings)
    second = SchemaEvolver().run(db, settings)

    assert first["proposals"] == 1
    assert second["proposals"] == 0
    assert second["skipped_dedup"] == 1
    assert len(_queue_rows(db)) == 1


def test_pending_carve_from_other_is_not_stacked(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cycle must not pile a differently-named carve out of 'other'
    on top of an undecided one — and must not burn an LLM call trying."""
    _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    calls = _mock_llm(
        monkeypatch,
        {
            "slug": "research_paper",
            "label": "L",
            "description": "D",
            "rationale": "R",
            "reassign_entity_ids": [],
            "confidence": 0.7,
        },
    )

    SchemaEvolver().run(db, settings)
    second = SchemaEvolver().run(db, settings)

    assert len(calls) == 1  # no second draft
    assert second["skipped_dedup"] == 1
    assert len(_queue_rows(db)) == 1


def test_worker_writes_nothing_outside_queue(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The propose-only guarantee: after a full cycle that produces both a
    deterministic and an LLM-drafted proposal, every substrate table
    (registry, revisions, observations, entities, assignments, merges,
    events) is byte-count-identical — only the quarantine queue grew."""
    other_ids = _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _mock_llm(
        monkeypatch,
        {
            "slug": "research_paper",
            "label": "Research paper",
            "description": "D",
            "rationale": "R",
            "reassign_entity_ids": other_ids[:2],
            "confidence": 0.8,
        },
    )
    before = _substrate_counts(db)

    stats = SchemaEvolver().run(db, settings)

    assert stats["proposals"] == 2
    assert _substrate_counts(db) == before
    assert len(_queue_rows(db)) == 2


def test_empty_vault_is_a_clean_noop(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)

    stats = SchemaEvolver().run(db, settings)

    assert stats["live_entities"] == 0
    assert stats["proposals"] == 0
    assert _queue_rows(db) == []
    marker = db.execute(
        "SELECT 1 FROM pipeline_events WHERE stage = 'schema_evolver.cycle'"
    ).fetchone()
    assert marker is not None


# ── queue mutability vs substrate protection ─────────────────────────────────


def test_queue_is_mutable_while_substrate_stays_protected(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_llm(monkeypatch)
    _register_kind(db, "gadget", created_at=_days_ago(100))
    _seed_entities(db, "person", 1)
    SchemaEvolver().run(db, settings)
    (row,) = _queue_rows(db)

    # The queue row's status transition (the Phase-5 decide path) works —
    # the table is intentionally outside the I2 trigger regime.
    with db:
        db.execute(
            "UPDATE proposed_ontology_revisions SET status = 'rejected', "
            "decided_at = ?, decided_by = 'operator' WHERE id = ?",
            (NOW.isoformat(), row["id"]),
        )
    updated = _queue_rows(db)[0]
    assert updated["status"] == "rejected"
    assert updated["decided_by"] == "operator"

    # The append-only substrate next door still refuses mutation.
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE kind_registry SET label = 'x' WHERE slug = 'gadget'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM kind_registry WHERE slug = 'gadget'")


# ── settings plumbing ────────────────────────────────────────────────────────


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("EXTRACTOR_MODEL", "SCHEMA_EVOLVER_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_schema_evolver_model_defaults_to_extractor_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "openai/gpt-4o-mini")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.schema_evolver_model == "openai/gpt-4o-mini"


def test_schema_evolver_model_override_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("EXTRACTOR_MODEL", "anthropic/claude-haiku-4-5")
    monkeypatch.setenv("SCHEMA_EVOLVER_MODEL", "anthropic/claude-sonnet-4-5")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.schema_evolver_model == "anthropic/claude-sonnet-4-5"
    assert s.extractor_model == "anthropic/claude-haiku-4-5"


def test_worker_uses_schema_evolver_model(
    db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drafting call runs on settings.schema_evolver_model, not the
    extractor's — the per-agent override reaches the LLM boundary."""
    _seed_entities(db, "other", 12)
    _seed_entities(db, "person", 8)
    calls = _mock_llm(
        monkeypatch,
        {
            "slug": "research_paper",
            "label": "L",
            "description": "D",
            "rationale": "R",
            "reassign_entity_ids": [],
            "confidence": 0.6,
        },
    )
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
        schema_evolver_model="anthropic/claude-sonnet-4-5",
    )

    SchemaEvolver().run(db, settings)

    assert calls[0]["model"] == "anthropic/claude-sonnet-4-5"

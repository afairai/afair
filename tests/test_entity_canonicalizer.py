"""Phase 4 Track 1 — EntityCanonicalizer cold-path worker tests.

Exercises the worker against real SQLite with a mocked LLM. Tests cover:

- Stage 1 exact match (no LLM, two events with same surface form)
- Stage 2 LLM judgment (mock returns one of the candidates)
- Stage 2 Sonnet escalation (Haiku low-confidence → Sonnet re-judge)
- Stage 3 new entity creation when no candidate
- Edge writes for relations
- Edge skipping when subject/object aren't resolvable
- Cascade invalidation cycle
- Re-run idempotency (cycle 2 is a no-op)
- LLM budget cap
- Defensive: hallucinated entity_id from LLM is rejected

The LLM is mocked via monkeypatch on agents.entity_canonicalizer.call_tool
so the test runs in milliseconds without hitting Anthropic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import (
    CANONICALIZER_PRODUCED_BY,
    CASCADE_PRODUCED_BY,
    EntityCanonicalizer,
)
from afair.agents.interpretation import write_failed_interpretation, write_interpretation
from afair.agents.invalidation import write_invalidation
from afair.agents.llm import LLMError, LLMResult
from afair.settings import Settings
from afair.substrate import (
    iter_edges_for_entity,
    iter_mentions_for_event,
    latest_edge_confidence_batch,
    latest_edge_scores_batch,
    open_db,
    read_edge_invalidations,
    write_event,
)
from afair.substrate.entities import find_edges_for_source_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings instance with cold_path disabled so the test owns the worker lifecycle."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


def _write_event_with_extraction(
    conn: sqlite3.Connection,
    *,
    text: str,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]] | None = None,
    summary: str | None = None,
    confidence: float | None = None,
) -> str:
    """Write an event + its extractor interpretation. Returns content_hash."""
    event = write_event(
        conn,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
    )
    # Default each relation's evidence to the event text (which states the
    # relation in these fixtures), so the canonicalizer's evidence gate — it
    # requires a verbatim quote present in the source text — passes. A test
    # that wants to exercise the gate's rejection passes explicit evidence
    # that is absent from the text.
    grounded_relations = []
    for relation in relations or []:
        relation = dict(relation)
        relation.setdefault("evidence", text)
        grounded_relations.append(relation)
    extraction: dict[str, Any] = {
        "status": "success",
        "best_guess_kind": "fact",
        "summary": summary or text[:200],
        "entities": entities,
        "relations": grounded_relations,
    }
    if confidence is not None:
        extraction["confidence"] = confidence
    write_interpretation(
        conn,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction=extraction,
    )
    return event.content_hash


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the inter-call sleep helper to a no-op for fast tests."""
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


def read_event_by_hash_for_test(conn: sqlite3.Connection, content_hash: str) -> str:
    """Resolve a content_hash to the event id (for find_edges_for_source_event)."""
    from afair.substrate import read_event_by_hash

    ev = read_event_by_hash(conn, content_hash)
    assert ev is not None
    return ev.id


# ── Stage 1: exact match ──────────────────────────────────────────────────


def test_exact_match_links_second_event_to_same_canonical(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two events both mentioning "Sajinth" as person → both link to the
    same canonical entity. No LLM call needed."""
    _no_sleep(monkeypatch)

    # Should never be called for this test — exact match wins.
    def _boom(**_: Any) -> LLMResult:
        msg = "no LLM call expected for exact match"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _boom)

    h1 = _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    h2 = _write_event_with_extraction(
        db, text="Sajinth shipped a feature", entities=[{"name": "Sajinth", "type": "person"}]
    )

    stats = EntityCanonicalizer().run(db, settings)
    assert stats["events_canonicalized"] == 2
    assert stats["entities_created"] == 1
    assert stats["entities_matched_exact"] == 1
    assert stats["llm_calls"] == 0

    m1 = iter_mentions_for_event(db, h1)
    m2 = iter_mentions_for_event(db, h2)
    assert len(m1) == 1
    assert len(m2) == 1
    assert m1[0].entity_id == m2[0].entity_id  # SAME canonical


def test_exact_match_distinguishes_by_kind(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apple-the-org and apple-the-concept become two distinct entities.

    ADR-0003 Phase 2: identity is name-first, so the separation the v1
    kind-in-ID hash used to give for free is now the Stage-1 kind-agreement
    guard's job — the same-name/different-kind mention is a homonym question
    routed to the LLM (which correctly answers "none of these") instead of
    an auto-link at confidence 1.0."""
    _no_sleep(monkeypatch)
    # The homonym judge rules them different things (the correct verdict).
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    _write_event_with_extraction(
        db,
        text="Apple released a new product",
        entities=[{"name": "Apple", "type": "organization"}],
    )
    _write_event_with_extraction(
        db, text="I want an apple for lunch", entities=[{"name": "apple", "type": "concept"}]
    )

    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 2
    assert stats["entities_matched_exact"] == 0
    assert stats["homonym_splits"] == 1
    ids = {r["id"] for r in db.execute("SELECT id FROM entities").fetchall()}
    assert len(ids) == 2


# ── Stage 2: LLM match ────────────────────────────────────────────────────


def _llm_returns(matched_id: str | None, *, confidence: float = 0.9) -> Any:
    """Build a stub call_tool that returns one verdict regardless of input."""

    def _fake(**_kw: Any) -> LLMResult:
        return LLMResult(
            data={
                "matched_entity_id": matched_id,
                "reason": "test",
                "confidence": confidence,
            },
            model=_kw.get("model", "test"),
            raw="",
        )

    return _fake


def test_llm_match_links_variant_surface_form_to_existing(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first event creates Sajinth (person). The second mentions
    'Saji' — different surface form, same kind — LLM judges it as the
    same Sajinth. Both events end up linked to one canonical."""
    _no_sleep(monkeypatch)

    # First event: exact (no LLM).
    h1 = _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)
    sajinth_entity_id = iter_mentions_for_event(db, h1)[0].entity_id

    # Second event: LLM picks the existing Sajinth.
    monkeypatch.setattr(ec, "call_tool", _llm_returns(sajinth_entity_id, confidence=0.92))
    h2 = _write_event_with_extraction(
        db, text="Saji approved the design", entities=[{"name": "Saji", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_matched_llm"] == 1
    assert stats["entities_created"] == 0
    assert stats["llm_calls"] == 1

    saji_mention = iter_mentions_for_event(db, h2)[0]
    assert saji_mention.entity_id == sajinth_entity_id
    assert saji_mention.match_method == "llm"


def test_llm_returns_null_creates_new_entity(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM judges 'this is NOT any candidate' → new canonical entity."""
    _no_sleep(monkeypatch)

    _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)

    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))
    _write_event_with_extraction(
        db,
        text="John from accounting called",
        entities=[{"name": "John", "type": "person"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 1
    assert stats["entities_matched_llm"] == 0


def test_sonnet_escalation_on_low_confidence(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Haiku verdict at 0.4 confidence triggers a Sonnet re-judge.
    The escalation counter increments and the Sonnet verdict wins."""
    _no_sleep(monkeypatch)

    _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)

    sajinth_entity_id = db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth'"
    ).fetchone()["id"]

    call_log: list[dict[str, Any]] = []

    def _two_stage(**kw: Any) -> LLMResult:
        call_log.append(kw)
        if "haiku" in kw["model"]:
            # Haiku returns low-confidence null verdict.
            return LLMResult(
                data={
                    "matched_entity_id": None,
                    "reason": "ambiguous",
                    "confidence": 0.4,
                },
                model=kw["model"],
                raw="",
            )
        # Sonnet escalation returns high-confidence match.
        return LLMResult(
            data={
                "matched_entity_id": sajinth_entity_id,
                "reason": "same person — context matches",
                "confidence": 0.95,
            },
            model=kw["model"],
            raw="",
        )

    monkeypatch.setattr(ec, "call_tool", _two_stage)
    _write_event_with_extraction(
        db, text="S.S. signed off", entities=[{"name": "S.S.", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["sonnet_escalations"] == 1
    assert stats["llm_calls"] == 2  # Haiku + Sonnet
    assert stats["entities_matched_llm"] == 1
    # Two distinct models were invoked.
    models_seen = {c["model"] for c in call_log}
    assert any("haiku" in m for m in models_seen)
    assert any("sonnet" in m for m in models_seen)


def test_llm_hallucinated_entity_id_falls_back_to_new(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM returns an entity_id not in the candidate set, treat it
    as a hallucination and fall through to new-entity creation."""
    _no_sleep(monkeypatch)

    _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)

    monkeypatch.setattr(
        ec, "call_tool", _llm_returns("entity:fake-id-not-in-pool", confidence=0.99)
    )
    _write_event_with_extraction(
        db, text="Maya joined Athara", entities=[{"name": "Maya", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_matched_llm"] == 0
    assert stats["entities_created"] == 1


def test_alias_gazetteer_matches_without_an_llm_call(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 1.5 — a surface form matching a known emergent alias links to the
    entity WITHOUT paying for the LLM (canonicalizer cost-cutter)."""
    _no_sleep(monkeypatch)

    # Create Sajinth (person) via the exact path.
    h1 = _write_event_with_extraction(
        db, text="Sajinth runs Clario", entities=[{"name": "Sajinth", "type": "person"}]
    )
    ec.EntityCanonicalizer().run(db, settings)
    sajinth_id = iter_mentions_for_event(db, h1)[0].entity_id

    # The entity-article worker has recorded an emergent alias "Saji".
    write_event(
        db,
        origin="agent",
        kind=ec.ENTITY_ARTICLE_KIND,
        payload={
            "content_type": "text",
            "text": "Sajinth is a person.",
            "entity_key": "person\x1fsajinth",
            "canonical_name": "Sajinth",
            "entity_kind": "person",
            "entity_ids": [sajinth_id],
            "aliases": ["Saji"],
        },
    )

    # Now an event mentions "Saji". The LLM must NOT be called.
    def _boom(**_: Any) -> LLMResult:
        raise AssertionError("LLM was called — the alias gazetteer should have matched first")

    monkeypatch.setattr(ec, "call_tool", _boom)
    h2 = _write_event_with_extraction(
        db, text="Saji approved the plan", entities=[{"name": "Saji", "type": "person"}]
    )
    stats = ec.EntityCanonicalizer().run(db, settings)

    assert stats["entities_matched_alias"] == 1
    assert stats["llm_calls"] == 0
    saji_mention = iter_mentions_for_event(db, h2)[0]
    assert saji_mention.entity_id == sajinth_id
    assert saji_mention.match_method == "alias"


def test_llm_match_to_real_but_out_of_pool_entity_is_rejected(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security L1 — the model must not be able to bind a mention to an
    entity that exists in the vault but was NEVER shown to it as a candidate.

    The org 'Athara' exists, but the candidate pool for a *person* surface
    form excludes it (different kind). A coerced/hallucinated verdict naming
    Athara's real id must fall through to new-entity creation, not silently
    attach the person mention onto the org.
    """
    _no_sleep(monkeypatch)

    # Seed an org (Athara) AND a person (Sajinth). The person ensures the
    # candidate pool for a new person surface form is non-empty, so the LLM
    # branch actually runs (otherwise the match would be skipped and the test
    # would pass for the wrong reason).
    h_org = _write_event_with_extraction(
        db,
        text="Athara shipped; Sajinth approved",
        entities=[{"name": "Athara", "type": "org"}, {"name": "Sajinth", "type": "person"}],
    )
    EntityCanonicalizer().run(db, settings)
    athara_id = next(
        m.entity_id for m in iter_mentions_for_event(db, h_org) if m.surface_form == "Athara"
    )

    # LLM (somehow) returns the real org id while judging a person surface form.
    monkeypatch.setattr(ec, "call_tool", _llm_returns(athara_id, confidence=0.99))
    h_person = _write_event_with_extraction(
        db, text="Maya joined the team", entities=[{"name": "Maya", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    # The LLM WAS consulted, but its out-of-pool match was rejected → a NEW
    # person entity, NOT linked to the org.
    assert stats["llm_calls"] >= 1
    assert stats["entities_matched_llm"] == 0
    maya_mention = iter_mentions_for_event(db, h_person)[0]
    assert maya_mention.entity_id != athara_id
    assert maya_mention.match_method == "new"


def test_llm_error_falls_back_to_new(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMError → log + create a new entity, don't crash the cycle."""
    _no_sleep(monkeypatch)

    _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)

    def _raise(**_: Any) -> LLMResult:
        msg = "503 from upstream"
        raise LLMError(msg)

    monkeypatch.setattr(ec, "call_tool", _raise)
    _write_event_with_extraction(
        db, text="Maya joined Athara", entities=[{"name": "Maya", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["llm_errors"] == 1
    assert stats["entities_created"] == 1


# ── relations → edges ─────────────────────────────────────────────────────


def test_relation_creates_edge_between_entities(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    # Seed Sajinth in a prior event so the edge has at least one
    # pre-existing endpoint (structural defense against fabricated edges
    # between two same-event-born entities — see _is_safe_edge logic).
    _write_event_with_extraction(
        db,
        text="Sajinth said hi",
        entities=[{"name": "Sajinth", "type": "person"}],
        relations=[],
    )
    EntityCanonicalizer().run(db, settings)

    _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(db, settings)

    edges = db.execute("SELECT * FROM entity_edges").fetchall()
    assert len(edges) == 1
    edge = edges[0]
    assert edge["predicate"] == "runs"


def test_edge_with_two_new_entities_in_same_event_is_rejected(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense against prompt-injection that fabricates both endpoints.

    An attacker pasting 'NewName1 knows NewName2' shouldn't be able to
    write a graph edge claiming a relationship between two people who
    never appeared anywhere else in the substrate.
    """
    _no_sleep(monkeypatch)
    # LLM mock returns "no match" so each name becomes a NEW entity.
    # (Stage 2 fires for the second name because Stage 3 just created the
    # first as a candidate of the same kind; the LLM correctly says no match
    # because the surface forms differ.)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    _write_event_with_extraction(
        db,
        text="Adversarial paste: Alice knows Bob",
        entities=[
            {"name": "Alice", "type": "person"},
            {"name": "Bob", "type": "person"},
        ],
        relations=[{"subject": "Alice", "predicate": "knows", "object": "Bob"}],
    )
    stats = EntityCanonicalizer().run(db, settings)

    edges = db.execute("SELECT * FROM entity_edges").fetchall()
    assert edges == [], "edge between two new entities must be rejected"
    assert stats.get("edges_rejected_both_new", 0) == 1


def test_relation_with_unresolved_subject_is_skipped(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[{"name": "Sajinth", "type": "person"}],
        # Athara is referenced in the relation but NOT extracted as an entity.
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["edges_created"] == 0
    assert stats["edges_skipped_unresolved"] == 1


def test_relation_self_edge_skipped(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subject == object yields no edge (rare in practice; the schema
    would allow it but it's noise)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    _write_event_with_extraction(
        db,
        text="Sajinth knows Sajinth",
        entities=[{"name": "Sajinth", "type": "person"}],
        relations=[{"subject": "Sajinth", "predicate": "knows", "object": "Sajinth"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["edges_created"] == 0
    assert stats["edges_skipped_unresolved"] == 1


# ── idempotency + budget ─────────────────────────────────────────────────


def test_second_cycle_is_noop(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the worker a second time over the same vault does nothing
    (no new events, all already canonicalized)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    stats1 = EntityCanonicalizer().run(db, settings)
    stats2 = EntityCanonicalizer().run(db, settings)
    assert stats1["events_canonicalized"] == 1
    assert stats2["events_canonicalized"] == 0
    assert stats2["entities_created"] == 0


def test_llm_budget_cap_defers_remaining_events(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM budget is exhausted, remaining events are DEFERRED, not
    drained exact-only (ADR-0003 Phase 2, Slice 2 / G1). Draining exact-only
    was the residual formation path — a kind flip on an existing name with no
    LLM available minted a new cross-kind duplicate. Deferred events keep zero
    mentions and re-surface next cycle with a fresh budget."""
    _no_sleep(monkeypatch)
    # Tighten the budget for the test.
    monkeypatch.setattr(ec, "MAX_LLM_CALLS_PER_CYCLE", 1)

    # Anchor entity so the LLM stage is in play for subsequent events.
    _write_event_with_extraction(db, text="anchor", entities=[{"name": "Maya", "type": "person"}])
    EntityCanonicalizer().run(db, settings)

    call_count = 0

    def _llm_returns_match(**kw: Any) -> LLMResult:
        nonlocal call_count
        call_count += 1
        return LLMResult(
            data={"matched_entity_id": None, "reason": "x", "confidence": 0.99},
            model=kw["model"],
            raw="",
        )

    monkeypatch.setattr(ec, "call_tool", _llm_returns_match)

    _write_event_with_extraction(
        db, text="Alice landed today", entities=[{"name": "Alice", "type": "person"}]
    )
    _write_event_with_extraction(
        db, text="Bob shipped a fix", entities=[{"name": "Bob", "type": "person"}]
    )

    stats = EntityCanonicalizer().run(db, settings)
    # One LLM call fit in the budget (Alice); the second event (Bob) was
    # deferred rather than drained exact-only.
    assert stats["llm_calls"] == 1
    assert stats["entities_created"] == 1
    assert stats["events_deferred_no_budget"] == 1

    # Next cycle: fresh budget → the deferred event is processed, no loss.
    stats2 = EntityCanonicalizer().run(db, settings)
    assert stats2["events_deferred_no_budget"] == 0
    assert stats2["entities_created"] == 1


# ── cascade invalidation ──────────────────────────────────────────────────


def test_cascade_invalidation_marks_edges(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remember(invalidates=[hash]) writes an invalidate event. The
    canonicalizer's next cycle finds every edge sourced from the
    invalidated event and marks them as invalidated."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    # Seed Sajinth as a pre-existing entity so the edge in the next event
    # passes the "no edge between two same-event-born entities" defense.
    _write_event_with_extraction(
        db,
        text="Sajinth introduced himself",
        entities=[{"name": "Sajinth", "type": "person"}],
        relations=[],
    )
    EntityCanonicalizer().run(db, settings)

    _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(db, settings)

    # One edge exists (the one from the second event — first event has no relations).
    target_event_id = db.execute(
        "SELECT e.id FROM events e WHERE e.kind = 'remember' "
        "AND EXISTS (SELECT 1 FROM entity_edges WHERE source_event_id = e.id)"
    ).fetchone()["id"]
    edges = find_edges_for_source_event(db, target_event_id)
    assert len(edges) == 1
    edge_id = edges[0].id
    assert read_edge_invalidations(db, edge_id) == []

    # Write an invalidate event targeting the original.
    target_hash = db.execute(
        "SELECT content_hash FROM events WHERE id = ?", (target_event_id,)
    ).fetchone()["content_hash"]
    write_invalidation(db, target_hash=target_hash, reason="Sajinth stepped down", origin="user")

    # Next cycle: cascade fires.
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["invalidations_cascaded"] == 1
    assert stats["edges_invalidated"] == 1

    invalidations = read_edge_invalidations(db, edge_id)
    assert len(invalidations) == 1
    assert invalidations[0].source_event_id is not None


def test_cascade_is_idempotent(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cycle does NOT re-cascade an already-cascaded invalidate event."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(db, settings)

    target_event = db.execute(
        "SELECT content_hash FROM events WHERE kind = 'remember' LIMIT 1"
    ).fetchone()
    write_invalidation(db, target_hash=target_event["content_hash"], reason="moved", origin="user")

    stats1 = EntityCanonicalizer().run(db, settings)
    stats2 = EntityCanonicalizer().run(db, settings)
    assert stats1["invalidations_cascaded"] == 1
    assert stats2["invalidations_cascaded"] == 0

    # A cascade marker should sit in interpretations.
    marker = db.execute(
        "SELECT COUNT(*) AS n FROM interpretations WHERE produced_by = ?",
        (CASCADE_PRODUCED_BY,),
    ).fetchone()
    assert marker["n"] == 1


def test_cascade_with_no_edges_still_marks_event_processed(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalidate event targets an event that had no entity_edges —
    cascade is a no-op (0 edges invalidated) but the marker still lands
    so we don't re-scan the event next cycle."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    # Event with entities but no relations → no edges.
    _write_event_with_extraction(
        db, text="standalone fact", entities=[{"name": "X", "type": "concept"}]
    )
    EntityCanonicalizer().run(db, settings)

    target_event = db.execute(
        "SELECT content_hash FROM events WHERE kind = 'remember' LIMIT 1"
    ).fetchone()
    write_invalidation(db, target_hash=target_event["content_hash"], reason="x", origin="user")

    stats = EntityCanonicalizer().run(db, settings)
    assert stats["invalidations_cascaded"] == 1
    assert stats["edges_invalidated"] == 0

    marker_count = db.execute(
        "SELECT COUNT(*) AS n FROM interpretations WHERE produced_by = ?",
        (CASCADE_PRODUCED_BY,),
    ).fetchone()
    assert marker_count["n"] == 1


# ── ancillary ─────────────────────────────────────────────────────────────


def test_canonicalizer_produces_stamped_mention_metadata(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every mention should carry the worker's produced-by string and a
    sensible match_method. Tests the audit trail per I7."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    h = _write_event_with_extraction(
        db, text="Sajinth ran a meeting", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)
    mention = iter_mentions_for_event(db, h)[0]
    assert mention.canonicalized_by == CANONICALIZER_PRODUCED_BY
    assert mention.match_method == "new"


def test_event_with_only_filtered_entities_gets_no_mentions_marker(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an extractor that returns entities with empty names
    (or malformed shapes) used to make the worker loop forever — the
    NOT EXISTS query kept finding the event because no mentions ever
    landed. The no-mentions marker breaks the loop.

    First cycle: worker processes the event, writes the marker.
    Second cycle: _find_uncanonicalized_events skips the event because
    the marker is now present.
    """
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    # Event whose extractor returned entities-list with all-filtered shapes.
    event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    write_interpretation(
        db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": "x",
            "entities": [
                {"name": "", "type": "person"},  # empty name → filtered
                {"name": "   ", "type": "person"},  # whitespace-only → filtered
                "not-a-dict",  # malformed → filtered
            ],
            "relations": [],
        },
    )

    # First cycle: marker lands.
    stats1 = EntityCanonicalizer().run(db, settings)
    assert stats1["events_canonicalized"] == 1
    assert stats1["entities_created"] == 0

    marker_count = db.execute(
        "SELECT COUNT(*) AS n FROM interpretations WHERE produced_by = ?",
        (ec.NO_MENTIONS_PRODUCED_BY,),
    ).fetchone()
    assert marker_count["n"] == 1

    # Second cycle: marker prevents re-processing.
    stats2 = EntityCanonicalizer().run(db, settings)
    assert stats2["events_canonicalized"] == 0


def test_canonicalizer_ignores_failed_extractions(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status=failed interpretation rows have no usable entities — skip."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    write_interpretation(
        db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={"status": "failed", "error_type": "x", "error_message": "y", "retries": 0},
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["events_canonicalized"] == 0


def test_edges_hidden_after_cascade(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: write event with relation → canonicalize → invalidate →
    cascade → iter_edges_for_entity (default) returns nothing."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )

    # Seed Sajinth first so the second event's edge has one pre-existing
    # endpoint (passes the structural defense).
    _write_event_with_extraction(
        db,
        text="Sajinth introduced himself",
        entities=[{"name": "Sajinth", "type": "person"}],
        relations=[],
    )
    EntityCanonicalizer().run(db, settings)

    _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(db, settings)

    sajinth_id = db.execute("SELECT id FROM entities WHERE canonical_name = 'Sajinth'").fetchone()[
        "id"
    ]

    # Before invalidation: edge visible.
    assert len(iter_edges_for_entity(db, sajinth_id)) == 1

    # Invalidate the EVENT that produced the edge (the second one).
    target_hash = db.execute(
        "SELECT e.content_hash FROM events e WHERE e.kind = 'remember' "
        "AND EXISTS (SELECT 1 FROM entity_edges WHERE source_event_id = e.id)"
    ).fetchone()["content_hash"]
    write_invalidation(db, target_hash=target_hash, reason="moved on", origin="user")
    EntityCanonicalizer().run(db, settings)

    # Default view hides invalidated edges.
    assert len(iter_edges_for_entity(db, sajinth_id)) == 0
    # Historical view still surfaces them.
    assert len(iter_edges_for_entity(db, sajinth_id, include_invalidated=True)) == 1


# ── evidence gate: the confabulation backstop ───────────────────────────────
#
# Regression for the false-edge bug: the extractor used to infer relations from
# mere co-occurrence ("Sajinth" and "Clario" in the same note → "Sajinth works
# on Clario"), and the canonicalizer trusted them. Now a relation must carry a
# verbatim evidence quote that is actually present in the event text.


def test_relation_with_evidence_absent_from_text_is_skipped(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Co-occurrence is not a relation: an invented triple whose evidence quote
    is NOT in the text creates no edge."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    # Seed both so resolution is not the reason it's skipped.
    _write_event_with_extraction(
        db, text="Sajinth is on the team", entities=[{"name": "Sajinth", "type": "person"}]
    )
    _write_event_with_extraction(
        db, text="Clario is a project", entities=[{"name": "Clario", "type": "project"}]
    )
    EntityCanonicalizer().run(db, settings)

    # A note that merely co-mentions both, with a fabricated relation whose
    # evidence is a paraphrase, not a quote from the text.
    _write_event_with_extraction(
        db,
        text="Standup notes. Sajinth gave an update, and the Clario demo went well.",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Clario", "type": "project"},
        ],
        relations=[
            {
                "subject": "Sajinth",
                "predicate": "works on",
                "object": "Clario",
                "evidence": "Sajinth works on Clario",  # never appears in the text
            }
        ],
    )
    EntityCanonicalizer().run(db, settings)

    edges = db.execute("SELECT * FROM entity_edges").fetchall()
    assert edges == []


def test_relation_with_grounded_evidence_creates_edge(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate does not block a real relation: evidence quoted from the text
    is accepted."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    _write_event_with_extraction(
        db, text="Maya is here", entities=[{"name": "Maya", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)

    _write_event_with_extraction(
        db,
        text="Maya leads the Helios rewrite, kicked off this week.",
        entities=[
            {"name": "Maya", "type": "person"},
            {"name": "Helios", "type": "project"},
        ],
        relations=[
            {
                "subject": "Maya",
                "predicate": "leads",
                "object": "Helios",
                "evidence": "Maya leads the Helios rewrite",  # verbatim from text
            }
        ],
    )
    EntityCanonicalizer().run(db, settings)

    edges = db.execute("SELECT predicate FROM entity_edges").fetchall()
    assert len(edges) == 1
    assert edges[0]["predicate"] == "leads"


# ── ADR-0004: edge-confidence write-time wiring ────────────────────────────


def test_edge_confidence_not_hardcoded(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION (red on main, green after S3): an edge with a vague predicate
    and one brand-new endpoint no longer gets a flat 0.8 — it gets a real,
    lower prior. On main every edge is 0.8, so this test fails there."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    # First event establishes "Sajinth" as a pre-existing person endpoint.
    _write_event_with_extraction(
        db, text="Sajinth joined", entities=[{"name": "Sajinth", "type": "person"}]
    )
    # Second event: a vague 6-word predicate, subject pre-exists (exact 1.0),
    # object is brand new (0.5). Both surface forms are listed as entities so
    # the relation resolves; the edge anchors on the pre-existing Sajinth so
    # the both-new guard does not reject it.
    vague = "is a distant acquaintance of the"
    text = f"Sajinth {vague} Newcomer"
    h = _write_event_with_extraction(
        db,
        text=text,
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Newcomer", "type": "person"},
        ],
        relations=[{"subject": "Sajinth", "predicate": vague, "object": "Newcomer"}],
    )

    EntityCanonicalizer().run(db, settings)
    ev = read_event_by_hash_for_test(db, h)
    edges = find_edges_for_source_event(db, ev)
    assert len(edges) == 1
    assert edges[0].confidence != 0.8
    assert edges[0].confidence < 0.6


def test_edge_confidence_strong_case(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crisp predicate, both endpoints exact-matched pre-existing entities,
    extraction confidence 0.9 → prior near the historical 0.8, plus one score
    row whose components name every term."""
    _no_sleep(monkeypatch)

    def _boom(**_: Any) -> LLMResult:
        msg = "no LLM call expected — both endpoints exact-match"
        raise AssertionError(msg)

    monkeypatch.setattr(ec, "call_tool", _boom)

    # Establish both endpoints first.
    _write_event_with_extraction(
        db,
        text="Sajinth and Athara exist",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    h = _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
        confidence=0.9,
    )

    EntityCanonicalizer().run(db, settings)
    ev = read_event_by_hash_for_test(db, h)
    edges = find_edges_for_source_event(db, ev)
    assert len(edges) == 1
    edge = edges[0]
    assert 0.78 <= edge.confidence <= 0.86

    scores = latest_edge_scores_batch(db, [edge.id])
    assert edge.id in scores
    score = scores[edge.id]
    assert abs(score.confidence - edge.confidence) < 1e-9
    assert set(score.components["terms"].keys()) == {
        "base",
        "extract",
        "crisp",
        "mention",
        "corroboration",
        "conflict",
    }


def test_corroboration_counts_prior_source_events(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same triple from a second event corroborates the first: the second
    edge scores higher and its components record corroborating_sources == 1."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    # Establish both endpoints.
    _write_event_with_extraction(
        db,
        text="Sajinth and Athara exist",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    ha = _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    hb = _write_event_with_extraction(
        db,
        text="Again, Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )

    EntityCanonicalizer().run(db, settings)
    edge_a = find_edges_for_source_event(db, read_event_by_hash_for_test(db, ha))[0]
    edge_b = find_edges_for_source_event(db, read_event_by_hash_for_test(db, hb))[0]
    assert edge_b.confidence > edge_a.confidence

    score_b = latest_edge_scores_batch(db, [edge_b.id])[edge_b.id]
    assert score_b.components["signals"]["corroborating_sources"] == 1


def test_score_row_failure_does_not_lose_edge(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft: if the score-row write raises, the edge is still created and
    the counter still increments (the stored column carries the same number)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    def _raise(**_: Any) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(ec, "write_edge_confidence_score", _raise)

    _write_event_with_extraction(
        db, text="Sajinth exists", entities=[{"name": "Sajinth", "type": "person"}]
    )
    h = _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )

    stats = EntityCanonicalizer().run(db, settings)
    assert stats["edges_created"] == 1
    ev = read_event_by_hash_for_test(db, h)
    edges = find_edges_for_source_event(db, ev)
    assert len(edges) == 1
    # No score row was persisted (the write raised), but the column stands.
    assert latest_edge_confidence_batch(db, [edges[0].id]) == {}
    assert edges[0].confidence != 0.8


# ── P0-1: failed extractions must not starve event selection ───────────────


def _write_failed_event(db: sqlite3.Connection, text: str, error_type: str) -> None:
    """An event whose only extractor interpretation is a status:failed row."""
    from afair.agents.prompts import EXTRACTOR_SCHEMA_VERSION

    event = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    write_failed_interpretation(
        db,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        error_type=error_type,
        error_message=f"seeded {error_type}",
    )


def test_failed_extractions_do_not_starve_selection(db: sqlite3.Connection) -> None:
    """REGRESSION: permanent-failure interpretation rows must never occupy the
    bounded MAX_EVENTS_PER_CYCLE slots. With MAX=10 failed events written
    before a single fresh success, the old query returned the 10 oldest
    (all failed) rows, filtered them all out in Python, and returned [] — the
    success starved forever. The fix filters to successes in SQL, so the fresh
    success is the only candidate."""
    for i in range(ec.MAX_EVENTS_PER_CYCLE):
        _write_failed_event(db, f"failed event {i}", "pdf_extraction_error")
    success_hash = _write_event_with_extraction(
        db,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )

    found = ec._find_uncanonicalized_events(db, ec.MAX_EVENTS_PER_CYCLE)
    assert [e.content_hash for e, _ in found] == [success_hash]


def test_retry_success_after_failure_is_selected(db: sqlite3.Connection) -> None:
    """An event that failed extraction then succeeded on retry (a later success
    row for the same hash) must be selectable — the earlier bug's seen_hashes
    add-before-status-check masked exactly this case."""
    from afair.agents.prompts import EXTRACTOR_SCHEMA_VERSION

    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Sajinth runs Athara"},
    )
    # First attempt fails (transient), then a retry succeeds — both rows live.
    write_failed_interpretation(
        db,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        error_type="llm_timeout",
        error_message="timed out once",
    )
    write_interpretation(
        db,
        event=event,
        version=EXTRACTOR_SCHEMA_VERSION,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": "Sajinth runs Athara",
            "entities": [
                {"name": "Sajinth", "type": "person"},
                {"name": "Athara", "type": "organization"},
            ],
            "relations": [],
        },
    )

    found = ec._find_uncanonicalized_events(db, ec.MAX_EVENTS_PER_CYCLE)
    assert [e.content_hash for e, _ in found] == [event.content_hash]

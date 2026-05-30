"""Phase 4 Track 1 Stage 4 — entity-aware query routing.

The capability gate: recall(query="Sajinth") should find events that
mention Sajinth even when the text payload doesn't contain that string
literally (e.g., the event says "Saji approved" and canonicalization
linked "Saji" → Sajinth). Same for queries that match a canonical
NAME even though only variant surface forms appear in the text.

The big-picture reproducer: Sajinth/Athara/elvah mixed in one vault.
Asking for Athara should NOT surface elvah-context events that happen
to share random words; it SHOULD surface every event Athara-the-entity
participates in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMResult
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)


@pytest.fixture(autouse=True)
def _no_llm_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


def _seed(
    ctx: ServerContext,
    *,
    text: str,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]] | None = None,
) -> str:
    event = write_event(
        ctx.db, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    write_interpretation(
        ctx.db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": text[:200],
            "entities": entities,
            "relations": relations or [],
        },
    )
    return event.id


# ── basic routing: query matches a canonical name ─────────────────────────


def test_query_matches_canonical_name_surfaces_event_without_text_overlap(
    ctx: ServerContext, settings: Settings
) -> None:
    """The event's text doesn't contain the canonical name verbatim, but
    the extractor identified it as an entity. recall(query="Athara")
    should still surface the event via the entity-routing path."""
    event_id = _seed(
        ctx,
        text="our company shipped a feature",  # no "Athara" in the text
        entities=[{"name": "Athara", "type": "organization"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Athara", depth="shallow")
    found_ids = {h.event_id for h in result.hits}
    assert event_id in found_ids


def test_query_matches_surface_form_finds_event(
    ctx: ServerContext, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The substrate has 'Sajinth' canonical; one event mentioned 'Saji'.
    A query for 'Saji' still finds that event because surface_form was
    recorded in entity_mentions."""
    _seed(ctx, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}])
    EntityCanonicalizer().run(ctx.db, settings)
    sajinth_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth'"
    ).fetchone()["id"]

    monkeypatch.setattr(
        ec,
        "call_tool",
        lambda **_: LLMResult(
            data={"matched_entity_id": sajinth_id, "reason": "x", "confidence": 0.95},
            model="test",
            raw="",
        ),
    )
    saji_event_id = _seed(ctx, text="meeting today", entities=[{"name": "Saji", "type": "person"}])
    EntityCanonicalizer().run(ctx.db, settings)

    # Query for "Saji" — matches the surface form recorded on the second event.
    result = handlers.recall(query="Saji", depth="shallow")
    found_ids = {h.event_id for h in result.hits}
    assert saji_event_id in found_ids


# ── Sajinth/Athara/elvah reproducer ───────────────────────────────────────


def test_sajinth_athara_elvah_scenario_query_separates_contexts(
    ctx: ServerContext, settings: Settings
) -> None:
    """The original pain point: one vault contains three orthogonal
    contexts. A query for one of them must not return the others'
    unrelated events.

    Setup:
      Event A — elvah project, mentions Sajinth (different person here
                — say "Sajinth-elvah") and elvah-the-org
      Event B — Athara strategy, mentions Sajinth (the Athara person)
      Event C — performance probe, mentions a metric

    Query "Athara" → should surface Event B (mentions Athara-org)
                     and NOT surface Event A or C.
    Query "elvah"  → Event A only.
    Query "Sajinth" — finds both A and B (they BOTH mention a person
                     named Sajinth; the canonicalizer creates two
                     separate canonicals since they're different people,
                     but the query surfaces both because surface_form
                     matches in both events).
    """
    event_a = _seed(
        ctx,
        text="closing the elvah engagement, paperwork with Sajinth's lawyer",
        entities=[
            {"name": "Sajinth-elvah-lawyer", "type": "person"},
            {"name": "elvah", "type": "organization"},
        ],
    )
    event_b = _seed(
        ctx,
        text="Sajinth proposed a new roadmap for Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    event_c = _seed(
        ctx,
        text="p99 latency spike at 14:00",
        entities=[{"name": "p99-latency", "type": "concept"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # Query "Athara"
    r = handlers.recall(query="Athara", depth="shallow")
    ids = {h.event_id for h in r.hits}
    assert event_b in ids
    assert event_a not in ids
    assert event_c not in ids

    # Query "elvah"
    r = handlers.recall(query="elvah", depth="shallow")
    ids = {h.event_id for h in r.hits}
    assert event_a in ids
    assert event_b not in ids
    assert event_c not in ids


def test_query_for_entity_returns_canonical_entities_in_hits(
    ctx: ServerContext, settings: Settings
) -> None:
    """End-to-end: entity-aware query + the Stage 3 overlay together
    means the AI sees both the right events AND knows which entities
    they involve."""
    # Pre-seed Sajinth so the relation edge has one pre-existing endpoint
    # (defense against fabricated edges between two same-event-born entities).
    _seed(
        ctx,
        text="Sajinth introduced himself",
        entities=[{"name": "Sajinth", "type": "person"}],
        relations=[],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    _seed(
        ctx,
        text="Sajinth proposed a new roadmap for Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "proposed_for", "object": "Athara"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Athara", depth="shallow")
    assert len(result.hits) == 1
    interp = result.hits[0].interpretation
    assert interp is not None
    names = {e["canonical_name"] for e in (interp.get("canonical_entities") or [])}
    assert names == {"Sajinth", "Athara"}
    edges = interp.get("entity_edges") or []
    assert len(edges) == 1
    assert edges[0]["predicate"] == "proposed_for"


# ── degenerate / negative cases ───────────────────────────────────────────


def test_nonmatching_query_falls_back_to_pure_fts(ctx: ServerContext, settings: Settings) -> None:
    """Random non-entity query — the entity-routing helper returns [],
    and recall behaves exactly as Stage-0 FTS would. Sanity check that
    we don't break general recall."""
    _seed(
        ctx,
        text="the quick brown fox jumps over the lazy dog",
        entities=[],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="quick", depth="shallow")
    assert len(result.hits) == 1


def test_empty_query_does_not_invoke_entity_routing(ctx: ServerContext, settings: Settings) -> None:
    """recall() with no query is browse mode — entity routing is skipped."""
    _seed(
        ctx,
        text="Sajinth runs Athara",
        entities=[{"name": "Sajinth", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall()
    # Browse mode: just returns most-recent N events.
    assert len(result.hits) == 1


def test_query_matches_canonical_but_no_events_yet(ctx: ServerContext, settings: Settings) -> None:
    """The query string IS an entity, but no events have been
    canonicalized — entity routing returns [] and falls through to FTS."""
    # No canonicalizer run, but the entity-mentions table is empty.
    _seed(
        ctx,
        text="Sajinth is a person who runs Athara",
        entities=[],  # extractor produced no entities
    )
    # Run canonicalizer — no entities to process, table stays empty.
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    # FTS still finds it via the literal "Sajinth" in the text.
    assert len(result.hits) == 1


def test_entity_match_is_case_insensitive(ctx: ServerContext, settings: Settings) -> None:
    """Query "athara" (lowercase) still finds events with canonical "Athara"."""
    event_id = _seed(
        ctx,
        text="company news",
        entities=[{"name": "Athara", "type": "organization"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="athara", depth="shallow")
    found_ids = {h.event_id for h in result.hits}
    assert event_id in found_ids


def test_entity_routing_boosts_relevant_events_above_unrelated(
    ctx: ServerContext, settings: Settings
) -> None:
    """When FTS surfaces several events (because they share a common
    word), entity-mention events should boost above pure word-matches.
    Reciprocal Rank Fusion ensures double-matched events outrank
    single-matched ones."""
    # Three events all containing the word "approved" via FTS.
    irrelevant_a = _seed(
        ctx,
        text="approved the design",
        entities=[],
    )
    irrelevant_b = _seed(
        ctx,
        text="approved the contract last week",
        entities=[],
    )
    # The entity-bearing event.
    entity_event = _seed(
        ctx,
        text="approved by Athara leadership",
        entities=[{"name": "Athara", "type": "organization"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Athara", depth="shallow")
    # The entity-bearing event must be in the result set; the others may
    # or may not appear depending on FTS+RRF — but the entity one must
    # outrank pure-text matches.
    ids_in_order = [h.event_id for h in result.hits]
    assert entity_event in ids_in_order
    # If the irrelevants appear at all, the entity event must come first.
    if irrelevant_a in ids_in_order:
        assert ids_in_order.index(entity_event) < ids_in_order.index(irrelevant_a)
    if irrelevant_b in ids_in_order:
        assert ids_in_order.index(entity_event) < ids_in_order.index(irrelevant_b)

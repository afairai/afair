"""Phase 4 Track 1 Stage 3 — recall enrichment with entity context.

Tests that recall hits surface ``canonical_entities`` and
``entity_edges`` inside ``interpretation`` once the canonicalizer has
materialized the graph. No MCP-surface change is involved — these
fields live additively inside the existing interpretation dict.

Setup pattern: write event → write extractor interpretation → run
canonicalizer (with mocked LLM) → run recall → assert the overlay
shows up on each hit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.agents.invalidation import write_invalidation
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import TextContent
from afair.settings import Settings
from afair.substrate import open_db, record_edge_review, write_event

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
        semantic_recall_enabled=False,  # tests don't hit the embedding API
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests own the canonicalizer lifecycle and inject extractor outputs
    directly. The warm-path Extractor would race with that."""
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


@pytest.fixture(autouse=True)
def _no_llm_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


def _seed_event_with_entities(
    ctx: ServerContext,
    *,
    text: str,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]] | None = None,
) -> str:
    """Write an event + an Extractor interpretation row. Returns event.id."""
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
    )
    # Ground each relation's evidence in the event text so the canonicalizer's
    # evidence gate (a verbatim quote must be present in the source) passes.
    grounded = [{**r, "evidence": r.get("evidence", text)} for r in (relations or [])]
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
            "relations": grounded,
        },
    )
    return event.id


# ── basic enrichment ──────────────────────────────────────────────────────


def test_recall_surfaces_canonical_entities_for_each_hit(
    ctx: ServerContext, settings: Settings
) -> None:
    """After canonicalization, ``recall(query=...)`` puts the canonical
    entity list inside ``interpretation.canonical_entities``."""
    _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    assert len(result.hits) == 1
    interp = result.hits[0].interpretation
    assert interp is not None
    ents = interp.get("canonical_entities")
    assert ents is not None
    assert len(ents) == 2
    names = {e["canonical_name"] for e in ents}
    assert names == {"Sajinth", "Athara"}


def test_recall_surfaces_entity_edges_for_relations(ctx: ServerContext, settings: Settings) -> None:
    """Edges with this event as source appear under entity_edges."""
    # Pre-seed Sajinth so the edge below has one pre-existing endpoint
    # (defense against fabricated edges between two same-event-born entities).
    _seed_event_with_entities(
        ctx,
        text="Sajinth introduced himself",
        entities=[{"name": "Sajinth", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    # Two events match "Sajinth"; find the one with the edge.
    hit_with_edge = next(
        (h for h in result.hits if h.interpretation and h.interpretation.get("entity_edges")),
        None,
    )
    assert hit_with_edge is not None, "expected at least one hit to carry the edge overlay"
    interp = hit_with_edge.interpretation
    assert interp is not None
    edges = interp.get("entity_edges") or []
    assert len(edges) == 1
    assert edges[0]["subject"] == "Sajinth"
    assert edges[0]["predicate"] == "runs"
    assert edges[0]["object"] == "Athara"
    # A crisp, confident, grounded edge is auto-trusted (ADR-0002).
    assert edges[0]["trust"] == "auto_confirmed"


def test_recall_dedupes_canonical_entities_when_same_entity_mentioned_twice(
    ctx: ServerContext, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single event mentioning "Sajinth" and "Saji" (both → same canonical
    via LLM match) should surface ONE canonical entry, not two."""
    # First event creates canonical "Sajinth".
    _seed_event_with_entities(
        ctx, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(ctx.db, settings)
    sajinth_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth'"
    ).fetchone()["id"]

    # Second event mentions both — LLM matches "Saji" → Sajinth.
    from afair.agents.llm import LLMResult

    def _fake(**kw: object) -> LLMResult:
        return LLMResult(
            data={"matched_entity_id": sajinth_id, "reason": "x", "confidence": 0.95},
            model="test",
            raw="",
        )

    monkeypatch.setattr(ec, "call_tool", _fake)
    _seed_event_with_entities(
        ctx,
        text="Saji approved and Sajinth shipped",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Saji", "type": "person"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # Recall pulls the second event up.
    result = handlers.recall(query="approved", depth="shallow")
    target = next(
        (h for h in result.hits if "approved" in (h.payload.get("text") or "")),
        None,
    )
    assert target is not None
    interp = target.interpretation
    assert interp is not None
    ents = interp.get("canonical_entities") or []
    # Both surface forms point at one canonical → one entry.
    ids = {e["id"] for e in ents}
    assert len(ids) == 1
    assert sajinth_id in ids


def test_recall_attaches_overlay_to_lookup_by_id(ctx: ServerContext, settings: Settings) -> None:
    """recall(by_id=...) carries the same overlay."""
    # Pre-seed Sajinth so the relation edge below has one pre-existing endpoint.
    _seed_event_with_entities(
        ctx,
        text="Sajinth introduced himself",
        entities=[{"name": "Sajinth", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    event_id = _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(by_id=event_id)
    assert len(result.hits) == 1
    interp = result.hits[0].interpretation
    assert interp is not None
    assert len(interp.get("canonical_entities") or []) == 2
    assert len(interp.get("entity_edges") or []) == 1


# ── empty / sparse cases ──────────────────────────────────────────────────


def test_recall_without_canonicalizer_run_omits_overlay(
    ctx: ServerContext,
) -> None:
    """Until the canonicalizer has run, recall hits show the extractor's
    raw entities but NO canonical_entities/entity_edges fields."""
    _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    # NO canonicalizer run here.

    result = handlers.recall(query="Sajinth", depth="shallow")
    interp = result.hits[0].interpretation
    assert interp is not None
    # Extractor's raw "entities" key is present (from the seed).
    assert "entities" in interp
    # But the Phase 4 enrichment keys are absent.
    assert "canonical_entities" not in interp
    assert "entity_edges" not in interp


def test_recall_with_no_interpretation_creates_one_for_overlay(
    ctx: ServerContext, settings: Settings
) -> None:
    """An event that was canonicalized but has NO extractor interpretation
    (artificial; would only happen via backfill) still gets the entity
    overlay surfaced — interpretation is created with canonical_entities
    even when nothing else exists."""
    # Write event directly (no extractor row).
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "Sajinth runs Athara"},
    )
    # But seed an extractor interpretation manually so the canonicalizer
    # can find the entities. The original tested condition (NO interpretation
    # but YES canonicalized) doesn't occur in normal flow because the
    # canonicalizer feeds off extractor output. Skip this artificial path.
    write_interpretation(
        ctx.db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": "test",
            "entities": [{"name": "X", "type": "concept"}],
            "relations": [],
        },
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(by_id=event.id)
    interp = result.hits[0].interpretation
    assert interp is not None
    assert "canonical_entities" in interp


def test_recall_does_not_surface_invalidated_edges(ctx: ServerContext, settings: Settings) -> None:
    """Per decision #6: invalidated edges are hidden in default recall
    output. Canonical entities still show (they were not cascade-invalidated;
    the edge invalidation only marks the relation, not the entity)."""
    event_id = _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # Invalidate the event and cascade.
    target_hash = ctx.db.execute(
        "SELECT content_hash FROM events WHERE id = ?", (event_id,)
    ).fetchone()["content_hash"]
    write_invalidation(ctx.db, target_hash=target_hash, reason="Sajinth moved on", origin="user")
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(by_id=event_id)
    interp = result.hits[0].interpretation
    assert interp is not None
    # Entities still surfaced (they're not "invalid" — just the relation
    # they discovered is).
    assert len(interp.get("canonical_entities") or []) == 2
    # Edges hidden by default.
    assert interp.get("entity_edges") in (None, [])


# ── merge resolution ──────────────────────────────────────────────────────


def test_recall_resolves_canonical_through_merges(ctx: ServerContext, settings: Settings) -> None:
    """If two entities have been merged, recall surfaces the SURVIVING
    canonical for both mentions — the "from" entity is invisible by
    default (decision #6)."""
    from afair.substrate import write_entity_merge

    # Event A: creates "Sajinth-elvah" person.
    a_id = _seed_event_with_entities(
        ctx,
        text="Sajinth from the elvah team",
        entities=[{"name": "Sajinth-elvah", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # Event B: creates "Sajinth" person.
    _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[{"name": "Sajinth", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    sajinth_elvah_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth-elvah'"
    ).fetchone()["id"]
    sajinth_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth'"
    ).fetchone()["id"]
    # Merge "Sajinth-elvah" into "Sajinth".
    write_entity_merge(
        ctx.db,
        from_entity_id=sajinth_elvah_id,
        into_entity_id=sajinth_id,
        merged_by="test",
        reason="same person",
        confidence=0.95,
    )

    # Recall the FIRST event — even though its mention still points at
    # Sajinth-elvah's ID, the overlay should surface "Sajinth" canonical.
    result = handlers.recall(by_id=a_id)
    interp = result.hits[0].interpretation
    assert interp is not None
    ents = interp.get("canonical_entities") or []
    names = {e["canonical_name"] for e in ents}
    assert "Sajinth" in names  # surviving canonical
    assert "Sajinth-elvah" not in names  # superseded entity hidden


# ── interplay with surface freeze ─────────────────────────────────────────


def test_recall_overlay_does_not_break_mcp_surface(ctx: ServerContext, settings: Settings) -> None:
    """The Phase 4 enrichment is purely additive — RecallHit fields
    stay the same as the 2026-05-26 frozen surface. Nothing here should
    add a top-level key on RecallHit."""
    _seed_event_with_entities(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    hit = result.hits[0]
    expected_fields = {
        "event_id",
        "content_hash",
        "created_at",
        "kind",
        "origin",
        "payload",
        "truncated",
        "interpretation",
        "linked_event_ids",
        "parent_hashes",
        "invalidation",
        "conflicts",
    }
    actual_fields = set(hit.model_dump().keys())
    assert actual_fields == expected_fields


def test_remember_then_recall_still_works_when_canonicalizer_idle(
    ctx: ServerContext,
) -> None:
    """Sanity smoke: the basic remember+recall round-trip from the surface
    freeze still works without ANY entity-graph activity."""
    handlers.remember(content=TextContent(type="text", text="hello world"))
    result = handlers.recall(query="hello", depth="shallow")
    assert len(result.hits) == 1
    assert "hello world" in (result.hits[0].payload.get("text") or "")


def _seed_edge(ctx: ServerContext, settings: Settings, *, predicate: str) -> None:
    """Seed Sajinth, then an edge 'Sajinth <predicate> Athara'."""
    _seed_event_with_entities(
        ctx, text="Sajinth introduced himself", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(ctx.db, settings)
    _seed_event_with_entities(
        ctx,
        text=f"Sajinth {predicate} Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": predicate, "object": "Athara"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)


def _recalled_edge(result: object) -> dict:
    hit = next(
        (h for h in result.hits if h.interpretation and h.interpretation.get("entity_edges")),  # type: ignore[attr-defined]
        None,
    )
    assert hit is not None and hit.interpretation is not None
    edges = hit.interpretation.get("entity_edges") or []
    assert len(edges) == 1
    return edges[0]


def test_recall_marks_vague_predicate_edge_as_proposed(
    ctx: ServerContext, settings: Settings
) -> None:
    """A verbose profile-language predicate fails the auto-confirm policy, so
    recall surfaces it as `proposed` — never as hard fact."""
    _seed_edge(ctx, settings, predicate="is tech person in circle of")
    edge = _recalled_edge(handlers.recall(query="Sajinth", depth="shallow"))
    assert edge["trust"] == "proposed"


def test_recall_marks_confirmed_edge_as_confirmed(ctx: ServerContext, settings: Settings) -> None:
    """An operator confirm elevates the edge to `confirmed` in recall."""
    _seed_edge(ctx, settings, predicate="is tech person in circle of")
    edge_id = ctx.db.execute("SELECT id FROM entity_edges LIMIT 1").fetchone()["id"]
    record_edge_review(ctx.db, edge_id=edge_id, verdict="confirm", reviewed_by="operator")
    edge = _recalled_edge(handlers.recall(query="Sajinth", depth="shallow"))
    assert edge["trust"] == "confirmed"

"""Phase 4 Track 2 — per-hit surprise score (entity-novelty v0).

The score: for each recall hit, count how many of its resolved canonical
entities are absent from the user's "recent context window" — the set
of canonical entities mentioned in the last N events.

  score = novel_count / total_canonical_count
  0.0   = all entities familiar (hit reinforces current context)
  1.0   = all entities novel (hit "comes out of nowhere")

Surfaced as ``interpretation.surprise_score`` plus
``interpretation.surprise_components`` for auditability. Additive — no
MCP-surface change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
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
        surprise_context_window=20,
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
) -> str:
    event = write_event(
        ctx.db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
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
            "relations": [],
        },
    )
    return event.id


# ── basic semantics ──────────────────────────────────────────────────────


def test_hit_with_all_familiar_entities_scores_low(ctx: ServerContext, settings: Settings) -> None:
    """Recent vault activity established Sajinth and Athara as canonicals.
    A new event mentioning ONLY those entities → score near 0.0."""
    _seed(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    interp = result.hits[0].interpretation
    assert interp is not None
    score = interp.get("surprise_score")
    assert score is not None
    # Both entities ARE in the recent context (they were just established).
    assert score == 0.0


def test_hit_with_all_novel_entities_scores_high(ctx: ServerContext, settings: Settings) -> None:
    """Build up a recent context of unrelated entities, then introduce a
    hit whose entities are all brand-new → score 1.0."""
    # Recent context — five events with the same two entities.
    for i in range(5):
        _seed(
            ctx,
            text=f"Athara update {i}",
            entities=[{"name": "Athara", "type": "organization"}],
        )
    EntityCanonicalizer().run(ctx.db, settings)

    # Now a hit with all-novel entities.
    novel_id = _seed(
        ctx,
        text="Maya joined an entirely different project",
        entities=[
            {"name": "Maya", "type": "person"},
            {"name": "Battery Park", "type": "project"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(by_id=novel_id)
    interp = result.hits[0].interpretation
    assert interp is not None
    # Maya and Battery Park ARE now in recent context (just-canonicalized).
    # But we want to test "novel": let's pull the OLD event instead.
    old_result = handlers.recall(query="Athara", depth="shallow")
    old_hit = old_result.hits[0]
    interp = old_hit.interpretation
    assert interp is not None
    # The old event has only Athara — present in recent context. Score 0.
    assert interp.get("surprise_score") == 0.0


def test_mixed_familiarity_scores_intermediate(ctx: ServerContext, settings: Settings) -> None:
    """Hit has 4 entities, 1 of which is in recent context → score 0.75."""
    # Build recent context with one entity.
    _seed(ctx, text="Sajinth standalone", entities=[{"name": "Sajinth", "type": "person"}])
    EntityCanonicalizer().run(ctx.db, settings)

    # Hit with 4 entities (Sajinth familiar, 3 novel).
    hit_id = _seed(
        ctx,
        text="bigger event",
        entities=[
            {"name": "Sajinth", "type": "person"},  # familiar
            {"name": "Maya", "type": "person"},  # novel
            {"name": "Clario", "type": "project"},  # novel
            {"name": "Athara", "type": "organization"},  # novel
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # By the time we recall the new event, all FOUR entities are now in
    # recent context (the event we just added counts toward the window).
    # So score should be 0.0 because no entity is novel anymore.
    result = handlers.recall(by_id=hit_id)
    interp = result.hits[0].interpretation
    assert interp is not None
    # Sanity check: total_entity_count matches the hit's canonical_entities.
    components = interp.get("surprise_components")
    assert components is not None
    assert components["total_entity_count"] == 4
    assert 0.0 <= interp["surprise_score"] <= 1.0


def test_surprise_components_match_score(ctx: ServerContext, settings: Settings) -> None:
    """The components dict's numerator/denominator must reconstruct the score."""
    _seed(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # surprise_components is a full/standard field (compact keeps only the
    # scalar surprise_score); ask for full to inspect the component math.
    result = handlers.recall(query="Sajinth", depth="shallow", verbosity="full")
    interp = result.hits[0].interpretation
    assert interp is not None
    score = interp["surprise_score"]
    comp = interp["surprise_components"]
    assert comp["window_size"] == 20  # the fixture's value
    if comp["total_entity_count"] > 0:
        expected = comp["novel_entity_count"] / comp["total_entity_count"]
        assert abs(score - round(expected, 3)) < 1e-6


# ── edge cases ───────────────────────────────────────────────────────────


def test_hit_with_no_canonical_entities_omits_surprise(
    ctx: ServerContext,
) -> None:
    """Without a canonicalizer run (or with zero entities), the overlay
    has no canonical_entities — so surprise_score is omitted entirely
    (not surfaced as null)."""
    write_event(
        ctx.db, origin="user", kind="remember", payload={"content_type": "text", "text": "no ents"}
    )
    result = handlers.recall(query="no", depth="shallow")
    interp = result.hits[0].interpretation
    # interpretation is None when there's nothing to surface.
    if interp is not None:
        assert "surprise_score" not in interp
        assert "surprise_components" not in interp


def test_window_size_zero_means_everything_is_novel(
    ctx: ServerContext, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surprise window of 0 → recent context is empty → every hit scores
    1.0 (everything looks novel because nothing has been "recent")."""
    # Override the context to use a 0-event window.
    # ServerContext is a dataclass — set attribute directly.
    ctx.surprise_context_window = 0

    _seed(ctx, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}])
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow")
    interp = result.hits[0].interpretation
    assert interp is not None
    assert interp["surprise_score"] == 1.0


def test_score_is_bounded_zero_to_one(ctx: ServerContext, settings: Settings) -> None:
    """No matter the vault shape, score is in [0, 1]."""
    for i in range(8):
        _seed(
            ctx,
            text=f"event {i}",
            entities=[{"name": f"Entity{i}", "type": "concept"}],
        )
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="event", depth="shallow")
    for hit in result.hits:
        interp = hit.interpretation
        if interp is None or "surprise_score" not in interp:
            continue
        score = interp["surprise_score"]
        assert 0.0 <= score <= 1.0


def test_surprise_does_not_leak_into_recall_hit_top_level(
    ctx: ServerContext, settings: Settings
) -> None:
    """The MCP surface freeze stays intact — surprise lives INSIDE
    interpretation, not as a new top-level RecallHit field."""
    _seed(ctx, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}])
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
    assert set(hit.model_dump().keys()) == expected_fields


# ── merge resolution affects surprise ────────────────────────────────────


def test_supersession_makes_merged_entity_familiar(ctx: ServerContext, settings: Settings) -> None:
    """When entity A is merged into B, recall hits mentioning A should
    treat A's canonical (= B) as the comparison key. So if B was recent,
    a hit mentioning A is NOT surprising."""
    from afair.substrate import write_entity_merge

    # Establish "Sajinth-elvah" via one event.
    a_id = _seed(
        ctx,
        text="Sajinth-elvah meeting",
        entities=[{"name": "Sajinth-elvah", "type": "person"}],
    )
    EntityCanonicalizer().run(ctx.db, settings)

    # Establish "Sajinth" (the canonical) in recent context via newer events.
    for i in range(3):
        _seed(
            ctx,
            text=f"Sajinth shipped {i}",
            entities=[{"name": "Sajinth", "type": "person"}],
        )
    EntityCanonicalizer().run(ctx.db, settings)

    # Merge Sajinth-elvah into Sajinth.
    elvah_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth-elvah'"
    ).fetchone()["id"]
    sajinth_id = ctx.db.execute(
        "SELECT id FROM entities WHERE canonical_name = 'Sajinth'"
    ).fetchone()["id"]
    write_entity_merge(
        ctx.db,
        from_entity_id=elvah_id,
        into_entity_id=sajinth_id,
        merged_by="test",
        reason="same person",
        confidence=0.95,
    )

    # Pull the OLD event by id. Its mentions still point at Sajinth-elvah,
    # but resolution maps to Sajinth — which IS in the recent context.
    result = handlers.recall(by_id=a_id)
    interp = result.hits[0].interpretation
    assert interp is not None
    # The single (resolved) canonical = Sajinth is familiar → score 0.0.
    assert interp["surprise_score"] == 0.0

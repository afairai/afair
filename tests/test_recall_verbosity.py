"""P1-2: recall verbosity levels, limit cap, cursor pagination.

Compact is the default shape — the AI-useful minimum per hit. Standard is
today's shape minus two harmless subtractions; full is byte-identical to the
pre-P1-2 builder. Everything compact drops is re-fetchable via full or by_id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.handlers import (
    COMPACT_DEFAULT_LIMIT,
    COMPACT_MAX_EDGES,
    COMPACT_MAX_ENTITIES,
    COMPACT_MAX_LINKED_IDS,
    COMPACT_SUMMARY_CHARS,
    COMPACT_TEXT_CHARS,
    DEFAULT_RECALL_LIMIT,
    MAX_RECALL_LIMIT,
    _event_to_hit,
    _shape_interpretation,
)
from afair.substrate import open_db
from afair.substrate.events import Event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ── fixtures / builders ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _event_id: None)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db, vault_dir=tmp_path, inline_text_max_bytes=64 * 1024, semantic_recall_enabled=False
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


_ALL_VERDICTS = [
    "updates",
    "reverts",
    "evolves",
    "conflicts",
    "false_conflict",
    "confirms",
    "unrelated",
    "name_clash",
    "unsure",
]
# Compact keeps only conflicts that carry a user-facing signal.
_COMPACT_KEPT_VERDICTS = {"updates", "reverts", "conflicts", "name_clash"}


def _maximal_extraction() -> dict:
    return {
        "best_guess_kind": "decision",
        "summary": "S" * 400,
        "entities": [{"name": f"E{i}", "type": "person"} for i in range(8)],
        "salient_facts": [f"fact {i} " + "x" * 40 for i in range(12)],
        "language": "en",
        "confidence": 0.9,
        "source_attribution": "someone",
    }


def _maximal_overlay() -> dict:
    return {
        "canonical_entities": [
            {
                "id": f"entity:v2:{i:064d}",
                "canonical_name": f"Name{i}",
                "kind": "person",
                "surface_form": f"surf{i}",
                "match_method": "exact",
            }
            for i in range(8)
        ],
        "entity_edges": [
            {
                "subject": f"Name{i}",
                "predicate": "knows",
                "object": f"Name{i + 1}",
                "valid_from": None,
                "valid_to": None,
                "trust": "auto_confirmed",
                "confidence": 0.79,
            }
            for i in range(7)
        ],
        "surprise_score": 0.75,
        "surprise_components": {
            "novel_entity_count": 3,
            "total_entity_count": 4,
            "window_size": 20,
        },
        "temporal_class": "decaying",
        "temporal_relevance": 0.5,
    }


def _maximal_event() -> Event:
    return Event(
        id="01K" + "0" * 23,
        content_hash="sha256:" + "a" * 64,
        created_at="2026-01-01T00:00:00+00:00",
        origin="agent",
        kind="remember",
        payload={
            "content_type": "text",
            "text": "T" * 2000,
            "context": "C" * 1000,
            "type_hint": "decision",
        },
        schema_version=1,
    )


def _conflicts() -> list[dict]:
    return [
        {
            "with_event_id": f"ev{i}",
            "with_content_hash": f"sha256:{i:064d}",
            "verdict": v,
            "reason": "R" * 400,
            "confidence": 0.85,
        }
        for i, v in enumerate(_ALL_VERDICTS)
    ]


# ── _shape_interpretation (pure) ─────────────────────────────────────────────


def test_shape_compact_keys_and_caps() -> None:
    interp = _shape_interpretation(_maximal_extraction(), _maximal_overlay(), "compact")
    assert interp is not None
    assert set(interp) <= {
        "best_guess_kind",
        "summary",
        "canonical_entities",
        "entity_edges",
        "surprise_score",
        "temporal_class",
    }
    # dropped fields
    for dropped in (
        "entities",
        "salient_facts",
        "language",
        "confidence",
        "source_attribution",
        "surprise_components",
        "temporal_relevance",
    ):
        assert dropped not in interp
    assert len(interp["summary"]) <= COMPACT_SUMMARY_CHARS
    assert len(interp["canonical_entities"]) == COMPACT_MAX_ENTITIES
    for e in interp["canonical_entities"]:
        assert set(e) == {"id", "canonical_name", "kind"}  # surface_form/match_method dropped
    assert len(interp["entity_edges"]) == COMPACT_MAX_EDGES
    for edge in interp["entity_edges"]:
        assert "valid_from" not in edge and "valid_to" not in edge  # null → dropped
    assert interp["surprise_score"] == 0.75
    assert interp["temporal_class"] == "decaying"


def test_shape_standard_two_subtractions() -> None:
    interp = _shape_interpretation(_maximal_extraction(), _maximal_overlay(), "standard")
    assert interp is not None
    # raw entities dropped because canonical_entities present
    assert "canonical_entities" in interp
    assert "entities" not in interp
    # salient_facts KEPT in standard
    assert "salient_facts" in interp
    # null edge validity dropped
    for edge in interp["entity_edges"]:
        assert "valid_from" not in edge and "valid_to" not in edge
    # full interpretation fields still present
    assert "surprise_components" in interp
    assert "temporal_relevance" in interp


def test_shape_standard_keeps_raw_entities_without_overlay() -> None:
    interp = _shape_interpretation(_maximal_extraction(), None, "standard")
    assert interp is not None
    assert "entities" in interp  # no canonical overlay → raw entities kept
    assert "canonical_entities" not in interp


def test_shape_full_is_complete() -> None:
    interp = _shape_interpretation(_maximal_extraction(), _maximal_overlay(), "full")
    assert interp is not None
    # everything present, incl. redundant/verbose fields + null edge validity
    for key in (
        "entities",
        "salient_facts",
        "language",
        "confidence",
        "source_attribution",
        "canonical_entities",
        "entity_edges",
        "surprise_score",
        "surprise_components",
        "temporal_class",
        "temporal_relevance",
    ):
        assert key in interp
    assert len(interp["canonical_entities"]) == 8  # no cap
    assert "surface_form" in interp["canonical_entities"][0]  # not trimmed
    assert "valid_from" in interp["entity_edges"][0]  # null kept in full


def test_shape_compact_is_prefix_subset_of_full() -> None:
    full = _shape_interpretation(_maximal_extraction(), _maximal_overlay(), "full")
    compact = _shape_interpretation(_maximal_extraction(), _maximal_overlay(), "compact")
    assert full is not None and compact is not None
    # summary is a prefix of the full summary (truncation, no rewrite)
    assert full["summary"].startswith(compact["summary"])
    # compact canonical entities are the first N of full's, with fields dropped
    for c, f in zip(compact["canonical_entities"], full["canonical_entities"], strict=False):
        assert c["id"] == f["id"]
        assert c["canonical_name"] == f["canonical_name"]
        assert c["kind"] == f["kind"]


# ── _event_to_hit shaping (payload, conflicts, linked) ───────────────────────


def test_compact_hit_caps_payload_and_conflicts_and_linked() -> None:
    hit = _event_to_hit(
        _maximal_event(),
        db=None,
        full_payload=False,
        conflicts=_conflicts(),
        entity_overlay=_maximal_overlay(),
        interpretation_extraction=_maximal_extraction(),
        linked_event_ids=[f"sha256:{i}" for i in range(6)],
        verbosity="compact",
    )
    assert len(hit.payload["text"]) == COMPACT_TEXT_CHARS
    assert hit.truncated is True
    assert len(hit.payload["context"]) == 200
    assert len(hit.linked_event_ids) == COMPACT_MAX_LINKED_IDS
    kept_verdicts = {c.verdict for c in hit.conflicts}
    assert kept_verdicts == _COMPACT_KEPT_VERDICTS
    for c in hit.conflicts:
        assert len(c.reason) <= 160


def test_full_hit_keeps_everything() -> None:
    hit = _event_to_hit(
        _maximal_event(),
        db=None,
        full_payload=False,
        conflicts=_conflicts(),
        entity_overlay=_maximal_overlay(),
        interpretation_extraction=_maximal_extraction(),
        linked_event_ids=[f"sha256:{i}" for i in range(6)],
        verbosity="full",
    )
    # full keeps all conflicts and all linked ids, and the 500-char text cap.
    assert len(hit.conflicts) == len(_ALL_VERDICTS)
    assert len(hit.linked_event_ids) == 6
    assert len(hit.payload["text"]) == 500  # SUMMARY_TEXT_CHARS, unchanged
    assert hit.payload["context"] == "C" * 1000  # verbatim in full


# ── limit cap + defaults ─────────────────────────────────────────────────────


def _seed_events(ctx: ServerContext, n: int) -> None:
    from afair.mcp.schemas import TextContent

    for i in range(n):
        handlers.remember(content=TextContent(type="text", text=f"aurora memory number {i}"))


def test_limit_defaults_and_cap(ctx: ServerContext) -> None:
    _seed_events(ctx, 130)
    # compact no-limit → 10
    assert len(handlers.recall(query="aurora").hits) == COMPACT_DEFAULT_LIMIT
    # standard no-limit → 20
    assert len(handlers.recall(query="aurora", verbosity="standard").hits) == DEFAULT_RECALL_LIMIT
    # explicit over-cap limit clamped to 100
    assert len(handlers.recall(query="aurora", limit=500).hits) == MAX_RECALL_LIMIT
    # limit=None accepted at the tool layer (compact default)
    assert len(handlers.recall(query="aurora", limit=None).hits) == COMPACT_DEFAULT_LIMIT


# ── cursor pagination ────────────────────────────────────────────────────────


def test_cursor_pages_disjoint_and_terminates(ctx: ServerContext) -> None:
    _seed_events(ctx, 25)
    page1 = handlers.recall(query="aurora", limit=10)
    assert len(page1.hits) == 10
    assert page1.next_cursor == "10"

    page2 = handlers.recall(query="aurora", limit=10, cursor="10")
    assert page2.next_cursor == "20"
    ids1 = {h.event_id for h in page1.hits}
    ids2 = {h.event_id for h in page2.hits}
    assert ids1.isdisjoint(ids2)

    page3 = handlers.recall(query="aurora", limit=10, cursor="20")
    assert len(page3.hits) == 5
    assert page3.next_cursor is None  # final page


def test_cursor_paging_terminates_at_offset_cap(ctx: ServerContext) -> None:
    """Regression: next_cursor must never emit a value the cursor clamp
    (MAX_RECALL_OFFSET) can't honor, or a client paging 'until next_cursor is
    None' loops forever re-serving the capped window. Seed > the cap and drain
    the whole queue, asserting termination + zero repeated pages."""
    from afair.mcp.handlers import MAX_RECALL_OFFSET

    _seed_events(ctx, MAX_RECALL_OFFSET + 15)  # 215: enough to hit the cap edge

    seen_ids: set[str] = set()
    cursor: str | None = None
    capped_note = False
    for _ in range(100):  # hard safety bound: must terminate well before this
        r = handlers.recall(query="aurora", limit=10, cursor=cursor)
        page_ids = [h.event_id for h in r.hits]
        assert seen_ids.isdisjoint(page_ids), "a page was re-served (pagination loop)"
        seen_ids.update(page_ids)
        if r.note and "capped" in r.note:
            capped_note = True
        cursor = r.next_cursor
        if cursor is None:
            break
    else:  # pragma: no cover - only hit if pagination never terminates
        pytest.fail("cursor pagination did not terminate")

    assert cursor is None
    assert capped_note  # the final capped page announced the window cap
    # Drained exactly the reachable window (offset cap + one page), no repeats.
    assert len(seen_ids) == MAX_RECALL_OFFSET + 10


def test_malformed_cursor_served_page_one_with_note(ctx: ServerContext) -> None:
    _seed_events(ctx, 15)
    r = handlers.recall(query="aurora", limit=10, cursor="garbage")
    assert len(r.hits) == 10
    assert r.note is not None and "cursor" in r.note


def test_browse_mode_pages(ctx: ServerContext) -> None:
    _seed_events(ctx, 25)
    page1 = handlers.recall(limit=10)  # no query → browse
    assert len(page1.hits) == 10
    assert page1.next_cursor == "10"
    page2 = handlers.recall(limit=10, cursor="10")
    assert {h.event_id for h in page1.hits}.isdisjoint({h.event_id for h in page2.hits})


# ── coverage independence ────────────────────────────────────────────────────


def test_coverage_independent_of_verbosity(ctx: ServerContext) -> None:
    _seed_events(ctx, 5)
    covs = [
        handlers.recall(query="aurora", verbosity=v).coverage
        for v in ("compact", "standard", "full")
    ]
    counts = {c.unresolved_contradictions for c in covs if c is not None}
    assert len(counts) == 1  # identical across verbosities (computed pre-filter)

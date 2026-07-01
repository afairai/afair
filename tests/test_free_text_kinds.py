"""ADR-0003 Phase 3 — free-text extractor kinds + the kind_observations ledger.

The extractor's entity ``type`` is a free string (see the schema tests in
test_kinds.py); this file exercises what the canonicalizer does with it:

- a raw kind that maps to a live registry kind (slug, case variant, or
  variant map) resolves exactly as before and writes NO observation row;
- a genuinely novel raw kind lands the entity on a deterministic fallback
  (``other``) so the graph stays consistent, and the raw string is
  preserved in the append-only ``kind_observations`` ledger for the
  Schema-Evolver (Phase 4) — nothing auto-registers a kind;
- the Phase-2 kind-agreement homonym guard still separates same-name
  entities whose kinds arrive as free text.

Same fixture style as test_entity_canonicalizer.py: real SQLite, mocked LLM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import (
    CANONICALIZER_PRODUCED_BY,
    EntityCanonicalizer,
    _normalize_kind_with_novelty,
)
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import (
    iter_mentions_for_event,
    live_kind_slugs,
    open_db,
    resolve_entity_kind,
    write_event,
)

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
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


def _write_event_with_extraction(
    conn: sqlite3.Connection, *, text: str, entities: list[dict[str, str]]
) -> str:
    """Write an event + its extractor interpretation. Returns content_hash."""
    event = write_event(
        conn,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
    )
    write_interpretation(
        conn,
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
    return event.content_hash


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


def _llm_returns(matched_id: str | None, *, confidence: float = 0.9) -> Any:
    def _fake(**_kw: Any) -> LLMResult:
        return LLMResult(
            data={"matched_entity_id": matched_id, "reason": "test", "confidence": confidence},
            model=_kw.get("model", "test"),
            raw="",
        )

    return _fake


def _llm_boom(**_: Any) -> LLMResult:
    msg = "no LLM call expected in this test"
    raise AssertionError(msg)


def _observations(conn: sqlite3.Connection) -> list[Any]:
    return conn.execute(
        "SELECT raw_kind, normalized_slug, entity_id, event_id, observed_at, observed_by "
        "FROM kind_observations ORDER BY id"
    ).fetchall()


# ── known kinds and variants: behavior unchanged, no ledger row ────────────


def test_known_slug_and_variant_resolve_without_observation(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free-text kinds that map to a live registry kind — a verbatim slug
    (any case) or a variant ("human" → person) — resolve exactly as before
    Phase 3 and write NO observation row."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_boom)

    content_hash = _write_event_with_extraction(
        db,
        text="Sajinth founded Acme",
        entities=[
            {"name": "Sajinth", "type": "human"},
            {"name": "Acme", "type": "ORGANIZATION"},
        ],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 2
    assert stats["kind_observations"] == 0
    assert _observations(db) == []

    kinds = {
        m.surface_form: resolve_entity_kind(db, m.entity_id)
        for m in iter_mentions_for_event(db, content_hash)
    }
    assert kinds == {"Sajinth": "person", "Acme": "organization"}


def test_normalize_kind_with_novelty_flags(db: sqlite3.Connection) -> None:
    """The novelty flag fires only for raw kinds that map to nothing.
    Empty input is 'the extractor said nothing', not a proposal."""
    assert _normalize_kind_with_novelty("person", db) == ("person", False)
    assert _normalize_kind_with_novelty("  ORGANIZATION ", db) == ("organization", False)
    assert _normalize_kind_with_novelty("human", db) == ("person", False)
    assert _normalize_kind_with_novelty("org", db) == ("organization", False)
    assert _normalize_kind_with_novelty("recipe", db) == ("other", True)
    assert _normalize_kind_with_novelty("song", db) == ("other", True)
    assert _normalize_kind_with_novelty("", db) == ("other", False)
    assert _normalize_kind_with_novelty("   ", db) == ("other", False)


# ── novel kinds: fallback + ledger ─────────────────────────────────────────


def test_novel_kind_records_observation_and_falls_back(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely novel kind ('recipe') lands the entity on 'other' — the
    graph stays consistent, every entity kind resolves to a live registry
    kind — and the raw string is preserved in kind_observations."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    content_hash = _write_event_with_extraction(
        db,
        text="Cooked carbonara tonight",
        entities=[{"name": "Carbonara", "type": "recipe"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 1
    assert stats["kind_observations"] == 1

    mention = iter_mentions_for_event(db, content_hash)[0]
    assert resolve_entity_kind(db, mention.entity_id) == "other"
    assert resolve_entity_kind(db, mention.entity_id) in live_kind_slugs(db)

    rows = _observations(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["raw_kind"] == "recipe"
    assert row["normalized_slug"] == "other"
    assert row["entity_id"] == mention.entity_id
    assert row["event_id"] == mention.event_id
    assert row["observed_by"] == CANONICALIZER_PRODUCED_BY
    assert row["observed_at"]

    # Nothing auto-registered: 'recipe' is NOT a live kind (Phase 3 records,
    # the Schema-Evolver proposes, the operator confirms — later phases).
    assert "recipe" not in live_kind_slugs(db)


def test_novel_kind_squashed_into_existing_entity_kind(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ADR's promotion narrative: 'research_paper' squashed into
    'concept'. A novel raw kind normalizes to 'other', which kind-agrees
    with the existing same-name concept entity — the mention links there
    and the ledger records the kind it actually landed under."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_boom)

    h1 = _write_event_with_extraction(
        db,
        text="Read about GraphRAG today",
        entities=[{"name": "GraphRAG", "type": "concept"}],
    )
    h2 = _write_event_with_extraction(
        db,
        text="GraphRAG is a paper worth citing",
        entities=[{"name": "GraphRAG", "type": "research_paper"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 1
    assert stats["entities_matched_exact"] == 1
    assert stats["kind_observations"] == 1

    concept_id = iter_mentions_for_event(db, h1)[0].entity_id
    assert iter_mentions_for_event(db, h2)[0].entity_id == concept_id

    row = _observations(db)[0]
    assert row["raw_kind"] == "research_paper"
    assert row["normalized_slug"] == "concept"  # what it landed under
    assert row["entity_id"] == concept_id


def test_duplicate_surface_form_records_one_observation(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within-event repeats dedupe in the ledger too — one observation per
    (event, surface form), matching the one mention that gets written."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    _write_event_with_extraction(
        db,
        text="Bohemian Rhapsody, yes Bohemian Rhapsody",
        entities=[
            {"name": "Bohemian Rhapsody", "type": "song"},
            {"name": "Bohemian Rhapsody", "type": "song"},
        ],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["kind_observations"] == 1
    assert len(_observations(db)) == 1


def test_multiple_novel_kinds_record_one_row_each(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    _write_event_with_extraction(
        db,
        text="Carbonara while listening to Bohemian Rhapsody",
        entities=[
            {"name": "Carbonara", "type": "recipe"},
            {"name": "Bohemian Rhapsody", "type": "song"},
        ],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["kind_observations"] == 2
    assert {r["raw_kind"] for r in _observations(db)} == {"recipe", "song"}


# ── the Phase-2 homonym guard, with free-text kinds ────────────────────────


def test_homonym_guard_holds_with_free_text_kinds(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apple-the-org vs apple-the-concept, with kinds arriving as free text
    through the loosened schema ('org' via the variant map, 'concept' as a
    verbatim slug). Both resolve to live kinds, the guard compares the
    RESOLVED kinds, disagrees, and routes the homonym to the LLM instead of
    auto-linking — exactly the Phase 2 behavior. No ledger rows: both raw
    kinds mapped."""
    _no_sleep(monkeypatch)
    # The homonym judge rules them different things (the correct verdict).
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    h1 = _write_event_with_extraction(
        db,
        text="Apple released a new product",
        entities=[{"name": "Apple", "type": "org"}],
    )
    h2 = _write_event_with_extraction(
        db,
        text="I want an apple for lunch",
        entities=[{"name": "apple", "type": "concept"}],
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_created"] == 2
    assert stats["entities_matched_exact"] == 0
    assert stats["homonym_splits"] == 1
    assert stats["kind_observations"] == 0

    org_id = iter_mentions_for_event(db, h1)[0].entity_id
    concept_id = iter_mentions_for_event(db, h2)[0].entity_id
    assert org_id != concept_id
    assert resolve_entity_kind(db, org_id) == "organization"
    assert resolve_entity_kind(db, concept_id) == "concept"

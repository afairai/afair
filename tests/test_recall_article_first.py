"""Recall article-first ordering — entity_article hits surface before the
raw events they summarize (Karpathy LLM-Wiki / RAG-bypass)."""

from __future__ import annotations

from afair.agents.entity_articles import ENTITY_ARTICLE_KIND
from afair.agents.living_syntheses import LIVING_SYNTHESIS_KIND
from afair.mcp.handlers import _article_first_order
from afair.substrate.events import Event


def _ev(kind: str, n: int) -> Event:
    return Event(
        id=f"e{n}",
        content_hash=f"sha256:{n:064d}",
        created_at=f"2026-06-07T00:00:0{n}+00:00",
        origin="agent",
        kind=kind,
        payload={},
        schema_version=1,
    )


def test_articles_move_to_front_preserving_order() -> None:
    events = [
        _ev("remember", 1),
        _ev(ENTITY_ARTICLE_KIND, 2),
        _ev("observe", 3),
        _ev(ENTITY_ARTICLE_KIND, 4),
    ]
    out = _article_first_order(events)
    assert [e.id for e in out] == ["e2", "e4", "e1", "e3"]


def test_living_synthesis_precedes_legacy_article() -> None:
    events = [
        _ev(ENTITY_ARTICLE_KIND, 1),
        _ev("remember", 2),
        _ev(LIVING_SYNTHESIS_KIND, 3),
        _ev(ENTITY_ARTICLE_KIND, 4),
    ]
    out = _article_first_order(events)
    assert [event.id for event in out] == ["e3", "e1", "e4", "e2"]


def test_no_articles_is_identity() -> None:
    events = [_ev("remember", 1), _ev("observe", 2)]
    assert _article_first_order(events) == events


def test_all_articles_keep_relative_order() -> None:
    events = [_ev(ENTITY_ARTICLE_KIND, 1), _ev(ENTITY_ARTICLE_KIND, 2)]
    assert [e.id for e in _article_first_order(events)] == ["e1", "e2"]


def test_invalidated_article_is_dropped_not_hoisted() -> None:
    # The stale article (e2) is superseded; it must not lead — or even
    # appear in — the result. The current article (e4) leads; raw events
    # follow.
    events = [
        _ev("remember", 1),
        _ev(ENTITY_ARTICLE_KIND, 2),  # superseded
        _ev("observe", 3),
        _ev(ENTITY_ARTICLE_KIND, 4),  # current
    ]
    invalidated = {events[1].content_hash}
    out = _article_first_order(events, invalidated=invalidated)
    assert [e.id for e in out] == ["e4", "e1", "e3"]
    assert events[1].content_hash not in {e.content_hash for e in out}


def test_only_stale_articles_falls_through_to_raw_events() -> None:
    # Every matching article is stale → recall must still return the raw
    # events, never an empty result nor the dead articles.
    events = [
        _ev(ENTITY_ARTICLE_KIND, 1),
        _ev("remember", 2),
        _ev(ENTITY_ARTICLE_KIND, 3),
    ]
    invalidated = {events[0].content_hash, events[2].content_hash}
    out = _article_first_order(events, invalidated=invalidated)
    assert [e.id for e in out] == ["e2"]

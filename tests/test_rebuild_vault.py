"""Vault replay: source-event selection.

The risky part of rebuild_vault is deciding what to copy. The source set must
keep the user's irreplaceable input (remember/observe) and their own
supersessions (invalidate targeting a source event), while dropping the
cold-path derivations (entity_article/consolidation and the agent-issued
invalidations that superseded prior articles). This locks that down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.invalidation import write_invalidation
from afair.substrate import open_db, write_event
from afair.substrate.db import set_vault_key
from afair.substrate.events import iter_events
from scripts.rebuild_vault import select_source_events

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _plaintext():
    set_vault_key(None)
    yield
    set_vault_key(None)


def test_selects_user_input_and_user_invalidations_drops_derivations(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        # User input — kept.
        r1 = write_event(
            db, origin="user", kind="remember", payload={"content_type": "text", "text": "a fact"}
        )
        write_event(
            db,
            origin="agent",
            kind="observe",
            payload={"content_type": "event", "action": "edit", "subject": "x"},
        )
        # A supersession the user issued (invalidate targeting a remember) — kept.
        write_invalidation(db, target_hash=r1.content_hash, reason="superseded", origin="user")

        # Cold-path derivations — dropped.
        article = write_event(
            db, origin="agent", kind="entity_article", payload={"content_type": "text", "text": "p"}
        )
        write_event(
            db, origin="agent", kind="consolidation", payload={"content_type": "text", "text": "c"}
        )
        write_event(
            db,
            origin="agent",
            kind="entity_dedup_decision",
            payload={"content_type": "text", "text": "d"},
        )
        # An agent invalidation superseding a derived article — dropped (its
        # target is not a source event).
        write_invalidation(db, target_hash=article.content_hash, reason="re-synth", origin="agent")

        events = list(iter_events(db))
    finally:
        db.close()

    selected = select_source_events(events)
    kinds = sorted(e.kind for e in selected)
    assert kinds == ["invalidate", "observe", "remember"]
    # The kept invalidate is the user one (targets the remember), not the
    # agent one (targets the article).
    inval = next(e for e in selected if e.kind == "invalidate")
    assert r1.content_hash in (inval.parent_hashes or [])


def test_selection_is_chronological(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": "second"},
            created_at="2026-02-01T00:00:00+00:00",
        )
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": "first"},
            created_at="2026-01-01T00:00:00+00:00",
        )
        events = list(iter_events(db))
    finally:
        db.close()

    selected = select_source_events(events)
    created = [e.created_at for e in selected]
    assert created == sorted(created)

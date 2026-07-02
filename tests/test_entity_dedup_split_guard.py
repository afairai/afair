"""ADR-0003 Phase 2 completion — Slice 4: deliberate-split guard.

A recorded homonym split (>= 2 v2 disambiguators for a name, all members
v2 split identities) is skipped by the deduplicator: the split was an
explicit judgment already sitting in entity_identities, and re-judging
risks merging what was deliberately separated. A cluster that also holds a
member OUTSIDE the split set (a v1 leftover) is judged as usual.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import entity_dedup as ed
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.entities import (
    entity_id,
    write_entity,
    write_entity_mention,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _mention(conn: sqlite3.Connection, *, entity_id_: str, name: str, text: str) -> None:
    event = write_event(
        conn, origin="agent", kind="remember", payload={"content_type": "text", "text": text}
    )
    write_entity_mention(
        conn,
        entity_id=entity_id_,
        event_id=event.id,
        event_hash=event.content_hash,
        surface_form=name,
        canonicalized_by="test",
        match_method="exact",
        confidence=0.9,
    )


def _v2_split_pair(conn: sqlite3.Connection, name: str, kind: str) -> tuple[str, str]:
    """Two v2 identities of the same name (disambiguators 0 and 1) — a
    deliberate homonym split as the canonicalizer records it."""
    seed = write_event(
        conn, origin="agent", kind="remember", payload={"content_type": "text", "text": "seed"}
    )
    first = write_entity(
        conn,
        canonical_name=name,
        kind=kind,
        created_by="entity_canonicalizer:v0",
        source_event_id=seed.id,
        confidence=0.5,
    )
    second = write_entity(
        conn,
        canonical_name=name,
        kind=kind,
        created_by="entity_canonicalizer:v0",
        source_event_id=seed.id,
        confidence=0.5,
        split_homonym=True,
    )
    _mention(conn, entity_id_=first.id, name=name, text=f"{name} number one")
    _mention(conn, entity_id_=second.id, name=name, text=f"{name} number two")
    return first.id, second.id


def _seed_v1(conn: sqlite3.Connection, *, name: str, kind: str) -> str:
    eid = entity_id(name, kind)
    event = write_event(
        conn, origin="agent", kind="remember", payload={"content_type": "text", "text": "v1 seed"}
    )
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO entities (
                id, canonical_name, kind, created_at, created_by,
                confidence, source_event_id
            ) VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 'pre-phase2', 0.8, ?)
            """,
            (eid, name, kind, event.id),
        )
    _mention(conn, entity_id_=eid, name=name, text=f"{name} legacy note")
    return eid


def _boom_judge(monkeypatch) -> None:
    def _call(**_: Any) -> LLMResult:
        msg = "a deliberate split must be skipped before the LLM judge"
        raise AssertionError(msg)

    monkeypatch.setattr(ed, "call_tool", _call)


def _stub_judge(monkeypatch, *, same: bool, confidence: float, counter: dict) -> None:
    def _call(**_: Any) -> LLMResult:
        counter["n"] = counter.get("n", 0) + 1
        return LLMResult(
            data={"same_entity": same, "reason": "stub", "confidence": confidence},
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ed, "call_tool", _call)


def test_deliberate_split_pair_is_skipped(conn, monkeypatch) -> None:
    _v2_split_pair(conn, "Sajinth", "person")
    _boom_judge(monkeypatch)  # asserts the LLM is never called

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["skipped_deliberate_split"] == 1
    assert stats["clusters_examined"] == 0
    assert stats["clusters_merged"] == 0


def test_split_pair_plus_v1_leftover_is_judged(conn, monkeypatch) -> None:
    """A v1 member sharing the name is OUTSIDE the split set → judge as usual."""
    _v2_split_pair(conn, "Sajinth", "person")
    _seed_v1(conn, name="Sajinth", kind="person")
    counter: dict = {}
    _stub_judge(monkeypatch, same=False, confidence=0.9, counter=counter)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["skipped_deliberate_split"] == 0
    assert stats["clusters_examined"] == 1
    assert counter["n"] == 1  # the LLM judged it


def test_single_v2_identity_is_not_a_split(conn, monkeypatch) -> None:
    """One v2 identity plus a v1 leftover (only one disambiguator) is not a
    deliberate split — the guard must not fire on a lone v2 entity."""
    seed = write_event(
        conn, origin="agent", kind="remember", payload={"content_type": "text", "text": "seed"}
    )
    v2 = write_entity(
        conn,
        canonical_name="Clario",
        kind="product",
        created_by="entity_canonicalizer:v0",
        source_event_id=seed.id,
        confidence=0.5,
    )
    _mention(conn, entity_id_=v2.id, name="Clario", text="Clario the product")
    _seed_v1(conn, name="Clario", kind="project")
    counter: dict = {}
    _stub_judge(monkeypatch, same=False, confidence=0.9, counter=counter)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["skipped_deliberate_split"] == 0
    assert stats["clusters_examined"] == 1
    assert counter["n"] == 1

"""Chain-arm tests (P1b) — multihop assembly, abstention guard, fail-soft.

The scenario mirrors the vault-native benchmark's multihop shape:
query "Alpha Gamma", evidence events mention {Alpha,Beta} and {Beta,Gamma},
the middle entity Beta is hidden from the query. Soft associations come from
a sidecar file next to the vault; a missing sidecar must degrade silently.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from afair.mcp.assoc_expand import (
    _query_entity_names,
    events_via_chain,
)
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from pathlib import Path


def _mk_entity(db: sqlite3.Connection, eid: str, name: str, src: str) -> None:
    db.execute(
        """INSERT INTO entities (id, canonical_name, kind, created_at, created_by,
           confidence, source_event_id) VALUES (?, ?, 'concept',
           '2026-08-01T00:00:00+00:00', 'test', 0.9, ?)""",
        (eid, name, src),
    )


def _mention(db: sqlite3.Connection, mid: str, eid: str, event_id: str,
             event_hash: str, surface: str) -> None:
    db.execute(
        """INSERT INTO entity_mentions (id, entity_id, event_id, event_hash,
           surface_form, canonicalized_at, canonicalized_by, match_method,
           confidence) VALUES (?, ?, ?, ?, ?, '2026-08-01T00:00:00+00:00',
           'test', 'exact', 0.9)""",
        (mid, eid, event_id, event_hash, surface),
    )


@pytest.fixture
def vault(tmp_path: Path):
    db = open_db(tmp_path)
    ev1 = write_event(db, origin="user", kind="remember",
                      payload={"content_type": "text", "text": "Alpha arbeitet mit Beta"})
    ev2 = write_event(db, origin="user", kind="remember",
                      payload={"content_type": "text", "text": "Beta gehoert zu Gamma"})
    ev3 = write_event(db, origin="user", kind="remember",
                      payload={"content_type": "text", "text": "Nur Alpha allein"})
    for eid, name in (("en-a", "Alpha"), ("en-b", "Beta"), ("en-g", "Gamma"),
                      ("en-x", "Delta")):
        _mk_entity(db, eid, name, ev1.id)
    _mention(db, "m1", "en-a", ev1.id, ev1.content_hash, "Alpha")
    _mention(db, "m2", "en-b", ev1.id, ev1.content_hash, "Beta")
    _mention(db, "m3", "en-b", ev2.id, ev2.content_hash, "Beta")
    _mention(db, "m4", "en-g", ev2.id, ev2.content_hash, "Gamma")
    _mention(db, "m5", "en-a", ev3.id, ev3.content_hash, "Alpha")
    db.commit()
    return tmp_path, db, ev1, ev2, ev3


def _sidecar(tmp_path: Path, rows) -> None:
    con = sqlite3.connect(str(tmp_path / "associations.db"))
    con.execute("CREATE TABLE assoc_weights(name TEXT, other TEXT, weight REAL)")
    con.executemany("INSERT INTO assoc_weights VALUES (?,?,?)", rows)
    con.commit()
    con.close()


def test_query_entity_names_finds_both_endpoints(vault) -> None:
    _tmp_path, db, *_ = vault
    assert set(_query_entity_names(db, "Alpha Gamma")) == {"Alpha", "Gamma"}


def test_chain_assembles_multihop_via_sidecar(vault) -> None:
    """Soft association Alpha~Beta + Gamma~Beta bridges the hidden middle:
    BOTH evidence events must surface, chain events (2 matches) first."""
    tmp_path, db, ev1, ev2, ev3 = vault
    _sidecar(tmp_path, [("Alpha", "Beta", 2.0), ("Gamma", "Beta", 1.5)])
    got = [e.id for e in events_via_chain(db, tmp_path, "Alpha Gamma")]
    assert ev1.id in got and ev2.id in got
    assert ev3.id not in got  # single-entity event never clears the >=2 floor


def test_chain_uses_hard_edges_without_sidecar(vault) -> None:
    """No sidecar file: hard edges on BOTH sides make Beta a bridge — a
    single one-sided edge is deliberately NOT enough (abstention guard:
    a hub's unrelated edges must never fabricate a chain)."""
    tmp_path, db, ev1, ev2, _ev3 = vault
    for eid, s, o, src in (("ed1", "en-a", "en-b", ev1.id),
                           ("ed2", "en-g", "en-b", ev2.id)):
        db.execute(
            """INSERT INTO entity_edges (id, subject_id, predicate, object_id,
               valid_from, discovered_at, discovered_by, source_event_id,
               confidence) VALUES (?, ?, 'works_with', ?,
               '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
               'test', ?, 0.9)""",
            (eid, s, o, src),
        )
    db.commit()
    got = [e.id for e in events_via_chain(db, tmp_path, "Alpha Gamma")]
    assert ev1.id in got and ev2.id in got  # both legs of the bridge


def test_abstention_guard_unrelated_pair(vault) -> None:
    """Alpha and Delta share no event and no neighbor overlap → the >=2 floor
    keeps the arm empty for the {Alpha,Delta}-only chain; the lone shared-free
    event ev3 (Alpha only) must not be fabricated into a connection."""
    tmp_path, db, _ev1, _ev2, ev3 = vault
    got = [e.id for e in events_via_chain(db, tmp_path, "Alpha Delta")]
    assert ev3.id not in got


def test_kill_switch(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path, db, *_ = vault  # tmp_path: sidecar target below
    _sidecar(tmp_path, [("Alpha", "Beta", 2.0)])
    monkeypatch.setenv("AFAIR_CHAIN_RECALL", "0")
    assert events_via_chain(db, tmp_path, "Alpha Gamma") == []


def test_prose_query_is_noop(vault) -> None:
    tmp_path, db, *_ = vault
    q = "was haben wir eigentlich damals in dem langen gespraech ueber die " \
        "zukunft des projekts im detail besprochen und entschieden"
    assert events_via_chain(db, tmp_path, q) == []

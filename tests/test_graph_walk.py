"""Bounded multi-hop expansion — tests for ``substrate/graph_walk.py``.

Everything here runs against real SQLite with the real I2 triggers, matching
the convention in ``test_entities.py``: the belief filters (invalidated edges,
retracted entities, the ADR-0004 confidence floor) are only worth having if
they hold against the actual tables.

The properties under test are the three the recall path was promised:
**bounded** (every ceiling actually bites), **hub-suppressed**, and
**deterministic**.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from afair.substrate import (
    open_db,
    retract_entity,
    write_edge_invalidation,
    write_entity,
    write_entity_edge,
    write_entity_mention,
    write_event,
)
from afair.substrate.edge_validity import write_edge_validity_span
from afair.substrate.graph_walk import (
    DEFAULT_LIMITS,
    WalkLimits,
    effective_validity_batch,
    is_currently_valid,
    seed_entities,
    walk_from_events,
)
from afair.substrate.search import mmr_rerank

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    from afair.substrate.events import Event


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


class _Graph:
    """Tiny builder so each test reads as the graph it is about, not as setup."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.events: dict[str, Event] = {}
        self.entities: dict[str, str] = {}

    def event(self, name: str, text: str | None = None) -> Event:
        event = write_event(
            self.conn,
            origin="user",
            kind="remember",
            payload={"content_type": "text", "text": text or f"memory about {name}"},
        )
        self.events[name] = event
        return event

    def entity(self, name: str, *, on: str, kind: str = "person") -> str:
        """Create an entity and record that event ``on`` mentions it."""
        source = self.events[on]
        entity = write_entity(
            self.conn,
            canonical_name=name,
            kind=kind,
            created_by="test",
            source_event_id=source.id,
            confidence=0.9,
        )
        self.entities[name] = entity.id
        write_entity_mention(
            self.conn,
            entity_id=entity.id,
            event_id=source.id,
            event_hash=source.content_hash,
            surface_form=name,
            canonicalized_by="test",
            match_method="exact",
            confidence=0.9,
        )
        return entity.id

    def mention(self, entity_name: str, *, on: str) -> None:
        source = self.events[on]
        write_entity_mention(
            self.conn,
            entity_id=self.entities[entity_name],
            event_id=source.id,
            event_hash=source.content_hash,
            surface_form=entity_name,
            canonicalized_by="test",
            match_method="exact",
            confidence=0.9,
        )

    def edge(
        self, subject: str, predicate: str, obj: str, *, on: str, confidence: float = 0.9
    ) -> str | None:
        edge = write_entity_edge(
            self.conn,
            subject_id=self.entities[subject],
            predicate=predicate,
            object_id=self.entities[obj],
            source_event_id=self.events[on].id,
            discovered_by="test",
            confidence=confidence,
        )
        return edge.id if edge else None


def _legacy_edge(
    db: sqlite3.Connection,
    g: _Graph,
    *,
    valid_from: str | None,
    valid_to: str | None,
    subject: str = "Anchor",
    obj: str = "Neighbour",
    on: str = "here",
) -> str:
    """Insert an edge the way a pre-ADR-0008 vault holds one: columns only.

    Direct INSERT rather than ``write_entity_edge`` because that helper now
    always seeds a validity span — and a span cannot be removed afterwards, the
    append-only triggers see to that. This is the honest way to reproduce a
    legacy row.
    """
    edge_id = f"edge-legacy-{valid_from}-{valid_to}"
    db.execute(
        """
        INSERT INTO entity_edges (
            id, subject_id, predicate, object_id, valid_from, valid_to,
            discovered_at, discovered_by, source_event_id, confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_id,
            g.entities[subject],
            "works_with",
            g.entities[obj],
            valid_from,
            valid_to,
            "2026-01-01T00:00:00+00:00",
            "legacy",
            g.events[on].id,
            0.9,
        ),
    )
    db.commit()
    return edge_id


@pytest.fixture
def chain(db: sqlite3.Connection) -> _Graph:
    """A → B → C → D, each entity mentioned on its own event.

    ``e_a`` is the recall hit the walk starts from; ``e_b`` is one hop away,
    ``e_c`` two, and ``e_d`` three — i.e. out of reach by construction.
    """
    g = _Graph(db)
    for name in ("e_a", "e_b", "e_c", "e_d"):
        g.event(name)
    g.entity("Anna", on="e_a")
    g.entity("Bruno", on="e_b")
    g.entity("Carla", on="e_c")
    g.entity("Dora", on="e_d")
    g.edge("Anna", "works_with", "Bruno", on="e_a")
    g.edge("Bruno", "works_with", "Carla", on="e_b")
    g.edge("Carla", "works_with", "Dora", on="e_c")
    return g


class TestSeeding:
    def test_seeds_come_from_the_top_hits(self, db: sqlite3.Connection, chain: _Graph) -> None:
        seeds = seed_entities(db, [chain.events["e_a"]])
        assert seeds == [chain.entities["Anna"]]

    def test_no_mentions_means_no_seeds(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        lonely = g.event("lonely")
        assert seed_entities(db, [lonely]) == []

    def test_empty_input_is_a_no_op(self, db: sqlite3.Connection) -> None:
        assert seed_entities(db, []) == []

    def test_seed_count_is_capped(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("crowded")
        for i in range(12):
            g.entity(f"Person{i:02d}", on="crowded")
        limits = replace(DEFAULT_LIMITS, max_seeds=3)
        assert len(seed_entities(db, [g.events["crowded"]], limits=limits)) == 3

    def test_only_the_top_events_contribute(self, db: sqlite3.Connection, chain: _Graph) -> None:
        limits = replace(DEFAULT_LIMITS, max_seed_events=1)
        seeds = seed_entities(db, [chain.events["e_a"], chain.events["e_b"]], limits=limits)
        assert seeds == [chain.entities["Anna"]]


class TestHopBounds:
    def test_one_hop_reaches_the_neighbour(self, db: sqlite3.Connection, chain: _Graph) -> None:
        limits = replace(DEFAULT_LIMITS, max_hops=1)
        found, stats = walk_from_events(db, [chain.events["e_a"]], limits=limits)
        assert [e.content_hash for e in found] == [chain.events["e_b"].content_hash]
        assert stats.hops_used == 1
        assert stats.seeds == 1

    def test_two_hops_reach_the_neighbours_neighbour(
        self, db: sqlite3.Connection, chain: _Graph
    ) -> None:
        found, stats = walk_from_events(db, [chain.events["e_a"]])
        hashes = {e.content_hash for e in found}
        assert chain.events["e_b"].content_hash in hashes
        assert chain.events["e_c"].content_hash in hashes
        assert stats.hops_used == 2

    def test_the_third_hop_is_never_reached(self, db: sqlite3.Connection, chain: _Graph) -> None:
        """max_hops=2 is a ceiling, not a default that a dense graph can bend."""
        found, _ = walk_from_events(db, [chain.events["e_a"]])
        assert chain.events["e_d"].content_hash not in {e.content_hash for e in found}

    def test_max_hops_cannot_exceed_two_in_the_shipped_defaults(self) -> None:
        assert DEFAULT_LIMITS.max_hops == 2

    def test_events_already_in_the_result_are_not_repeated(
        self, db: sqlite3.Connection, chain: _Graph
    ) -> None:
        found, _ = walk_from_events(db, [chain.events["e_a"], chain.events["e_b"]])
        hashes = {e.content_hash for e in found}
        assert chain.events["e_a"].content_hash not in hashes
        assert chain.events["e_b"].content_hash not in hashes


class TestBeliefFilters:
    def test_an_invalidated_edge_is_not_followed(
        self, db: sqlite3.Connection, chain: _Graph
    ) -> None:
        edge_id = chain.edge("Anna", "also_knows", "Dora", on="e_a")
        assert edge_id is not None
        write_edge_invalidation(
            db,
            edge_id=edge_id,
            invalidated_by="test",
            reason="superseded",
            source_event_id=chain.events["e_a"].id,
        )
        found, _ = walk_from_events(
            db, [chain.events["e_a"]], limits=replace(DEFAULT_LIMITS, max_hops=1)
        )
        assert chain.events["e_d"].content_hash not in {e.content_hash for e in found}

    def test_a_retracted_entity_is_never_reached(
        self, db: sqlite3.Connection, chain: _Graph
    ) -> None:
        retract_entity(
            db,
            entity_id=chain.entities["Bruno"],
            retracted_by="test",
            reason="noise",
            source_event_id=chain.events["e_a"].id,
        )
        found, _ = walk_from_events(db, [chain.events["e_a"]])
        assert chain.events["e_b"].content_hash not in {e.content_hash for e in found}

    def test_an_edge_below_the_confidence_floor_is_not_followed(
        self, db: sqlite3.Connection
    ) -> None:
        g = _Graph(db)
        g.event("e_x")
        g.event("e_y")
        g.entity("Xaver", on="e_x")
        g.entity("Yvonne", on="e_y")
        g.edge("Xaver", "maybe_knows", "Yvonne", on="e_x", confidence=0.1)
        found, _ = walk_from_events(db, [g.events["e_x"]])
        assert found == []

    def test_the_floor_is_configurable_downwards(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("e_x")
        g.event("e_y")
        g.entity("Xaver", on="e_x")
        g.entity("Yvonne", on="e_y")
        g.edge("Xaver", "maybe_knows", "Yvonne", on="e_x", confidence=0.1)
        limits = replace(DEFAULT_LIMITS, min_edge_confidence=0.0)
        found, _ = walk_from_events(db, [g.events["e_x"]], limits=limits)
        assert [e.content_hash for e in found] == [g.events["e_y"].content_hash]


class TestWorldTimeValidity:
    """ADR-0008: only relations that hold *now* are roads through the graph.

    The clock is injected rather than mocked, so these assert the real
    comparison against the real sidecar rows.
    """

    NOW = "2026-08-03T12:00:00+00:00"

    @pytest.fixture
    def pair(self, db: sqlite3.Connection) -> _Graph:
        g = _Graph(db)
        g.event("here")
        g.event("there")
        g.entity("Anchor", on="here")
        g.entity("Neighbour", on="there")
        return g

    def _edge_with_span(
        self,
        db: sqlite3.Connection,
        g: _Graph,
        *,
        valid_from: str | None,
        valid_to: str | None,
    ) -> str:
        edge_id = g.edge("Anchor", "works_with", "Neighbour", on="here")
        assert edge_id is not None
        write_edge_validity_span(
            db,
            edge_id=edge_id,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_by="test:validity",
            confidence=0.9,
            reason="pinned by test",
        )
        return edge_id

    def test_an_expired_relation_is_not_traversed(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        self._edge_with_span(
            db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to="2026-06-01T00:00:00+00:00"
        )
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert found == []
        assert stats.expired_skipped == 1
        assert stats.not_yet_valid_skipped == 0

    def test_a_not_yet_valid_relation_is_not_traversed(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        self._edge_with_span(db, pair, valid_from="2027-01-01T00:00:00+00:00", valid_to=None)
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert found == []
        assert stats.not_yet_valid_skipped == 1
        assert stats.expired_skipped == 0

    def test_an_open_ended_current_relation_is_traversed(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        self._edge_with_span(db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to=None)
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert [e.content_hash for e in found] == [pair.events["there"].content_hash]
        assert stats.expired_skipped == 0
        assert stats.not_yet_valid_skipped == 0

    def test_the_interval_is_half_open_at_the_upper_bound(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """valid_to exactly == now means the fact has already stopped holding,
        matching edge_validity.edge_is_valid_at's [from, to) convention."""
        self._edge_with_span(db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to=self.NOW)
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert found == []
        assert stats.expired_skipped == 1

    def test_the_interval_is_closed_at_the_lower_bound(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """valid_from exactly == now means the fact holds as of this instant."""
        self._edge_with_span(db, pair, valid_from=self.NOW, valid_to=None)
        found, _ = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert [e.content_hash for e in found] == [pair.events["there"].content_hash]

    def test_a_later_span_overrides_an_earlier_one(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """Correction, not mutation: the newest append wins, and it can revive a
        relation an earlier interpretation had closed."""
        edge_id = self._edge_with_span(
            db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to="2026-06-01T00:00:00+00:00"
        )
        assert walk_from_events(db, [pair.events["here"]], now=self.NOW)[0] == []
        write_edge_validity_span(
            db,
            edge_id=edge_id,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to=None,
            recorded_by="test:validity:v2",
            confidence=0.95,
            reason="correction: the relation never actually ended",
        )
        found, _ = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert [e.content_hash for e in found] == [pair.events["there"].content_hash]

    def test_an_edge_without_any_span_is_still_traversed(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """Legacy edges and anything the backfill has not reached must not
        silently vanish from the graph for lack of a sidecar row.

        The edge row is inserted directly, which is exactly the shape a
        pre-ADR-0008 vault has: bare columns, no span. It cannot be produced by
        deleting a span afterwards — the I2 triggers refuse that, correctly.
        """
        _legacy_edge(db, pair, valid_from=None, valid_to=None)
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert [e.content_hash for e in found] == [pair.events["there"].content_hash]
        assert stats.expired_skipped == 0

    def test_a_legacy_column_bound_is_honored_without_a_span(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """The immutable column is the fallback, not decoration: an old edge
        whose column says the relation ended is still correctly excluded."""
        _legacy_edge(
            db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to="2026-06-01T00:00:00+00:00"
        )
        found, stats = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert found == []
        assert stats.expired_skipped == 1

    def test_a_malformed_bound_reads_as_unknown_not_as_invalid(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """Unparseable legacy data must degrade to "no bound", never to a
        silent exclusion from the live graph."""
        _legacy_edge(db, pair, valid_from="not-a-date", valid_to=None)
        found, _ = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert [e.content_hash for e in found] == [pair.events["there"].content_hash]

    def test_expired_relations_do_not_consume_the_fanout_budget(
        self, db: sqlite3.Connection
    ) -> None:
        """A cluster of dead relations must not starve the one live relation."""
        g = _Graph(db)
        g.event("centre")
        g.entity("Centre", on="centre", kind="project")
        for i in range(5):
            g.event(f"dead{i}")
            g.entity(f"Dead{i}", on=f"dead{i}")
            edge_id = g.edge("Centre", "involved", f"Dead{i}", on="centre", confidence=0.95)
            assert edge_id is not None
            write_edge_validity_span(
                db,
                edge_id=edge_id,
                valid_from="2026-01-01T00:00:00+00:00",
                valid_to="2026-06-01T00:00:00+00:00",
                recorded_by="test:validity",
                confidence=0.9,
                reason="ended",
            )
        g.event("alive")
        g.entity("Alive", on="alive")
        live_edge = g.edge("Centre", "involves", "Alive", on="centre", confidence=0.5)
        assert live_edge is not None
        write_edge_validity_span(
            db,
            edge_id=live_edge,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to=None,
            recorded_by="test:validity",
            confidence=0.9,
            reason="ongoing",
        )
        # Fanout of 2 — the five expired edges outrank the live one on
        # confidence and would eat the whole budget if they were counted.
        limits = replace(DEFAULT_LIMITS, max_fanout_per_entity=2, max_hops=1)
        found, stats = walk_from_events(db, [g.events["centre"]], limits=limits, now=self.NOW)
        assert [e.content_hash for e in found] == [g.events["alive"].content_hash]
        assert stats.expired_skipped == 5

    def test_knowledge_expiry_still_wins_over_a_valid_world_time(
        self, db: sqlite3.Connection, pair: _Graph
    ) -> None:
        """The two clocks are independent boundaries: an invalidated edge stays
        out even when its world-time interval says it currently holds."""
        edge_id = self._edge_with_span(
            db, pair, valid_from="2026-01-01T00:00:00+00:00", valid_to=None
        )
        write_edge_invalidation(
            db,
            edge_id=edge_id,
            invalidated_by="test",
            reason="retired belief",
            source_event_id=pair.events["here"].id,
        )
        found, _ = walk_from_events(db, [pair.events["here"]], now=self.NOW)
        assert found == []


class TestEffectiveValidityBatch:
    def test_the_sidecar_overrides_the_immutable_column(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("here")
        g.event("there")
        g.entity("Anchor", on="here")
        g.entity("Neighbour", on="there")
        edge_id = g.edge("Anchor", "works_with", "Neighbour", on="here")
        assert edge_id is not None
        write_edge_validity_span(
            db,
            edge_id=edge_id,
            valid_from="2025-05-05T00:00:00+00:00",
            valid_to="2025-09-09T00:00:00+00:00",
            recorded_by="test:validity:latest",
            confidence=0.9,
            reason="corrected interval",
        )
        out = effective_validity_batch(db, {edge_id: ("2020-01-01T00:00:00+00:00", None)})
        assert out[edge_id] == ("2025-05-05T00:00:00+00:00", "2025-09-09T00:00:00+00:00")

    def test_the_column_is_the_fallback_without_a_span(self, db: sqlite3.Connection) -> None:
        out = effective_validity_batch(db, {"edge:absent": ("2020-01-01T00:00:00+00:00", None)})
        assert out["edge:absent"] == ("2020-01-01T00:00:00+00:00", None)

    def test_empty_input_costs_no_query(self, db: sqlite3.Connection) -> None:
        assert effective_validity_batch(db, {}) == {}


class TestCurrentValidityPredicate:
    NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

    def test_open_on_both_sides_is_always_current(self) -> None:
        assert is_currently_valid(None, None, self.NOW) == (True, None)

    def test_expired_reports_its_reason(self) -> None:
        assert is_currently_valid(None, "2026-01-01T00:00:00+00:00", self.NOW) == (False, "expired")

    def test_future_reports_its_reason(self) -> None:
        assert is_currently_valid("2027-01-01T00:00:00+00:00", None, self.NOW) == (False, "not_yet")

    def test_a_naive_bound_is_read_as_utc(self) -> None:
        """normalize_temporal_bound's contract — substrate timestamps are UTC."""
        assert is_currently_valid("2026-01-01T00:00:00", None, self.NOW)[0] is True


class TestHubSuppression:
    def test_a_hub_is_reachable_but_not_traversed(self, db: sqlite3.Connection) -> None:
        """The operator's own name connects to everything. Reaching it is fine;
        expanding through it would drag the whole vault into every answer."""
        g = _Graph(db)
        g.event("seed")
        g.event("hub_home")
        g.entity("Seedling", on="seed")
        g.entity("Michael", on="hub_home")
        g.edge("Seedling", "knows", "Michael", on="seed")
        # Give the hub a large neighbourhood, each with its own event.
        for i in range(8):
            g.event(f"far{i}")
            g.entity(f"Far{i}", on=f"far{i}")
            g.edge("Michael", "knows", f"Far{i}", on="hub_home")

        limits = replace(DEFAULT_LIMITS, hub_degree_max=3)
        found, stats = walk_from_events(db, [g.events["seed"]], limits=limits)
        hashes = {e.content_hash for e in found}
        assert g.events["hub_home"].content_hash in hashes, "the hub itself stays reachable"
        assert not any(g.events[f"far{i}"].content_hash in hashes for i in range(8))
        assert stats.hubs_skipped >= 1

    def test_a_normal_entity_is_traversed(self, db: sqlite3.Connection, chain: _Graph) -> None:
        _found, stats = walk_from_events(db, [chain.events["e_a"]])
        assert stats.hubs_skipped == 0


class TestFanoutAndTotals:
    @pytest.fixture
    def star(self, db: sqlite3.Connection) -> _Graph:
        g = _Graph(db)
        g.event("centre")
        g.entity("Centre", on="centre", kind="project")
        for i in range(10):
            g.event(f"leaf{i:02d}")
            g.entity(f"Leaf{i:02d}", on=f"leaf{i:02d}")
            g.edge("Centre", "involves", f"Leaf{i:02d}", on="centre")
        return g

    def test_fanout_per_entity_is_capped(self, db: sqlite3.Connection, star: _Graph) -> None:
        limits = replace(DEFAULT_LIMITS, max_fanout_per_entity=3, max_hops=1)
        found, stats = walk_from_events(db, [star.events["centre"]], limits=limits)
        assert len(found) <= 3
        assert stats.truncated is True

    def test_total_event_budget_is_capped(self, db: sqlite3.Connection, star: _Graph) -> None:
        limits = replace(DEFAULT_LIMITS, max_events_total=4, max_fanout_per_entity=10, max_hops=1)
        found, _ = walk_from_events(db, [star.events["centre"]], limits=limits)
        assert len(found) == 4

    def test_entity_budget_is_capped(self, db: sqlite3.Connection, star: _Graph) -> None:
        limits = replace(DEFAULT_LIMITS, max_entities_total=4, max_fanout_per_entity=10)
        _found, stats = walk_from_events(db, [star.events["centre"]], limits=limits)
        assert stats.entities_discovered <= 4
        assert stats.truncated is True

    def test_events_per_entity_is_capped(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("centre")
        g.event("neighbour_home")
        g.entity("Centre", on="centre", kind="project")
        g.entity("Neighbour", on="neighbour_home")
        g.edge("Centre", "involves", "Neighbour", on="centre")
        for i in range(6):
            g.event(f"extra{i}")
            g.mention("Neighbour", on=f"extra{i}")
        limits = replace(DEFAULT_LIMITS, max_events_per_entity=2, max_hops=1)
        found, _ = walk_from_events(db, [g.events["centre"]], limits=limits)
        assert len(found) == 2

    def test_the_shipped_defaults_are_all_bounded(self) -> None:
        for field, value in vars(DEFAULT_LIMITS).items():
            if field == "min_edge_confidence":
                continue
            assert isinstance(value, int) and value > 0, f"{field} must be a positive ceiling"


class TestExpansionSurvivesAFullFetchWindow:
    """Regression: the candidate pool must not be cut back before diversity runs.

    ``_expand_via_graph`` used to return ``(direct + walked)[:fetch_n]``. Since
    the direct arms normally fill the whole fetch window, that slice discarded
    every graph candidate before MMR could look at one — the expansion was a
    no-op in exactly the situation it was built for. These tests pin the two
    halves of the fix: the pool survives, and a graph-only candidate really can
    reach the served result.
    """

    @pytest.fixture
    def crowded(self, db: sqlite3.Connection) -> _Graph:
        """Enough near-identical direct hits to fill a small fetch window, plus
        one genuinely different record two hops away."""
        g = _Graph(db)
        for i in range(6):
            g.event(
                f"direct{i}",
                "Session handoff: Zentrale deployed, dashboard verified, router checked",
            )
        g.event("bridge", "the memory work and who is on it")
        g.event("far", "Amazon advertising loss of seventy euro per day on the headband")
        g.entity("Anchor", on="direct0", kind="project")
        for i in range(1, 6):
            g.mention("Anchor", on=f"direct{i}")
        g.entity("Bridge", on="bridge")
        g.entity("Faraway", on="far")
        g.edge("Anchor", "involves", "Bridge", on="direct0")
        g.edge("Bridge", "involves", "Faraway", on="bridge")
        return g

    def test_walk_returns_candidates_even_when_direct_hits_are_plentiful(
        self, db: sqlite3.Connection, crowded: _Graph
    ) -> None:
        direct = [crowded.events[f"direct{i}"] for i in range(6)]
        found, stats = walk_from_events(db, direct)
        hashes = {e.content_hash for e in found}
        assert crowded.events["far"].content_hash in hashes, "the 2-hop record must be reachable"
        assert stats.hops_used == 2

    def test_the_pool_handed_to_diversity_keeps_the_graph_candidate(
        self, db: sqlite3.Connection, crowded: _Graph
    ) -> None:
        """Mirrors what the recall path does: expand, then diversify. With the
        old slice the graph candidate was gone before this point."""
        direct = [crowded.events[f"direct{i}"] for i in range(6)]
        fetch_n = len(direct)  # the window is already full
        found, _ = walk_from_events(db, direct)
        pool = direct + found
        assert len(pool) > fetch_n, "the pool must grow past the fetch window"
        assert crowded.events["far"].content_hash in {e.content_hash for e in pool}

        diversified = mmr_rerank(pool, limit=fetch_n)
        assert crowded.events["far"].content_hash in {e.content_hash for e in diversified}, (
            "a distinct graph candidate must beat a fifth copy of the same handoff"
        )

    def test_a_full_window_of_distinct_hits_is_not_displaced(self, db: sqlite3.Connection) -> None:
        """The counterpart guarantee: when the direct hits are all different,
        a graph candidate does NOT push one of them out."""
        g = _Graph(db)
        texts = [
            "amazon advertising spend on the headband listing",
            "mounjaro dosage and the pharmacy appointment",
            "notion database migration for the invoice archive",
            "ollama model storage moved to the internal ssd",
        ]
        for i, text in enumerate(texts):
            g.event(f"direct{i}", text)
        g.event("far", "a completely unrelated neighbour record")
        g.entity("Anchor", on="direct0", kind="project")
        g.entity("Faraway", on="far")
        g.edge("Anchor", "involves", "Faraway", on="direct0")

        direct = [g.events[f"direct{i}"] for i in range(4)]
        found, _ = walk_from_events(db, direct)
        diversified = mmr_rerank(direct + found, limit=len(direct))
        assert [e.content_hash for e in diversified] == [e.content_hash for e in direct]


class TestDeterminism:
    def test_the_same_vault_yields_the_same_walk(
        self, db: sqlite3.Connection, chain: _Graph
    ) -> None:
        first, stats_a = walk_from_events(db, [chain.events["e_a"]])
        second, stats_b = walk_from_events(db, [chain.events["e_a"]])
        assert [e.id for e in first] == [e.id for e in second]
        assert stats_a == stats_b

    def test_truncation_picks_the_same_subset_every_time(self, db: sqlite3.Connection) -> None:
        """The fanout cap must cut reproducibly, not by row order."""
        g = _Graph(db)
        g.event("centre")
        g.entity("Centre", on="centre", kind="project")
        for i in range(10):
            g.event(f"leaf{i:02d}")
            g.entity(f"Leaf{i:02d}", on=f"leaf{i:02d}")
            # Distinct confidences make the intended ordering observable.
            g.edge("Centre", "involves", f"Leaf{i:02d}", on="centre", confidence=0.5 + i / 100)
        limits = replace(DEFAULT_LIMITS, max_fanout_per_entity=3, max_hops=1)
        runs = [
            [e.content_hash for e in walk_from_events(db, [g.events["centre"]], limits=limits)[0]]
            for _ in range(3)
        ]
        assert runs[0] == runs[1] == runs[2]

    def test_highest_confidence_edges_are_followed_first(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("centre")
        g.entity("Centre", on="centre", kind="project")
        for i in range(5):
            g.event(f"leaf{i}")
            g.entity(f"Leaf{i}", on=f"leaf{i}")
            g.edge("Centre", "involves", f"Leaf{i}", on="centre", confidence=0.5 + i / 10)
        limits = replace(DEFAULT_LIMITS, max_fanout_per_entity=1, max_hops=1)
        found, _ = walk_from_events(db, [g.events["centre"]], limits=limits)
        assert [e.content_hash for e in found] == [g.events["leaf4"].content_hash]


class TestEmptyAndDegenerate:
    def test_no_events_in_no_walk_out(self, db: sqlite3.Connection) -> None:
        found, stats = walk_from_events(db, [])
        assert found == []
        assert stats.seeds == 0
        assert stats.events_found == 0

    def test_an_isolated_entity_finds_nothing(self, db: sqlite3.Connection) -> None:
        g = _Graph(db)
        g.event("alone")
        g.entity("Solo", on="alone")
        found, stats = walk_from_events(db, [g.events["alone"]])
        assert found == []
        assert stats.seeds == 1
        assert stats.entities_discovered == 1

    def test_a_cycle_terminates(self, db: sqlite3.Connection) -> None:
        """A ↔ B and back: the visited set, not the hop counter, must stop it."""
        g = _Graph(db)
        g.event("e_a")
        g.event("e_b")
        g.entity("Anna", on="e_a")
        g.entity("Bruno", on="e_b")
        g.edge("Anna", "knows", "Bruno", on="e_a")
        g.edge("Bruno", "knows", "Anna", on="e_b")
        found, stats = walk_from_events(db, [g.events["e_a"]])
        assert [e.content_hash for e in found] == [g.events["e_b"].content_hash]
        assert stats.entities_discovered == 2

    def test_walk_limits_are_frozen(self) -> None:
        with pytest.raises((AttributeError, TypeError)):
            DEFAULT_LIMITS.max_hops = 9  # type: ignore[misc]

    def test_limits_can_be_narrowed_by_a_caller(self) -> None:
        narrowed = WalkLimits(max_hops=1, max_seeds=2)
        assert narrowed.max_hops == 1
        assert narrowed.max_seeds == 2

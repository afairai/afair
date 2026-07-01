"""ADR-0003 Phase 1 — kind-registry tests.

Covers the registry substrate (seed idempotency, I2 triggers, the
``kind_current_v1`` view), latest-row-wins resolution over
rename/merge/deprecate/restore chains, and the three former enum sites
(canonicalizer normalization, correction validation, extractor tool
schema) now reading from the registry with the bootstrap-seven fallback.

Phase 1 is behavior-preserving: the live kind set is still exactly the
seven, so several tests here are byte-identity checks against the
pre-registry behavior.

Like test_entities.py, nothing below the helpers is mocked — real
SQLite, real triggers, real view.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from ulid import ULID

from afair.agents.entity_canonicalizer import _normalize_kind
from afair.agents.prompts import EXTRACTOR_TOOL_SCHEMA, extractor_tool_schema
from afair.substrate import (
    BOOTSTRAP_KIND_SLUGS,
    live_kind_slugs,
    live_kinds,
    open_db,
    resolve_kind_batch,
    resolve_kind_slug,
    resolve_to_live_kind,
    seed_bootstrap_kinds,
    write_entity,
    write_event,
)
from afair.substrate.kinds import BOOTSTRAP_CREATED_BY

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

THE_SEVEN = ("person", "organization", "place", "project", "product", "concept", "other")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


def _register_kind(conn: sqlite3.Connection, slug: str) -> None:
    """Insert a non-bootstrap registry row (what a Phase-5 apply will do)."""
    with conn:
        conn.execute(
            """
            INSERT INTO kind_registry (
                id, slug, label, description, created_at, created_by, source_event_id
            ) VALUES (?, ?, ?, NULL, ?, 'test', NULL)
            """,
            (f"kind:{slug}", slug, slug.title(), "2026-07-01T00:00:00+00:00"),
        )


_REVISION_COUNTER = iter(range(10_000))


def _revise(
    conn: sqlite3.Connection,
    *,
    action: str,
    from_slug: str | None = None,
    to_slug: str | None = None,
) -> None:
    """Append a kind_revisions row with a strictly increasing revised_at, so
    latest-row-wins ordering is deterministic in tests."""
    seq = next(_REVISION_COUNTER)
    revised_at = f"2026-07-01T00:{seq // 60 % 60:02d}:{seq % 60:02d}.{seq:06d}+00:00"
    with conn:
        conn.execute(
            """
            INSERT INTO kind_revisions (
                id, action, from_slug, to_slug, detail,
                revised_at, revised_by, reason, source_event_id
            ) VALUES (?, ?, ?, ?, NULL, ?, 'test', 'test revision', NULL)
            """,
            (str(ULID()), action, from_slug, to_slug, revised_at),
        )


# ── seed ──────────────────────────────────────────────────────────────────


def test_open_db_seeds_exactly_the_seven(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT id, slug, created_by FROM kind_registry ORDER BY rowid").fetchall()
    assert tuple(r["slug"] for r in rows) == THE_SEVEN
    assert all(r["created_by"] == BOOTSTRAP_CREATED_BY for r in rows)
    assert all(r["id"] == f"kind:{r['slug']}" for r in rows)


def test_seed_is_idempotent(db: sqlite3.Connection) -> None:
    # First seed happened inside open_db; a second explicit seed is a no-op.
    assert seed_bootstrap_kinds(db) == 0
    count = db.execute("SELECT COUNT(*) FROM kind_registry").fetchone()[0]
    assert count == 7


def test_reopen_is_idempotent(tmp_path: Path) -> None:
    conn = open_db(tmp_path)
    first = conn.execute("SELECT id, created_at FROM kind_registry ORDER BY slug").fetchall()
    conn.close()
    conn = open_db(tmp_path)
    second = conn.execute("SELECT id, created_at FROM kind_registry ORDER BY slug").fetchall()
    conn.close()
    # Same rows, same timestamps — the re-open did not re-write anything.
    assert [tuple(r) for r in first] == [tuple(r) for r in second]
    assert len(second) == 7


def test_existing_vault_gains_registry_without_touching_entities(tmp_path: Path) -> None:
    """A vault created before ADR-0003 has no registry tables. Opening it
    with the new code creates + seeds them and leaves every entity byte-
    identical. Emulated by dropping the new DDL from a fresh vault (DROP
    is DDL — the I2 row triggers guard UPDATE/DELETE, exactly like a
    pre-upgrade vault that simply never had the tables)."""
    conn = open_db(tmp_path)
    ev = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "Sajinth"}
    )
    write_entity(
        conn,
        canonical_name="Sajinth",
        kind="person",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.9,
    )
    before = conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
    with conn:
        conn.execute("DROP VIEW kind_current_v1")
        conn.execute("DROP TABLE kind_observations")
        conn.execute("DROP TABLE kind_revisions")
        conn.execute("DROP TABLE kind_registry")
    conn.close()

    conn = open_db(tmp_path)
    try:
        assert live_kind_slugs(conn) == THE_SEVEN
        after = conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
    finally:
        conn.close()


# ── live set + fallback ───────────────────────────────────────────────────


def test_live_kind_slugs_returns_the_seven_in_order(db: sqlite3.Connection) -> None:
    assert live_kind_slugs(db) == THE_SEVEN
    assert live_kind_slugs(db) == BOOTSTRAP_KIND_SLUGS


def test_live_kind_slugs_fallback_without_conn() -> None:
    assert live_kind_slugs(None) == BOOTSTRAP_KIND_SLUGS


def test_live_kind_slugs_fallback_on_bare_db_without_registry() -> None:
    # A raw connection with no registry tables (e.g. a unit-test DB that
    # never ran init_db) must fall back, not raise.
    bare = sqlite3.connect(":memory:")
    try:
        assert live_kind_slugs(bare) == BOOTSTRAP_KIND_SLUGS
    finally:
        bare.close()


def test_live_kind_slugs_fallback_on_unseeded_registry(db: sqlite3.Connection) -> None:
    # Every kind deprecated → empty live set → fallback (an ontology can
    # never resolve to "no kinds at all").
    for slug in THE_SEVEN:
        _revise(db, action="deprecate", from_slug=slug)
    assert live_kind_slugs(db) == BOOTSTRAP_KIND_SLUGS


def test_deprecate_removes_from_live_set_and_restore_revives(db: sqlite3.Connection) -> None:
    _revise(db, action="deprecate", from_slug="concept")
    assert "concept" not in live_kind_slugs(db)
    assert len(live_kind_slugs(db)) == 6
    _revise(db, action="restore", from_slug="concept")
    assert live_kind_slugs(db) == THE_SEVEN


def test_kind_current_v1_view_matches_python_liveness(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    _revise(db, action="deprecate", from_slug="concept")
    view = {
        r["slug"]: bool(r["is_live"])
        for r in db.execute("SELECT slug, is_live FROM kind_current_v1").fetchall()
    }
    python_live = {k.slug for k in live_kinds(db)}
    assert {s for s, is_live in view.items() if is_live} == python_live
    assert view["organization"] is False
    assert view["concept"] is False
    assert view["company"] is True


# ── resolution ────────────────────────────────────────────────────────────


def test_resolve_is_identity_for_seeded_kinds(db: sqlite3.Connection) -> None:
    for slug in THE_SEVEN:
        assert resolve_kind_slug(db, slug) == slug


def test_resolve_is_identity_for_unknown_slug(db: sqlite3.Connection) -> None:
    assert resolve_kind_slug(db, "research_paper") == "research_paper"


def test_resolve_follows_rename_and_merge_chain(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _register_kind(db, "enterprise")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    _revise(db, action="merge", from_slug="company", to_slug="enterprise")
    assert resolve_kind_slug(db, "organization") == "enterprise"
    assert resolve_kind_slug(db, "company") == "enterprise"
    assert "organization" not in live_kind_slugs(db)
    assert "enterprise" in live_kind_slugs(db)


def test_restore_terminates_the_chain(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    assert resolve_kind_slug(db, "organization") == "company"
    # Reversal is a compensating append (I7), never a row mutation.
    _revise(db, action="restore", from_slug="organization")
    assert resolve_kind_slug(db, "organization") == "organization"
    assert "organization" in live_kind_slugs(db)


def test_resolve_latest_row_wins(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _register_kind(db, "firm")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    _revise(db, action="rename", from_slug="organization", to_slug="firm")
    assert resolve_kind_slug(db, "organization") == "firm"


def test_resolve_survives_a_cycle(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    _revise(db, action="rename", from_slug="company", to_slug="organization")
    # a → b → a: the walk stops when it re-sees a slug instead of looping.
    assert resolve_kind_slug(db, "organization") in {"organization", "company"}


def test_resolve_depth_cap_matches_resolve_canonical(db: sqlite3.Connection) -> None:
    for i in range(21):
        _register_kind(db, f"k{i}")
    for i in range(20):
        _revise(db, action="rename", from_slug=f"k{i}", to_slug=f"k{i + 1}")
    # 16 hops from k0 lands on k16; the cap stops the walk there.
    assert resolve_kind_slug(db, "k0") == "k16"


def test_resolve_kind_batch_matches_per_slug(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    slugs = ["person", "organization", "unknown", "person"]
    batch = resolve_kind_batch(db, slugs)
    assert batch == {
        "person": "person",
        "organization": "company",
        "unknown": "unknown",
    }


def test_resolve_to_live_kind(db: sqlite3.Connection) -> None:
    assert resolve_to_live_kind(db, "person") == "person"
    assert resolve_to_live_kind(db, "banana") is None
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    assert resolve_to_live_kind(db, "organization") == "company"
    _revise(db, action="deprecate", from_slug="concept")
    assert resolve_to_live_kind(db, "concept") is None
    # Fallback path without a connection: bootstrap membership only.
    assert resolve_to_live_kind(None, "person") == "person"
    assert resolve_to_live_kind(None, "banana") is None


# ── I2 append-only triggers ───────────────────────────────────────────────


def test_kind_registry_rejects_update_and_delete(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE kind_registry SET label = 'Human' WHERE slug = 'person'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM kind_registry WHERE slug = 'person'")


def test_kind_revisions_rejects_update_and_delete(db: sqlite3.Connection) -> None:
    _revise(db, action="deprecate", from_slug="concept")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE kind_revisions SET reason = 'edited'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM kind_revisions")


def test_kind_observations_rejects_update_and_delete(db: sqlite3.Connection) -> None:
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    entity = write_entity(
        db,
        canonical_name="research_paper_x",
        kind="concept",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.5,
    )
    with db:
        db.execute(
            """
            INSERT INTO kind_observations (
                id, raw_kind, normalized_slug, entity_id, event_id, observed_at, observed_by
            ) VALUES (?, 'research_paper', 'concept', ?, ?, '2026-07-01T00:00:00+00:00', 't')
            """,
            (str(ULID()), entity.id, ev.id),
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE kind_observations SET raw_kind = 'edited'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM kind_observations")


# ── the three former enum sites (behavior preserved) ──────────────────────


def test_normalize_kind_parity_with_and_without_registry(db: sqlite3.Connection) -> None:
    """Byte-identical to the pre-registry variant map, both through the
    registry (conn) and through the fallback (no conn)."""
    cases = {
        # the seven pass through
        **{slug: slug for slug in THE_SEVEN},
        # case / whitespace
        "  Person ": "person",
        "ORGANIZATION": "organization",
        # variants
        "org": "organization",
        "organisation": "organization",
        "people": "person",
        "human": "person",
        "individual": "person",
        "places": "place",
        "location": "place",
        "city": "place",
        "country": "place",
        # unknown → other
        "banana": "other",
        "research_paper": "other",
        "": "other",
    }
    for raw, expected in cases.items():
        assert _normalize_kind(raw, db) == expected, raw
        assert _normalize_kind(raw) == expected, raw


def test_normalize_kind_follows_registry_revisions(db: sqlite3.Connection) -> None:
    # Once a revision lands (a later phase), normalization tracks it — the
    # point of the registry: a renamed slug is not squashed into 'other'.
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    assert _normalize_kind("organization", db) == "company"
    assert _normalize_kind("org", db) == "company"


def test_extractor_tool_schema_renders_the_seven(db: sqlite3.Connection) -> None:
    static_enum = EXTRACTOR_TOOL_SCHEMA["properties"]["entities"]["items"]["properties"]["type"][
        "enum"
    ]
    assert static_enum == list(THE_SEVEN)
    rendered = extractor_tool_schema(db)
    rendered_enum = rendered["properties"]["entities"]["items"]["properties"]["type"]["enum"]
    # Behavior preserved: registry render == static constant, and the
    # fallback render (no conn) too.
    assert rendered_enum == static_enum
    fallback = extractor_tool_schema(None)
    assert fallback == EXTRACTOR_TOOL_SCHEMA
    # Everything except the enum source is the same schema.
    assert rendered == EXTRACTOR_TOOL_SCHEMA


def test_extractor_tool_schema_is_a_deep_copy(db: sqlite3.Connection) -> None:
    rendered = extractor_tool_schema(db)
    rendered["properties"]["entities"]["items"]["properties"]["type"]["enum"].append("mutant")
    static_enum = EXTRACTOR_TOOL_SCHEMA["properties"]["entities"]["items"]["properties"]["type"][
        "enum"
    ]
    assert "mutant" not in static_enum


def test_extractor_tool_schema_tracks_registry_revisions(db: sqlite3.Connection) -> None:
    _register_kind(db, "company")
    _revise(db, action="rename", from_slug="organization", to_slug="company")
    enum = extractor_tool_schema(db)["properties"]["entities"]["items"]["properties"]["type"][
        "enum"
    ]
    assert "organization" not in enum
    assert "company" in enum

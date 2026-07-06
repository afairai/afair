"""ADR-0003 Phase 2 — identity/kind decoupling regression suite.

Kind used to be baked into entity identity
(``entity:<sha256(name|kind)>``), which gave homonym separation for free:
"Apple" the organization and "apple" the concept could never collide
because their hashes differed. Phase 2 removes kind from the hash (v2
name-first IDs) and makes kind mutable-by-append
(``entity_kind_assignments`` + the ``entity_current_kind_v1`` resolution
view). This suite is the safety net for exactly what that trade risks:

- the known homonym cases (Apple/apple, Sajinth-from-elvah vs
  Sajinth-from-Athara) must stay DISTINCT — the Stage-1 kind-agreement
  guard replaces the hash separation;
- the same-name + same-kind fast path must stay a no-LLM exact link;
- existing v1 entities must resolve byte-identically with ZERO backfill;
- v2 IDs must be deterministic under replay, ordinals minted only on a
  deliberate split;
- a retype must be ONE assignment row with the identity unchanged;
- the new tables must be append-only (I2).

Like test_entities.py, nothing below the helpers is mocked — real SQLite,
real triggers, real view; only the LLM is stubbed.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMError, LLMResult
from afair.settings import Settings
from afair.substrate import (
    assign_entity_kind,
    entity_id,
    entity_id_v2,
    iter_mentions_for_event,
    next_disambiguator,
    open_db,
    read_entity_by_id,
    resolve_canonical,
    resolve_entity_kind,
    resolve_entity_kind_batch,
    write_entity,
    write_event,
)

if TYPE_CHECKING:
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


def _write_event_with_extraction(
    conn: sqlite3.Connection, *, text: str, entities: list[dict[str, str]]
) -> str:
    """Event + extractor interpretation; returns the content_hash."""
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": text}
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


def _source_event_id(conn: sqlite3.Connection, text: str = "seed") -> str:
    return write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    ).id


def _seed_v1_entity(conn: sqlite3.Connection, *, name: str, kind: str, source_event_id: str) -> str:
    """Insert an entity row under the pre-Phase-2 v1 identity scheme, exactly
    as an existing vault carries it: kind-in-hash ID, no entity_identities
    row, no assignment rows."""
    eid = entity_id(name, kind)
    with conn:
        conn.execute(
            """
            INSERT INTO entities (
                id, canonical_name, kind, created_at, created_by,
                confidence, source_event_id
            ) VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 'pre-phase2', 0.8, ?)
            """,
            (eid, name, kind, source_event_id),
        )
    return eid


# ── the Apple/apple homonym: the free lunch the guard must replace ─────────


def test_apple_org_and_apple_concept_stay_distinct_with_llm(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Apple" (organization) and "apple" (concept) must NOT collapse. The
    kind disagreement blocks the confidence-1.0 auto-link; the LLM homonym
    judge rules "none of these"; a kind-forked distinct identity is minted."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))

    h_org = _write_event_with_extraction(
        db, text="Apple shipped Vision Pro", entities=[{"name": "Apple", "type": "organization"}]
    )
    h_concept = _write_event_with_extraction(
        db, text="an apple a day", entities=[{"name": "apple", "type": "concept"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_created"] == 2
    assert stats["entities_matched_exact"] == 0
    org_id = iter_mentions_for_event(db, h_org)[0].entity_id
    concept_id = iter_mentions_for_event(db, h_concept)[0].entity_id
    assert org_id != concept_id
    assert resolve_entity_kind(db, org_id) == "organization"
    assert resolve_entity_kind(db, concept_id) == "concept"


def test_apple_homonyms_stay_distinct_even_without_llm(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must fail SAFE: with the LLM down, a cross-kind same-name
    mention still becomes a distinct entity — never a 1.0 auto-link onto the
    other kind (matching what the v1 kind-in-ID scheme did)."""
    _no_sleep(monkeypatch)

    def _down(**_: Any) -> LLMResult:
        msg = "503 from upstream"
        raise LLMError(msg)

    monkeypatch.setattr(ec, "call_tool", _down)

    h_org = _write_event_with_extraction(
        db, text="Apple shipped Vision Pro", entities=[{"name": "Apple", "type": "organization"}]
    )
    h_concept = _write_event_with_extraction(
        db, text="an apple a day", entities=[{"name": "apple", "type": "concept"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_created"] == 2
    org_id = iter_mentions_for_event(db, h_org)[0].entity_id
    concept_id = iter_mentions_for_event(db, h_concept)[0].entity_id
    assert org_id != concept_id


def test_apple_homonyms_stay_distinct_against_a_v1_vault(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee when the existing "Apple" is a pre-Phase-2 v1
    entity: the new concept mention forks a distinct identity instead of
    linking onto the v1 organization."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(ec, "call_tool", _llm_returns(None, confidence=0.95))
    v1_org = _seed_v1_entity(
        db, name="Apple", kind="organization", source_event_id=_source_event_id(db)
    )

    h_concept = _write_event_with_extraction(
        db, text="an apple a day", entities=[{"name": "apple", "type": "concept"}]
    )
    EntityCanonicalizer().run(db, settings)

    concept_id = iter_mentions_for_event(db, h_concept)[0].entity_id
    assert concept_id != v1_org
    assert resolve_entity_kind(db, v1_org) == "organization"
    assert resolve_entity_kind(db, concept_id) == "concept"


# ── the Sajinth case: same name, same kind, different people ────────────────


def test_sajinth_from_elvah_and_sajinth_from_athara_stay_distinct(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two people named Sajinth (person + person). Same name AND same kind —
    the case v1 could not even represent (the hashes collided). The LLM
    judge rules "none of these" for the second one; a deliberate homonym
    split mints the next disambiguator ordinal and the two stay distinct."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    h_elvah = _write_event_with_extraction(
        db, text="Sajinth from elvah pinged me", entities=[{"name": "Sajinth", "type": "person"}]
    )
    EntityCanonicalizer().run(db, settings)
    elvah_id = iter_mentions_for_event(db, h_elvah)[0].entity_id

    # Second, different Sajinth: one same-name agreeing candidate exists, so
    # normally the exact fast path links — the split happens when the judge
    # is explicitly asked. Emulate the deliberate split verdict at the
    # substrate level (the canonicalizer path for it is exercised in
    # test_second_sajinth_mention_is_not_auto_linked below).
    athara = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="entity_canonicalizer:v0",
        source_event_id=_source_event_id(db, "Sajinth from Athara"),
        confidence=0.5,
        split_homonym=True,
    )
    assert athara.id != elvah_id
    assert resolve_entity_kind(db, athara.id) == "person"
    assert resolve_entity_kind(db, elvah_id) == "person"
    ordinals = {
        r["entity_id"]: r["disambiguator"]
        for r in db.execute(
            "SELECT entity_id, disambiguator FROM entity_identities WHERE name_lower = 'sajinth'"
        ).fetchall()
    }
    assert ordinals == {elvah_id: "0", athara.id: "1"}


def test_second_sajinth_mention_is_not_auto_linked(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once two same-name same-kind Sajinths exist, a new "Sajinth" mention
    must NOT exact-link at confidence 1.0 to either — the ambiguity goes to
    the LLM with BOTH in the menu, and its pick wins."""
    _no_sleep(monkeypatch)
    ev = _source_event_id(db)
    first = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="t",
        source_event_id=ev,
        confidence=0.5,
    )
    second = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="t",
        source_event_id=ev,
        confidence=0.5,
        split_homonym=True,
    )
    assert first.id != second.id

    seen_candidates: list[set[str]] = []

    def _pick_second(**kw: Any) -> LLMResult:
        # Record the menu (both Sajinths must be offered), pick the second.
        seen_candidates.append(
            {line.split("id: ")[1] for line in kw["user"].splitlines() if "id: " in line}
        )
        return LLMResult(
            data={"matched_entity_id": second.id, "reason": "Athara ctx", "confidence": 0.9},
            model=kw["model"],
            raw="",
        )

    monkeypatch.setattr(ec, "call_tool", _pick_second)
    h = _write_event_with_extraction(
        db, text="Sajinth review call (Athara)", entities=[{"name": "Sajinth", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_matched_exact"] == 0  # no 1.0 auto-link
    assert stats["entities_matched_llm"] == 1
    assert seen_candidates and seen_candidates[0] == {first.id, second.id}
    mention = iter_mentions_for_event(db, h)[0]
    assert mention.entity_id == second.id
    assert mention.match_method == "llm"


# ── the fast path stays fast ────────────────────────────────────────────────


def test_same_name_same_kind_still_links_exact_without_llm(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kind agreement on a single same-name candidate keeps today's fast
    path: exact link at confidence 1.0, zero LLM calls."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    h1 = _write_event_with_extraction(
        db, text="Sajinth runs Athara", entities=[{"name": "Sajinth", "type": "person"}]
    )
    h2 = _write_event_with_extraction(
        db, text="Sajinth shipped a feature", entities=[{"name": "Sajinth", "type": "person"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_matched_exact"] == 1
    assert stats["llm_calls"] == 0
    m1, m2 = iter_mentions_for_event(db, h1)[0], iter_mentions_for_event(db, h2)[0]
    assert m1.entity_id == m2.entity_id
    # Whichever event the canonicalizer processes first in the batch creates the
    # entity ('new'); the other exact-links to it at confidence 1.0. The order
    # between two same-millisecond writes is not guaranteed (ULIDs aren't
    # monotonic within a ms), so assert the symmetric invariant, not a fixed
    # direction — the earlier `m2.match_method == "exact"` flaked on fast runners.
    assert {m1.match_method, m2.match_method} == {"new", "exact"}
    exact_mention = m2 if m2.match_method == "exact" else m1
    assert exact_mention.confidence == 1.0


def test_fast_path_links_to_a_v1_entity(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1/v2 coexistence on the read path: a new mention exact-links onto an
    existing v1 entity exactly as before — no v2 duplicate appears."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    v1_id = _seed_v1_entity(
        db, name="Athara", kind="organization", source_event_id=_source_event_id(db)
    )
    h = _write_event_with_extraction(
        db, text="Athara raised a round", entities=[{"name": "Athara", "type": "organization"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_matched_exact"] == 1
    assert stats["entities_created"] == 0
    assert iter_mentions_for_event(db, h)[0].entity_id == v1_id


# ── retype: one assignment row, identity unchanged ──────────────────────────


def test_retype_is_one_assignment_row_and_id_is_stable(db: sqlite3.Connection) -> None:
    ev = _source_event_id(db)
    e = write_entity(
        db,
        canonical_name="Clario",
        kind="product",
        created_by="t",
        source_event_id=ev,
        confidence=0.8,
    )
    assert resolve_entity_kind(db, e.id) == "product"

    assign_entity_kind(
        db,
        entity_id=e.id,
        kind_slug="project",
        assigned_by="operator",
        reason="operator re-typed",
        source_event_id=ev,
    )
    # ONE row; resolved kind changed; identity + canonical resolution stable.
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 1
    assert resolve_entity_kind(db, e.id) == "project"
    assert resolve_canonical(db, e.id) == e.id
    assert read_entity_by_id(db, e.id).id == e.id
    # The stored column is untouched (I2) — only the resolution overlay moved.
    assert read_entity_by_id(db, e.id).kind == "product"


def test_retype_revert_is_a_second_assignment(db: sqlite3.Connection) -> None:
    ev = _source_event_id(db)
    e = write_entity(
        db,
        canonical_name="Clario",
        kind="product",
        created_by="t",
        source_event_id=ev,
        confidence=0.8,
    )
    assign_entity_kind(
        db, entity_id=e.id, kind_slug="project", assigned_by="operator", reason="retype"
    )
    assign_entity_kind(
        db, entity_id=e.id, kind_slug="product", assigned_by="operator", reason="revert"
    )
    # Latest row wins; nothing was un-written (I7).
    assert resolve_entity_kind(db, e.id) == "product"
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 2


def test_retype_works_on_a_v1_entity_without_identity_change(db: sqlite3.Connection) -> None:
    """The whole point of the decoupling: a v1 entity (kind baked into its
    hash) is retypeable by ONE assignment row — the ID stays the v1 ID."""
    v1_id = _seed_v1_entity(
        db, name="maxime.team", kind="person", source_event_id=_source_event_id(db)
    )
    assign_entity_kind(
        db, entity_id=v1_id, kind_slug="product", assigned_by="operator", reason="domain name"
    )
    assert resolve_entity_kind(db, v1_id) == "product"
    assert resolve_canonical(db, v1_id) == v1_id
    assert read_entity_by_id(db, v1_id).id == v1_id
    assert db.execute("SELECT COUNT(*) FROM entity_merges").fetchone()[0] == 0


def test_recall_overlay_serves_the_resolved_kind(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recall's canonical_entities payload carries the CURRENT kind — a
    retype is visible on the next recall with zero re-canonicalization."""
    from afair.mcp import handlers
    from afair.mcp.context import ServerContext, set_context
    from afair.substrate import read_event_by_id, write_entity_mention

    set_context(
        ServerContext(
            db=db,
            vault_dir=tmp_path,
            inline_text_max_bytes=64 * 1024,
            embedding_dim=1024,
            embedding_model="stub",
            surprise_context_window=20,
            semantic_recall_enabled=False,
        )
    )
    ev = write_event(
        db, origin="user", kind="remember", payload={"content_type": "text", "text": "Clario"}
    )
    e = write_entity(
        db,
        canonical_name="Clario",
        kind="product",
        created_by="t",
        source_event_id=ev.id,
        confidence=0.8,
    )
    write_entity_mention(
        db,
        entity_id=e.id,
        event_id=ev.id,
        event_hash=ev.content_hash,
        surface_form="Clario",
        canonicalized_by="t",
        match_method="new",
        confidence=0.8,
    )
    event = read_event_by_id(db, ev.id)
    assert event is not None

    before = handlers._build_entity_overlay([event], db)
    assert before[ev.content_hash]["canonical_entities"][0]["kind"] == "product"

    assign_entity_kind(
        db, entity_id=e.id, kind_slug="project", assigned_by="operator", reason="retype"
    )
    after = handlers._build_entity_overlay([event], db)
    entry = after[ev.content_hash]["canonical_entities"][0]
    assert entry["kind"] == "project"
    assert entry["id"] == e.id  # identity unchanged


# ── v1 back-compat: zero backfill, byte-identical vaults ────────────────────


def test_v1_entity_with_no_assignment_resolves_to_its_stored_kind(
    db: sqlite3.Connection,
) -> None:
    v1_id = _seed_v1_entity(db, name="Sajinth", kind="person", source_event_id=_source_event_id(db))
    # The COALESCE fallback IS the backfill: no assignment row exists.
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
    assert resolve_entity_kind(db, v1_id) == "person"
    batch = resolve_entity_kind_batch(db, [v1_id])
    assert batch == {v1_id: "person"}


def test_existing_vault_reopens_byte_identical_except_new_tables(tmp_path: Path) -> None:
    """A pre-Phase-2 vault has neither the new tables nor the view. Opening
    it with the new code creates them (empty) and leaves every existing row
    byte-identical — zero backfill, zero rewrites (I3). Emulated by dropping
    the new DDL from a fresh vault, the test_kinds.py pattern."""
    conn = open_db(tmp_path)
    ev = _source_event_id(conn, "pre-upgrade event")
    _seed_v1_entity(conn, name="Sajinth", kind="person", source_event_id=ev)
    _seed_v1_entity(conn, name="Apple", kind="organization", source_event_id=ev)
    before_entities = conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
    before_events = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    with conn:
        conn.execute("DROP VIEW entity_current_kind_v1")
        conn.execute("DROP TABLE entity_kind_assignments")
        conn.execute("DROP TABLE entity_identities")
    conn.close()

    conn = open_db(tmp_path)
    try:
        after_entities = conn.execute("SELECT * FROM entities ORDER BY id").fetchall()
        after_events = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        assert [tuple(r) for r in before_entities] == [tuple(r) for r in after_entities]
        assert [tuple(r) for r in before_events] == [tuple(r) for r in after_events]
        # The new tables exist and are EMPTY — nothing was backfilled.
        assert conn.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM entity_identities").fetchone()[0] == 0
        # And the v1 rows resolve to their original kinds through the view.
        kinds = {
            r["entity_id"]: r["kind_slug"]
            for r in conn.execute("SELECT * FROM entity_current_kind_v1").fetchall()
        }
        assert kinds[entity_id("Sajinth", "person")] == "person"
        assert kinds[entity_id("Apple", "organization")] == "organization"
    finally:
        conn.close()


def test_resolution_pipes_through_the_kind_registry_chain(db: sqlite3.Connection) -> None:
    """Full resolution order: entities.kind → latest assignment → registry
    revision chain. A registry-level rename retypes at read time with ZERO
    per-entity writes."""
    from ulid import ULID

    v1_id = _seed_v1_entity(
        db, name="Athara", kind="organization", source_event_id=_source_event_id(db)
    )
    with db:
        db.execute(
            "INSERT INTO kind_registry (id, slug, label, description, created_at, "
            "created_by, source_event_id) VALUES ('kind:company', 'company', 'Company', "
            "NULL, '2026-07-01T00:00:00+00:00', 'test', NULL)"
        )
        db.execute(
            "INSERT INTO kind_revisions (id, action, from_slug, to_slug, detail, revised_at, "
            "revised_by, reason, source_event_id) VALUES (?, 'rename', 'organization', "
            "'company', NULL, '2026-07-01T00:00:00+00:00', 'test', 'test', NULL)",
            (str(ULID()),),
        )
    assert db.execute("SELECT COUNT(*) FROM entity_kind_assignments").fetchone()[0] == 0
    assert resolve_entity_kind(db, v1_id) == "company"
    assert resolve_entity_kind_batch(db, [v1_id]) == {v1_id: "company"}


# ── v2 determinism ──────────────────────────────────────────────────────────


def _replay_decisions(vault: Path) -> list[str]:
    """One fixed sequence of canonical decisions; returns the minted IDs."""
    conn = open_db(vault)
    try:
        ev = _source_event_id(conn)
        ids = [
            write_entity(
                conn,
                canonical_name="Sajinth",
                kind="person",
                created_by="t",
                source_event_id=ev,
                confidence=0.5,
            ).id,
            # deliberate homonym split: a second, different Sajinth
            write_entity(
                conn,
                canonical_name="Sajinth",
                kind="person",
                created_by="t",
                source_event_id=ev,
                confidence=0.5,
                split_homonym=True,
            ).id,
            # re-encounter of the first: reused, no new identity
            write_entity(
                conn,
                canonical_name="Sajinth",
                kind="person",
                created_by="t",
                source_event_id=ev,
                confidence=0.5,
            ).id,
            write_entity(
                conn,
                canonical_name="Clario",
                kind="project",
                created_by="t",
                source_event_id=ev,
                confidence=0.5,
            ).id,
        ]
    finally:
        conn.close()
    return ids


def test_v2_ids_are_deterministic_under_replay(tmp_path: Path) -> None:
    a = _replay_decisions(tmp_path / "vault_a")
    b = _replay_decisions(tmp_path / "vault_b")
    assert a == b
    # And they are pure functions of (name, ordinal):
    assert a[0] == entity_id_v2("Sajinth", "0")
    assert a[1] == entity_id_v2("Sajinth", "1")
    assert a[2] == a[0]  # the re-encounter reused ordinal 0
    assert a[3] == entity_id_v2("Clario", "0")


def test_disambiguator_increments_only_on_split(db: sqlite3.Connection) -> None:
    ev = _source_event_id(db)
    kwargs: dict[str, Any] = {
        "canonical_name": "Sajinth",
        "kind": "person",
        "created_by": "t",
        "source_event_id": ev,
        "confidence": 0.5,
    }
    assert next_disambiguator(db, "Sajinth") == "0"
    first = write_entity(db, **kwargs)
    assert next_disambiguator(db, "Sajinth") == "1"
    # Plain re-encounters do NOT mint ordinals.
    assert write_entity(db, **kwargs).id == first.id
    assert write_entity(db, **kwargs).id == first.id
    assert next_disambiguator(db, "Sajinth") == "1"
    # Only the deliberate split does.
    second = write_entity(db, **kwargs, split_homonym=True)
    assert second.id != first.id
    assert next_disambiguator(db, "Sajinth") == "2"
    n = db.execute(
        "SELECT COUNT(*) FROM entity_identities WHERE name_lower = 'sajinth'"
    ).fetchone()[0]
    assert n == 2


def test_v2_reuse_is_case_and_whitespace_insensitive(db: sqlite3.Connection) -> None:
    ev = _source_event_id(db)
    a = write_entity(
        db,
        canonical_name="Sajinth",
        kind="person",
        created_by="t",
        source_event_id=ev,
        confidence=0.5,
    )
    b = write_entity(
        db,
        canonical_name="  sajinth ",
        kind="person",
        created_by="t",
        source_event_id=ev,
        confidence=0.5,
    )
    assert a.id == b.id


# ── I2: the new tables are append-only ──────────────────────────────────────


def test_entity_kind_assignments_rejects_update_and_delete(db: sqlite3.Connection) -> None:
    e = write_entity(
        db,
        canonical_name="X",
        kind="concept",
        created_by="t",
        source_event_id=_source_event_id(db),
        confidence=0.5,
    )
    assign_entity_kind(db, entity_id=e.id, kind_slug="product", assigned_by="t", reason="t")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE entity_kind_assignments SET kind_slug = 'person'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM entity_kind_assignments")


def test_entity_identities_rejects_update_and_delete(db: sqlite3.Connection) -> None:
    write_entity(
        db,
        canonical_name="X",
        kind="concept",
        created_by="t",
        source_event_id=_source_event_id(db),
        confidence=0.5,
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE entity_identities SET disambiguator = '9'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM entity_identities")


# ── guard details ───────────────────────────────────────────────────────────


def test_other_acts_as_kind_wildcard_in_the_guard(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0003: 'other' means "the extractor didn't know", not "a different
    thing" — a same-name mention typed 'other' links to the single known
    entity instead of forking a parallel identity (the dedup worker's old
    workload)."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    h1 = _write_event_with_extraction(
        db, text="Clario kickoff", entities=[{"name": "Clario", "type": "project"}]
    )
    h2 = _write_event_with_extraction(
        db, text="something about Clario", entities=[{"name": "Clario", "type": "other"}]
    )
    stats = EntityCanonicalizer().run(db, settings)

    assert stats["entities_created"] == 1
    assert stats["entities_matched_exact"] == 1
    assert (
        iter_mentions_for_event(db, h1)[0].entity_id == iter_mentions_for_event(db, h2)[0].entity_id
    )


def test_retyped_entity_agrees_under_its_new_kind(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard compares the CURRENT resolved kind: after an operator
    retype (product → project), a mention typed 'project' fast-links and a
    mention typed 'product' is treated as the homonym question."""
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        ec, "call_tool", lambda **_: (_ for _ in ()).throw(AssertionError("no LLM expected"))
    )
    ev = _source_event_id(db)
    e = write_entity(
        db,
        canonical_name="Clario",
        kind="product",
        created_by="t",
        source_event_id=ev,
        confidence=0.8,
    )
    assign_entity_kind(
        db, entity_id=e.id, kind_slug="project", assigned_by="operator", reason="retype"
    )

    h = _write_event_with_extraction(
        db, text="Clario sprint review", entities=[{"name": "Clario", "type": "project"}]
    )
    stats = EntityCanonicalizer().run(db, settings)
    assert stats["entities_matched_exact"] == 1
    assert iter_mentions_for_event(db, h)[0].entity_id == e.id

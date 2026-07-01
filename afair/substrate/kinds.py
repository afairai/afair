"""Kind registry — entity kinds as data, not code (ADR-0003 Phase 1).

Invariant I6 demands an emergent ontology: the system must be able to
revise, merge, split, and discard categories based on usage, forever.
That is impossible while the kind set is a hardcoded enum, so this
module moves it into the substrate:

- ``kind_registry`` holds every kind ever registered (append-only, I2);
- ``kind_revisions`` holds the lifecycle history (rename / merge /
  deprecate / restore ... — appended, never edited, I7);
- ``kind_observations`` (written from Phase 3) preserves every raw
  extractor kind proposal that did not resolve to a live kind — the
  usage signal the Schema-Evolver mines;
- the *current* ontology is resolved latest-row-wins at read time,
  mirroring the ``tuner_state`` / ``resolve_canonical`` patterns.

The current seven kinds survive as the bootstrap seed (the "minimal
bootstrap scaffold" I6 permits) — seeded idempotently at every vault
open with ``created_by='bootstrap:v1'``, so both fresh and existing
vaults gain the registry on next open.

Every consumer of the kind set (extractor prompt enum, canonicalizer
normalization, correction validation) reads through :func:`live_kind_slugs`,
which falls back to :data:`BOOTSTRAP_KIND_SLUGS` when the registry is
unavailable (no connection, or a bare test DB without the tables) — so
Phase 1 is behavior-preserving by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel
from ulid import ULID

if TYPE_CHECKING:
    import sqlite3

BOOTSTRAP_CREATED_BY = "bootstrap:v1"
"""``created_by`` stamp for the seeded kinds — bootstrap scaffold, not law."""

# The seven bootstrap kinds, in the canonical order the extractor tool
# schema has always listed them (order preserved so the rendered enum is
# byte-identical to the pre-registry constant). This tuple is the ONLY
# in-code copy of the seven; everything else reads the registry and uses
# this as the unavailable-registry fallback.
BOOTSTRAP_KINDS: tuple[tuple[str, str, str], ...] = (
    ("person", "Person", "A human individual."),
    ("organization", "Organization", "A company, institution, team, or other group."),
    ("place", "Place", "A geographic or physical location."),
    ("project", "Project", "A named undertaking with a goal."),
    ("product", "Product", "A tool, service, site, or other made thing."),
    ("concept", "Concept", "An abstract idea, topic, or term."),
    ("other", "Other", "Anything that fits no other kind."),
)

BOOTSTRAP_KIND_SLUGS: tuple[str, ...] = tuple(slug for slug, _, _ in BOOTSTRAP_KINDS)

# Matches resolve_canonical's depth cap — defends against the impossible
# case of a revision cycle without looping forever.
_RESOLVE_DEPTH_CAP = 16

# A slug whose LATEST revision row carries one of these actions is not
# live: 'deprecate' retires it, 'rename'/'merge' redirect it to a
# successor. Mirrors the kind_current_v1 view in schema.py exactly.
_NOT_LIVE_ACTIONS = frozenset({"deprecate", "rename", "merge"})


class KindRow(BaseModel):
    """One registered kind — the registry row without lifecycle state."""

    slug: str
    label: str
    description: str | None = None
    created_at: str
    created_by: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── seed ──────────────────────────────────────────────────────────────────


def seed_bootstrap_kinds(conn: sqlite3.Connection) -> int:
    """Seed the seven bootstrap kinds. Idempotent — called at every vault
    open (from ``init_db``), so existing vaults gain the registry on their
    next open and re-seeding an already-seeded vault is a no-op.

    Returns the number of NEW rows inserted (7 on first init, 0 after).
    """
    inserted = 0
    with conn:
        for slug, label, description in BOOTSTRAP_KINDS:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO kind_registry (
                    id, slug, label, description, created_at, created_by, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (f"kind:{slug}", slug, label, description, _now_iso(), BOOTSTRAP_CREATED_BY),
            )
            inserted += cursor.rowcount
    return inserted


# ── resolution (latest-row-wins, mirroring resolve_canonical) ─────────────


def resolve_kind_slug(conn: sqlite3.Connection, slug: str) -> str:
    """Follow the revision chain to a slug's current canonical form.

    The latest ``kind_revisions`` row for ``from_slug`` among
    ('rename', 'merge', 'restore') decides: rename/merge redirect to
    ``to_slug`` (and the walk continues from there), restore terminates
    the chain at the slug itself — that is how a rename or merge is
    reversed, by appending a compensating row (I7). A slug with no
    redirecting revisions (including every unknown slug) resolves to
    itself. Depth-capped like :func:`~.entities.resolve_canonical`.
    """
    current = slug
    seen: set[str] = {current}
    for _ in range(_RESOLVE_DEPTH_CAP):
        row = conn.execute(
            """
            SELECT action, to_slug FROM kind_revisions
            WHERE from_slug = ? AND action IN ('rename', 'merge', 'restore')
            ORDER BY revised_at DESC, id DESC LIMIT 1
            """,
            (current,),
        ).fetchone()
        if row is None or row["action"] == "restore" or row["to_slug"] is None:
            return current
        next_slug = row["to_slug"]
        if next_slug in seen:
            # Cycle — a rename ping-pong without a restore row. Stop here.
            return current
        seen.add(next_slug)
        current = next_slug
    return current


def resolve_kind_batch(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, str]:
    """Batch variant of :func:`resolve_kind_slug` — one entry per distinct
    input slug. The registry stays tiny (tens of rows, not thousands), so a
    Python loop over deduplicated inputs is the whole optimization; no CTE
    needed, unlike ``resolve_canonical_batch``.
    """
    return {s: resolve_kind_slug(conn, s) for s in dict.fromkeys(slugs)}


def live_kinds(conn: sqlite3.Connection) -> list[KindRow]:
    """The currently-live registry kinds, in registration order.

    A slug is live unless its latest ``kind_revisions`` row is a
    deprecate/rename/merge (same rule as the ``kind_current_v1`` view).
    Registration order (rowid) keeps the rendered extractor enum in the
    exact order the bootstrap seed inserted — byte-identical to the
    pre-registry constant.
    """
    rows = conn.execute(
        "SELECT slug, label, description, created_at, created_by FROM kind_registry ORDER BY rowid"
    ).fetchall()
    # Latest lifecycle action per slug in one query: ascending order means
    # a later row overwrites an earlier one (the latest_edge_reviews_batch
    # pattern).
    revision_rows = conn.execute(
        "SELECT from_slug, action FROM kind_revisions "
        "WHERE from_slug IS NOT NULL ORDER BY revised_at ASC, id ASC"
    ).fetchall()
    latest_action = {r["from_slug"]: r["action"] for r in revision_rows}
    return [
        KindRow(
            slug=r["slug"],
            label=r["label"],
            description=r["description"],
            created_at=r["created_at"],
            created_by=r["created_by"],
        )
        for r in rows
        if latest_action.get(r["slug"]) not in _NOT_LIVE_ACTIONS
    ]


def live_kind_slugs(conn: sqlite3.Connection | None = None) -> tuple[str, ...]:
    """The current set of valid kind slugs, in registration order.

    This is THE read every former enum site goes through. Safe fallback:
    with no connection, an uninitialized DB (registry tables absent), or
    an unseeded registry, it returns the bootstrap seven — so callers on
    a bare test DB behave exactly as they did before the registry existed.
    """
    if conn is None:
        return BOOTSTRAP_KIND_SLUGS
    try:
        kinds = live_kinds(conn)
    except Exception:
        # Registry unavailable (table missing on a bare test conn, or a
        # driver-level error). Broad on purpose: stdlib sqlite3 and
        # sqlcipher3 raise error classes that do NOT share a base, and the
        # contract here is a deterministic fallback, never a crash on the
        # write path.
        return BOOTSTRAP_KIND_SLUGS
    if not kinds:
        return BOOTSTRAP_KIND_SLUGS
    return tuple(k.slug for k in kinds)


def resolve_to_live_kind(conn: sqlite3.Connection | None, slug: str) -> str | None:
    """Resolve ``slug`` to the live kind it currently denotes, or None.

    Membership first (the common case), then the revision chain — a slug
    that was renamed/merged away still resolves to its live successor.
    Parse-don't-cast helper for validation sites (corrections).
    """
    valid = live_kind_slugs(conn)
    if slug in valid:
        return slug
    if conn is None:
        return None
    try:
        resolved = resolve_kind_slug(conn, slug)
    except Exception:
        # Same driver-divergence rationale as live_kind_slugs: an
        # unavailable registry means "no chain to follow", not a crash.
        return None
    return resolved if resolved in valid else None


# ── observations ledger (ADR-0003 Phase 3) ─────────────────────────────────


def write_kind_observation(
    conn: sqlite3.Connection,
    *,
    raw_kind: str,
    normalized_slug: str,
    entity_id: str,
    event_id: str,
    observed_by: str,
) -> str:
    """Preserve one raw extractor kind proposal that did NOT resolve to a
    live registry kind (ADR-0003 Phase 3).

    Free-text kinds never auto-register: the write path flattens an unknown
    proposal deterministically so intake never blocks on ontology questions,
    and this row keeps what the extractor actually said. It is the usage
    signal the Schema-Evolver (Phase 4) mines — when ``research_paper``
    shows up 40 times squashed into ``concept``, the promotion evidence
    sits in ``kind_observations``. ``normalized_slug`` records the kind the
    mention actually landed under (the resolved current kind of the entity
    it attached to), not merely the fallback.

    Append-only per I2 (trigger-enforced). Returns the new row's id.
    """
    observation_id = str(ULID())
    with conn:
        conn.execute(
            """
            INSERT INTO kind_observations (
                id, raw_kind, normalized_slug, entity_id, event_id, observed_at, observed_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                raw_kind,
                normalized_slug,
                entity_id,
                event_id,
                _now_iso(),
                observed_by,
            ),
        )
    return observation_id

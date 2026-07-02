#!/usr/bin/env python3
"""Read-only entity-graph checkup (ADR-0003 Phase 2 verification).

Measure before treating. This script opens the vault STRICTLY read-only
(SQLite ``?mode=ro`` URI plus ``PRAGMA query_only=ON``) and reports the
five diagnostics that answer "is Phase 2 (kind decoupled from identity)
actually working on this vault, and what is the same-name cluster
backlog it still has to drain":

1. Identity-scheme census — how many entities carry a v2 (name-first) id
   vs the v1 (kind-in-hash) legacy scheme, plus the row counts of the
   Phase 2/3 overlay ledgers (``entity_identities``,
   ``entity_kind_assignments``, ``kind_observations``, ``kind_revisions``).
   Zero v2 ids on a busy vault is the "silently broken deploy" signal.
2. Cluster census — same-name clusters (>1 live entity), split into
   same-kind vs cross-kind through the ``entity_current_kind_v1``
   resolution view. This pins the backlog baseline the drain (Slice 5)
   works down.
3. Formation rate — new entities per day (14-day window) whose lowercased
   name already had an older entity. After Slice 2 lands this should
   trend toward zero for the cross-kind subset.
4. Drain rate — the ``entity_dedup.cycle`` pipeline markers, parsed per
   day (examined / merged / kept-separate / operator-deferred).
5. Wildcard-mediated-link metric — exact-match mentions attached to an
   entity whose CURRENT kind is ``other``, the approximation for the
   ``_kinds_agree`` ``other`` wildcard the ADR named as the spot to watch
   (open question Q3). Evidence stream, not an alarm.

Nothing here writes. The read-only connection makes that a hard
guarantee (any INSERT/UPDATE/DELETE raises), so a self-hoster can run the
same diagnostic against a live vault without risk (I4-friendly).

Usage:
    uv run python scripts/checkup_entities.py
    uv run python scripts/checkup_entities.py --vault-dir /tmp/test-vault
    uv run python scripts/checkup_entities.py --json

Environment:
    VAULT_DIR         — overrides the default vault directory
    AFAIR_VAULT_KEY   — when set, the vault is opened via SQLCipher (still
                        read-only); otherwise plaintext.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from afair.settings import Settings
from afair.substrate.encryption import derive_sqlcipher_key

if TYPE_CHECKING:
    from collections.abc import Iterable

FORMATION_WINDOW_DAYS = 14
"""Look-back window for the formation-rate census (item 3)."""

DRAIN_WINDOW_DAYS = 14
"""Look-back window for the drain-rate census (item 4)."""

_DEDUP_CYCLE_STAGE = "entity_dedup.cycle"

# The free-text ``detail`` string the deduplicator writes on each cycle,
# e.g. "examined=3 merged=1 entities_merged=1 kept_separate=1 ...". Parsed
# rather than re-derived so the checkup reflects exactly what ran.
_DETAIL_FIELD_RE = re.compile(r"(\w+)=(\d+)")


def _open_readonly(vault_dir: Path, vault_key: bytes | None) -> sqlite3.Connection:
    """Open the substrate STRICTLY read-only.

    Uses the ``?mode=ro`` URI so SQLite itself rejects any write, and adds
    ``PRAGMA query_only=ON`` as a second, connection-level guard. Never
    calls ``init_db`` — the checkup reads an already-initialized vault and
    must not create or alter a single row (the whole point of a checkup).

    Encrypted vaults (``AFAIR_VAULT_KEY`` set) open via SQLCipher with the
    same derived raw-hex key ``open_db`` uses; plaintext vaults use stdlib
    sqlite3.
    """
    db_path = vault_dir / "substrate.db"
    uri = f"file:{db_path}?mode=ro"
    if vault_key is not None:
        try:
            import sqlcipher3  # type: ignore
        except ImportError as exc:  # pragma: no cover - prod has the dep
            msg = (
                "AFAIR_VAULT_KEY is set but sqlcipher3 is not installed. "
                "Run `uv sync` to install dependencies."
            )
            raise RuntimeError(msg) from exc
        conn = sqlcipher3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlcipher3.Row
        hex_key = derive_sqlcipher_key(vault_key)
        conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    else:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _count(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> int:
    return int(conn.execute(sql, tuple(params)).fetchone()[0])


# ── item 1: identity-scheme census ────────────────────────────────────────


def identity_census(conn: sqlite3.Connection) -> dict[str, int]:
    """v2-vs-total entity count plus the Phase 2/3 overlay-ledger row counts."""
    row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN id LIKE 'entity:v2:%' THEN 1 ELSE 0 END) AS v2, "
        "  COUNT(*) AS total "
        "FROM entities"
    ).fetchone()
    return {
        "v2_entities": int(row["v2"] or 0),
        "total_entities": int(row["total"] or 0),
        "entity_identities": _count(conn, "SELECT COUNT(*) FROM entity_identities"),
        "entity_kind_assignments": _count(conn, "SELECT COUNT(*) FROM entity_kind_assignments"),
        "kind_observations": _count(conn, "SELECT COUNT(*) FROM kind_observations"),
        "kind_revisions": _count(conn, "SELECT COUNT(*) FROM kind_revisions"),
    }


# ── item 2: cluster census ─────────────────────────────────────────────────


def cluster_census(conn: sqlite3.Connection) -> dict[str, Any]:
    """Same-name clusters over LIVE entities, split same-kind vs cross-kind.

    Live = not retracted, and not the ``from`` side of a still-valid merge
    (a merged-away entity is no longer its own cluster member). Kinds come
    from ``entity_current_kind_v1`` so an operator/agent retype is
    reflected — a cluster the operator unified shows as same-kind.
    """
    rows = conn.execute(
        """
        WITH live AS (
            SELECT e.id, LOWER(e.canonical_name) AS name_lower
            FROM entities e
            WHERE e.id NOT IN (SELECT entity_id FROM entity_retractions)
              AND NOT EXISTS (
                  SELECT 1 FROM entity_merges m
                  WHERE m.from_entity_id = e.id
                    AND NOT EXISTS (
                        SELECT 1 FROM merge_invalidations mi WHERE mi.merge_id = m.id
                    )
              )
        )
        SELECT live.name_lower AS name_lower,
               COUNT(*) AS members,
               COUNT(DISTINCT ck.kind_slug) AS distinct_kinds
        FROM live
        JOIN entity_current_kind_v1 ck ON ck.entity_id = live.id
        GROUP BY live.name_lower
        HAVING members > 1
        ORDER BY members DESC, name_lower ASC
        """
    ).fetchall()
    clusters = [
        {
            "name": r["name_lower"],
            "members": int(r["members"]),
            "distinct_kinds": int(r["distinct_kinds"]),
        }
        for r in rows
    ]
    same_kind = sum(1 for c in clusters if c["distinct_kinds"] == 1)
    cross_kind = sum(1 for c in clusters if c["distinct_kinds"] > 1)
    return {
        "total_clusters": len(clusters),
        "same_kind_clusters": same_kind,
        "cross_kind_clusters": cross_kind,
        "clusters": clusters,
    }


# ── item 3: formation rate ─────────────────────────────────────────────────


def formation_rate(
    conn: sqlite3.Connection, *, window_days: int = FORMATION_WINDOW_DAYS
) -> dict[str, Any]:
    """New entities per day (within the window) that already had an older
    same-name entity — the rate at which same-name splits keep forming.

    ``cross_kind`` counts the subset whose CURRENT kind differs from an
    older same-name sibling's — the number Slice 2 is meant to drive to ~0.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    per_day = conn.execute(
        """
        SELECT substr(e.created_at, 1, 10) AS day, COUNT(*) AS n
        FROM entities e
        WHERE e.created_at >= ?
          AND EXISTS (
              SELECT 1 FROM entities o
              WHERE LOWER(o.canonical_name) = LOWER(e.canonical_name)
                AND o.created_at < e.created_at
          )
        GROUP BY day
        ORDER BY day
        """,
        (cutoff,),
    ).fetchall()
    cross_kind_total = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM entities e
        JOIN entity_current_kind_v1 ck ON ck.entity_id = e.id
        WHERE e.created_at >= ?
          AND EXISTS (
              SELECT 1 FROM entities o
              JOIN entity_current_kind_v1 cko ON cko.entity_id = o.id
              WHERE LOWER(o.canonical_name) = LOWER(e.canonical_name)
                AND o.created_at < e.created_at
                AND cko.kind_slug != ck.kind_slug
          )
        """,
        (cutoff,),
    )
    return {
        "window_days": window_days,
        "per_day": [{"day": r["day"], "new_with_older_sibling": int(r["n"])} for r in per_day],
        "cross_kind_total": cross_kind_total,
    }


# ── item 4: drain rate ─────────────────────────────────────────────────────


def drain_rate(conn: sqlite3.Connection, *, window_days: int = DRAIN_WINDOW_DAYS) -> dict[str, Any]:
    """Per-day totals parsed from the ``entity_dedup.cycle`` pipeline markers."""
    cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT substr(recorded_at, 1, 10) AS day, detail
        FROM pipeline_events
        WHERE stage = ? AND recorded_at >= ?
        ORDER BY recorded_at
        """,
        (_DEDUP_CYCLE_STAGE, cutoff),
    ).fetchall()
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        day = r["day"]
        fields = {k: int(v) for k, v in _DETAIL_FIELD_RE.findall(r["detail"] or "")}
        bucket = by_day.setdefault(day, {})
        for k, v in fields.items():
            bucket[k] = bucket.get(k, 0) + v
    return {
        "window_days": window_days,
        "cycles": _count(
            conn,
            "SELECT COUNT(*) FROM pipeline_events WHERE stage = ? AND recorded_at >= ?",
            (_DEDUP_CYCLE_STAGE, cutoff),
        ),
        "per_day": [{"day": day, **by_day[day]} for day in sorted(by_day)],
    }


# ── item 5: wildcard-mediated-link metric (Q3 evidence) ────────────────────


def wildcard_metric(conn: sqlite3.Connection) -> dict[str, int]:
    """Exact-match mentions whose linked entity's CURRENT kind is ``other``.

    Approximates how often the ``_kinds_agree`` ``other`` wildcard let an
    exact link through of ANY kind (the ADR's named dogfooding risk). We
    can only observe the entity side; the proposed-kind side isn't stored,
    so this is a lower-bounded proxy, not an exact count.
    """
    total_exact = _count(conn, "SELECT COUNT(*) FROM entity_mentions WHERE match_method = 'exact'")
    other_exact = _count(
        conn,
        """
        SELECT COUNT(*)
        FROM entity_mentions m
        JOIN entity_current_kind_v1 ck ON ck.entity_id = m.entity_id
        WHERE m.match_method = 'exact' AND ck.kind_slug = 'other'
        """,
    )
    return {"exact_mentions": total_exact, "other_kind_exact_mentions": other_exact}


# ── report assembly ────────────────────────────────────────────────────────


def run_checkup(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the full read-only report from a read-only connection."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "identity_census": identity_census(conn),
        "cluster_census": cluster_census(conn),
        "formation_rate": formation_rate(conn),
        "drain_rate": drain_rate(conn),
        "wildcard_metric": wildcard_metric(conn),
    }


def _render(report: dict[str, Any]) -> str:
    ic = report["identity_census"]
    cc = report["cluster_census"]
    fr = report["formation_rate"]
    dr = report["drain_rate"]
    wm = report["wildcard_metric"]
    lines: list[str] = []
    lines.append("afair entity-graph checkup (read-only)")
    lines.append(f"  generated_at: {report['generated_at']}")
    lines.append("")
    lines.append("1. identity-scheme census")
    lines.append(
        f"   entities: {ic['v2_entities']} v2 / {ic['total_entities']} total"
        f"  (v1: {ic['total_entities'] - ic['v2_entities']})"
    )
    lines.append(f"   entity_identities rows:       {ic['entity_identities']}")
    lines.append(f"   entity_kind_assignments rows: {ic['entity_kind_assignments']}")
    lines.append(f"   kind_observations rows:       {ic['kind_observations']}")
    lines.append(f"   kind_revisions rows:          {ic['kind_revisions']}")
    lines.append("")
    lines.append("2. cluster census (same-name, >1 live member)")
    lines.append(
        f"   total: {cc['total_clusters']}  "
        f"(same-kind: {cc['same_kind_clusters']}, cross-kind: {cc['cross_kind_clusters']})"
    )
    for c in cc["clusters"][:20]:
        lines.append(
            f"     - {c['name']}: {c['members']} members, {c['distinct_kinds']} distinct kinds"
        )
    if len(cc["clusters"]) > 20:
        lines.append(f"     … and {len(cc['clusters']) - 20} more")
    lines.append("")
    lines.append(f"3. formation rate (last {fr['window_days']} days)")
    lines.append(f"   cross-kind new-with-older-sibling: {fr['cross_kind_total']}")
    for d in fr["per_day"]:
        lines.append(f"     {d['day']}: {d['new_with_older_sibling']} new-with-older-sibling")
    lines.append("")
    lines.append(f"4. drain rate ({dr['cycles']} dedup cycles in last {dr['window_days']} days)")
    for d in dr["per_day"]:
        parts = " ".join(f"{k}={v}" for k, v in d.items() if k != "day")
        lines.append(f"     {d['day']}: {parts}")
    lines.append("")
    lines.append("5. wildcard-mediated-link metric (Q3 evidence)")
    lines.append(
        f"   exact mentions: {wm['exact_mentions']}  "
        f"(attached to a current-kind='other' entity: {wm['other_kind_exact_mentions']})"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only entity-graph checkup for an afair vault (ADR-0003 Phase 2)."
    )
    parser.add_argument(
        "--vault-dir",
        type=Path,
        default=None,
        help="Override vault directory (defaults to settings.vault_dir / $VAULT_DIR).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.vault_dir is not None:
        settings = settings.model_copy(update={"vault_dir": args.vault_dir})

    if not (settings.vault_dir / "substrate.db").exists():
        sys.stderr.write(
            f"no substrate.db found at {settings.vault_dir / 'substrate.db'}; nothing to check\n"
        )
        return 2

    vault_key = (
        settings.vault_key.get_secret_value().encode("utf-8")
        if settings.vault_key is not None
        else None
    )
    conn = _open_readonly(settings.vault_dir, vault_key)
    try:
        report = run_checkup(conn)
    finally:
        conn.close()

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_render(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

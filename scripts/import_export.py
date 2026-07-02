#!/usr/bin/env python3
"""Self-recoverable export reader for afair vaults.

Given a JSONL export from ``/internal/export`` (with or without
``?blobs=inline``), this script reconstructs a queryable SQLite
database on the user's local machine. The point of the script is
the bus-factor guarantee promised in /datenschutz and the AGB:

    Even if afair (the service) disappears, your vault export is a
    complete, self-contained, openable record of everything you put
    in. No afair binaries required, no afair infrastructure required,
    no afair-specific tools required to read it.

The script is deliberately dependency-free (stdlib only). It runs on
any Python 3.10+ with no virtualenv. Save the file, save your
export, run it. That is the entire onboarding for a recovery.

Usage
-----
    # Stream from the live server straight into a local DB:
    curl -H "Authorization: Bearer $AFAIR_EXPORT_TOKEN" \\
         https://mcp.afair.ai/internal/export?blobs=inline \\
         > export.jsonl
    python3 import_export.py export.jsonl --out my-vault.db

    # Then explore with any SQLite client:
    sqlite3 my-vault.db
    sqlite> SELECT json_extract(payload, '$.text') FROM events
    sqlite>   WHERE json_extract(payload, '$.text') IS NOT NULL
    sqlite>   ORDER BY created_at DESC LIMIT 20;

The output DB has tables matching the JSONL record kinds:
    events, interpretations, entities, entity_mentions, entity_edges,
    entity_merges, edge_invalidations, merge_invalidations,
    entity_retractions, edge_reviews, entity_identities,
    entity_kind_assignments, kind_registry, kind_revisions,
    kind_observations, proposed_corrections, proposed_ontology_revisions,
    tuner_state, blobs

The correction + ontology tables matter for fidelity (I4): they carry the
operator's verdicts (a retracted entity stays retracted, a rejected merge
stays rejected, a retyped entity keeps its assigned kind, a renamed kind
still resolves) — none of which can be regenerated from events alone.

Rows are inserted in stream order, and the export stream is FK-ordered
(events first, then entities, then everything that references them), so a
reconstruction that replays this file top-to-bottom into a real
FK-enforcing substrate never sees a dangling reference.

The ``blobs`` table stores the inlined base64 if ``?blobs=inline`` was
used. A separate ``--extract-blobs DIR`` flag writes each blob to its
sha256-named file on disk.

Verification
------------
The final JSONL record is a manifest. If the importer does not see it,
the export was truncated. Re-run the export.
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    event_kind      TEXT NOT NULL,
    origin          TEXT NOT NULL,
    parent_hashes   TEXT,
    schema_version  INTEGER,
    payload         TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_created_idx ON events(created_at);

CREATE TABLE IF NOT EXISTS interpretations (
    id              TEXT PRIMARY KEY,
    event_id        TEXT,
    event_hash      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    produced_by     TEXT NOT NULL,
    produced_at     TEXT NOT NULL,
    extraction      TEXT
);
CREATE INDEX IF NOT EXISTS interpretations_event_idx ON interpretations(event_id);

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_mentions (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_edges (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_merges (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_invalidations (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);

-- Correction ledger (ADR-0002): the operator's verdicts. Without these,
-- a restored vault silently resurrects deleted/merged entities.
CREATE TABLE IF NOT EXISTS merge_invalidations (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_retractions (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_reviews (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);

-- Ontology (ADR-0003): the emergent kind system. Without these, a
-- restored vault loses every operator/agent ontology decision.
CREATE TABLE IF NOT EXISTS entity_identities (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_kind_assignments (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kind_registry (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kind_revisions (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kind_observations (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);

-- Suggestion queues: decided rows are operator verdicts recorded nowhere
-- else; pending rows are regenerable but harmless.
CREATE TABLE IF NOT EXISTS proposed_corrections (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposed_ontology_revisions (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);

-- Self-improvement log (I7): every promote/rollback the tuner made.
CREATE TABLE IF NOT EXISTS tuner_state (
    rowid_external  INTEGER PRIMARY KEY,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
    blob_hash       TEXT PRIMARY KEY,
    size_bytes      INTEGER,
    content_b64     TEXT
);

CREATE TABLE IF NOT EXISTS import_manifest (
    produced_at     TEXT,
    include_blobs   INTEGER,
    format_version  INTEGER,
    imported_at     TEXT DEFAULT (datetime('now'))
);
"""


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _insert_event(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO events
           (id, content_hash, event_kind, origin, parent_hashes,
            schema_version, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rec["id"],
            rec["content_hash"],
            rec.get("event_kind") or rec.get("kind"),
            rec["origin"],
            json.dumps(rec.get("parent_hashes")) if rec.get("parent_hashes") else None,
            rec.get("schema_version"),
            json.dumps(rec.get("payload"), ensure_ascii=False)
            if rec.get("payload") is not None
            else None,
            rec["created_at"],
        ),
    )


def _insert_interpretation(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO interpretations
           (id, event_id, event_hash, version, produced_by, produced_at, extraction)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            rec["id"],
            rec.get("event_id"),
            rec["event_hash"],
            rec["version"],
            rec["produced_by"],
            rec["produced_at"],
            json.dumps(rec.get("extraction"), ensure_ascii=False)
            if rec.get("extraction") is not None
            else None,
        ),
    )


def _insert_generic(conn: sqlite3.Connection, table: str, rec: dict) -> None:
    rec.pop("kind", None)
    if table == "entities":
        # entities is keyed by the substrate entity id so dependents
        # (retractions, kind assignments, identities, edges) can be joined
        # back by id. Previously the id column existed but was never
        # populated — a NULL-id row per entity.
        conn.execute(
            "INSERT OR REPLACE INTO entities (id, payload) VALUES (?, ?)",
            (rec.get("id"), json.dumps(rec, ensure_ascii=False)),
        )
        return
    conn.execute(
        f"INSERT INTO {table} (payload) VALUES (?)",
        (json.dumps(rec, ensure_ascii=False),),
    )


def _insert_blob(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO blobs (blob_hash, size_bytes, content_b64) VALUES (?, ?, ?)",
        (rec["blob_hash"], rec.get("size_bytes"), rec.get("content_b64")),
    )


def _record_manifest(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT INTO import_manifest (produced_at, include_blobs, format_version)
           VALUES (?, ?, ?)""",
        (
            rec.get("produced_at"),
            1 if rec.get("include_blobs") else 0,
            rec.get("format_version"),
        ),
    )


# Record kind → target table. Insert order follows the stream, which the
# export emits FK-first (events → entities → dependents), so replaying
# top-to-bottom is always reference-safe.
GENERIC_KINDS = {
    "entity": "entities",
    "entity_mention": "entity_mentions",
    "entity_edge": "entity_edges",
    "entity_merge": "entity_merges",
    "edge_invalidation": "edge_invalidations",
    "merge_invalidation": "merge_invalidations",
    "entity_retraction": "entity_retractions",
    "edge_review": "edge_reviews",
    "entity_identity": "entity_identities",
    "entity_kind_assignment": "entity_kind_assignments",
    "kind_registry": "kind_registry",
    "kind_revision": "kind_revisions",
    "kind_observation": "kind_observations",
    "proposed_correction": "proposed_corrections",
    "proposed_ontology_revision": "proposed_ontology_revisions",
    "tuner_state": "tuner_state",
}


def import_jsonl(src: Path, dst: Path) -> dict[str, int]:
    """Walk the JSONL file, write to the target SQLite. Returns counts."""
    counts: dict[str, int] = {}
    saw_manifest = False
    conn = _open_db(dst)
    try:
        with src.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(
                        f"WARN line {line_no}: invalid JSON, skipping. ({exc})",
                        file=sys.stderr,
                    )
                    continue
                kind = rec.get("kind")
                if kind == "event":
                    _insert_event(conn, rec)
                elif kind == "interpretation":
                    _insert_interpretation(conn, rec)
                elif kind in GENERIC_KINDS:
                    _insert_generic(conn, GENERIC_KINDS[kind], rec)
                elif kind == "blob":
                    _insert_blob(conn, rec)
                elif kind == "manifest":
                    saw_manifest = True
                    _record_manifest(conn, rec)
                else:
                    print(
                        f"WARN line {line_no}: unknown kind {kind!r}, skipped.",
                        file=sys.stderr,
                    )
                counts[kind or "unknown"] = counts.get(kind or "unknown", 0) + 1
        conn.commit()
    finally:
        conn.close()
    if not saw_manifest:
        print(
            "ERROR: no manifest record at end of file — the export looks "
            "truncated. Re-run the export and try again.",
            file=sys.stderr,
        )
        sys.exit(2)
    return counts


def extract_blobs(db: Path, out_dir: Path) -> int:
    """Decode every base64-inlined blob to files under out_dir/<sha256>."""
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT blob_hash, content_b64 FROM blobs WHERE content_b64 IS NOT NULL"
        )
        n = 0
        for blob_hash, b64 in rows:
            # Hash format is "sha256:<hex>" — strip the prefix for the filename.
            name = blob_hash.split(":", 1)[-1]
            (out_dir / name).write_bytes(base64.b64decode(b64))
            n += 1
        return n
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Reconstruct a queryable SQLite from an afair JSONL export.",
    )
    p.add_argument("src", type=Path, help="Path to the .jsonl export file.")
    p.add_argument(
        "--out",
        type=Path,
        default=Path("afair-vault.db"),
        help="Output SQLite database (default: afair-vault.db).",
    )
    p.add_argument(
        "--extract-blobs",
        type=Path,
        metavar="DIR",
        default=None,
        help="If set, write base64-inlined blobs to DIR/<sha256> files.",
    )
    args = p.parse_args()

    if not args.src.exists():
        print(f"ERROR: {args.src} does not exist", file=sys.stderr)
        return 1

    counts = import_jsonl(args.src, args.out)
    total = sum(counts.values())
    print(f"imported {total} records into {args.out}:")
    for k in sorted(counts):
        print(f"  {k:20s} {counts[k]}")

    if args.extract_blobs is not None:
        n = extract_blobs(args.out, args.extract_blobs)
        print(f"extracted {n} blob files to {args.extract_blobs}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())

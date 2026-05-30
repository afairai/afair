"""Durable event-record persistence — every event also lives as an
immutable file in the filesystem, keyed by its content-hash.

Architecture rationale
----------------------
The SQLite ``events`` table is fast and queryable, but a single SQLite
file is a single point of failure: corruption of the file loses every
event. To bound that blast radius and satisfy I2 (append-only) at a
deeper layer than "one DB file", every event is dual-written:

* SQLite ``events`` row     — the working index (fast queries, FTS5)
* ``vault/event_records/``  — one immutable JSON file per event,
                              named by its content-hash

If the SQLite working copy is ever lost or corrupted, the substrate
can be rebuilt from the event-records directory alone (see
:mod:`afair.substrate.recovery`). This makes the event-records directory
the true source of truth; SQLite is a regenerable index.

Layout::

    vault/event_records/<aa>/<rest-of-sha256>.json

Where ``<aa>`` is the first two hex chars of the event's content-hash
(git-style sharding to keep any directory at manageable size).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .payload import canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_RECORDS_DIR = "event_records"
_HASH_PREFIX = "sha256:"
_HEX_LEN = 64


def record_path(vault_dir: Path, content_hash: str) -> Path:
    """Resolve a content-hash to its event-record path. Path may not yet exist."""
    if not content_hash.startswith(_HASH_PREFIX):
        msg = f"content_hash must start with {_HASH_PREFIX!r}, got {content_hash!r}"
        raise ValueError(msg)
    hex_part = content_hash.removeprefix(_HASH_PREFIX)
    if len(hex_part) != _HEX_LEN:
        msg = f"sha256 hex must be {_HEX_LEN} chars, got {len(hex_part)}"
        raise ValueError(msg)
    return vault_dir / _RECORDS_DIR / hex_part[:2] / f"{hex_part[2:]}.json"


def write_record(
    vault_dir: Path,
    *,
    event_id: str,
    content_hash: str,
    created_at: str,
    origin: str,
    kind: str,
    payload: dict[str, Any],
    parent_hashes: list[str] | None,
    schema_version: int,
) -> None:
    """Write one immutable event record. Idempotent on content-hash.

    Atomic: writes to a sibling temp file and renames into place, so a
    half-written record can never appear at the canonical path.
    """
    path = record_path(vault_dir, content_hash)
    if path.exists():
        return  # idempotent: same content_hash → same record bytes

    record = {
        "id": event_id,
        "content_hash": content_hash,
        "created_at": created_at,
        "origin": origin,
        "kind": kind,
        "payload": payload,
        "parent_hashes": parent_hashes,
        "schema_version": schema_version,
    }
    serialized = canonical_json(record).encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(serialized)
    tmp_path.replace(path)


def read_record(vault_dir: Path, content_hash: str) -> dict[str, Any]:
    """Read one event record by content-hash. Raises ``FileNotFoundError``."""
    path = record_path(vault_dir, content_hash)
    parsed: dict[str, Any] = json.loads(path.read_bytes())
    return parsed


def iter_records(vault_dir: Path) -> Iterator[dict[str, Any]]:
    """Iterate every event record in the vault, in arbitrary order.

    Used for substrate rebuild (recovery) and integrity audits. Does
    not load all records into memory.
    """
    root = vault_dir / _RECORDS_DIR
    if not root.exists():
        return
    for shard in sorted(root.iterdir()):
        if not shard.is_dir():
            continue
        for record_file in sorted(shard.iterdir()):
            if record_file.suffix != ".json" or record_file.name.endswith(".tmp"):
                continue
            yield json.loads(record_file.read_bytes())


def record_exists(vault_dir: Path, content_hash: str) -> bool:
    """Cheap check: is this event durably persisted in the records dir?"""
    return record_path(vault_dir, content_hash).exists()

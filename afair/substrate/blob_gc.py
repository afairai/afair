"""Object-store garbage collection: find blobs no live event references.

The object store and the events table are two stores with no shared
transaction (filesystem vs SQLite). A blob is written *before* its event row
(:func:`afair.substrate.payload.build_binary_payload` calls ``write_object``
ahead of the insert), and the streaming-upload path writes a blob that a
later ``remember(blob-ref)`` call wires up. If the event write never lands
(process death, a permanently-failing call), the blob stays on disk with
nothing pointing at it — an orphan. The substrate is append-only (I2), so
nothing ever deletes an event to *create* an orphan; orphans only come from
that write-ordering gap.

This module is the mark phase. ``reachable_blob_hashes`` is the single source
of truth for "which blobs are alive" — the export uses it to ship only
reachable blobs, and the sweeper uses it to find the rest. The sweep itself
**quarantines** rather than deletes (I4 — we never silently destroy the
user's bytes): an orphan older than a grace window moves to
``objects/.orphaned/`` where it is out of the live set but recoverable. A
deliberate second step (or a self-hoster) empties the quarantine.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .objects import _HASH_PREFIX, _HEX_LEN

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

# In-flight uploads create a real-but-not-yet-referenced blob: the bytes land
# via /internal/blob/upload, then a separate remember(blob-ref) call wires the
# event. A blob younger than this window might be mid-handshake, so the sweep
# leaves it alone. The normal gap is seconds; a full day is generous slack.
DEFAULT_ORPHAN_GRACE_SECONDS = 24 * 3600

_QUARANTINE_DIRNAME = ".orphaned"
_TMP_DIRNAME = ".tmp"


def blob_hashes_in_payload(payload: Any) -> Iterator[str]:
    """Yield every ``sha256:`` blob_hash referenced anywhere in a payload.

    Walks dicts and lists recursively so single binary payloads, blob-ref
    payloads, and compound events (whose parts each carry a blob_hash) are all
    covered. The single source of truth for "what does this event point at" —
    the export and the GC both read through it so they can never disagree.
    """
    if isinstance(payload, dict):
        h = payload.get("blob_hash")
        if isinstance(h, str) and h.startswith(_HASH_PREFIX):
            yield h
        for value in payload.values():
            yield from blob_hashes_in_payload(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from blob_hashes_in_payload(value)


def reachable_blob_hashes(conn: sqlite3.Connection) -> set[str]:
    """The set of blob hashes reachable from any event in the substrate.

    This is the GC mark set: every blob NOT in here is unreferenced. Narrows
    the table scan with a ``LIKE`` prefilter (only rows that mention a blob at
    all), then parses each payload — the append-only events table is the
    authority on what's alive, never the filesystem.
    """
    reachable: set[str] = set()
    rows = conn.execute(
        "SELECT payload FROM events WHERE payload LIKE '%blob_hash%'",
    )
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        reachable.update(blob_hashes_in_payload(payload))
    return reachable


def _objects_root(vault_dir: Path) -> Path:
    return vault_dir / "objects"


def iter_stored_blobs(vault_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(blob_hash, path)`` for every blob on disk.

    Reconstructs the content address from the sharded layout
    ``objects/<aa>/<rest-62-hex>`` → ``sha256:<aa><rest>``. Skips the
    ``.tmp`` (in-progress uploads) and ``.orphaned`` (already quarantined)
    sidecar directories. Files that don't fit the sharded hex shape are
    ignored rather than guessed at.
    """
    root = _objects_root(vault_dir)
    if not root.is_dir():
        return
    for shard in root.iterdir():
        if not shard.is_dir() or shard.name in {_TMP_DIRNAME, _QUARANTINE_DIRNAME}:
            continue
        prefix = shard.name
        if len(prefix) != 2:
            continue
        for blob in shard.iterdir():
            if not blob.is_file():
                continue
            hex_part = prefix + blob.name
            if len(hex_part) != _HEX_LEN:
                continue
            try:
                int(hex_part, 16)
            except ValueError:
                continue
            yield f"{_HASH_PREFIX}{hex_part}", blob


def quarantine_blob(vault_dir: Path, blob_hash: str, path: Path) -> Path:
    """Move an orphan into ``objects/.orphaned/<aa>/<rest>`` and return the
    new path. Preserves the sharded name so the content address is still
    recoverable; idempotent if the same hash is quarantined twice."""
    hex_part = blob_hash.removeprefix(_HASH_PREFIX)
    dest = _objects_root(vault_dir) / _QUARANTINE_DIRNAME / hex_part[:2] / hex_part[2:]
    dest.parent.mkdir(parents=True, exist_ok=True)
    path.replace(dest)
    return dest


def sweep_orphan_blobs(
    vault_dir: Path,
    conn: sqlite3.Connection,
    *,
    now: float,
    grace_seconds: int = DEFAULT_ORPHAN_GRACE_SECONDS,
    quarantine: bool = True,
) -> dict[str, Any]:
    """Find and (by default) quarantine orphaned blobs.

    An orphan is a stored blob that no event references AND whose file is
    older than ``grace_seconds`` (so an in-flight upload mid-handshake is
    never swept). ``now`` is passed in (not read from the clock) so the caller
    controls time — keeps this deterministic and testable.

    Returns counts: scanned, reachable, orphaned (over grace), quarantined,
    within_grace (skipped because too young). With ``quarantine=False`` it
    only reports — a dry run for ops before enabling the move.
    """
    reachable = reachable_blob_hashes(conn)
    scanned = 0
    orphaned = 0
    quarantined = 0
    within_grace = 0
    for blob_hash, path in iter_stored_blobs(vault_dir):
        scanned += 1
        if blob_hash in reachable:
            continue
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age < grace_seconds:
            within_grace += 1
            continue
        orphaned += 1
        if quarantine:
            try:
                quarantine_blob(vault_dir, blob_hash, path)
                quarantined += 1
            except FileNotFoundError:
                # Raced with another mover; nothing to do.
                continue
    return {
        "scanned": scanned,
        "reachable": len(reachable),
        "orphaned": orphaned,
        "quarantined": quarantined,
        "within_grace": within_grace,
    }

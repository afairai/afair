"""Object-store GC: reachability mark set + orphan sweep (quarantine).

Covers afair.substrate.blob_gc — the shared reachability walk the export and
the cold-path OrphanBlobSweeper both use, and the sweep's grace-window +
quarantine-not-delete behaviour.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from afair.substrate import open_db, write_event
from afair.substrate.blob_gc import (
    blob_hashes_in_payload,
    iter_stored_blobs,
    reachable_blob_hashes,
    sweep_orphan_blobs,
)
from afair.substrate.db import set_vault_key
from afair.substrate.objects import object_exists, write_object

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _plaintext_object_store():
    """No vault key — blobs land as plaintext so the tests don't depend on
    SQLCipher (Linux-only). The GC logic is encryption-agnostic."""
    set_vault_key(None)
    yield
    set_vault_key(None)


def _age_file(path, *, seconds_old: float, now: float) -> None:
    """Backdate a file's mtime so the grace-window logic sees it as old."""
    target = now - seconds_old
    os.utime(path, (target, target))


# ── reachability (the mark set) ─────────────────────────────────────────────


def test_blob_hashes_in_payload_walks_binary_blobref_and_compound() -> None:
    binary = {"content_type": "binary", "blob_hash": "sha256:" + "a" * 64}
    compound = {
        "content_type": "compound",
        "parts": [
            {"type": "text", "text": "hi"},
            {"type": "blob-ref", "blob_hash": "sha256:" + "b" * 64},
            {"type": "blob-ref", "blob_hash": "sha256:" + "c" * 64},
        ],
    }
    assert set(blob_hashes_in_payload(binary)) == {"sha256:" + "a" * 64}
    assert set(blob_hashes_in_payload(compound)) == {
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
    }
    # Non-blob keys / inline text yield nothing.
    assert list(blob_hashes_in_payload({"content_type": "text", "text": "x"})) == []


def test_reachable_blob_hashes_reads_the_events_table(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        h1 = write_object(tmp_path, b"referenced one")
        h2 = write_object(tmp_path, b"referenced two")
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "binary", "blob_hash": h1, "mime": "x"},
        )
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "binary", "blob_hash": h2, "mime": "x"},
        )
        assert reachable_blob_hashes(db) == {h1, h2}
    finally:
        db.close()


# ── the sweep ───────────────────────────────────────────────────────────────


def test_sweep_quarantines_old_orphan_keeps_referenced(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        now = 1_000_000.0
        referenced = write_object(tmp_path, b"i am referenced")
        write_event(
            db,
            origin="user",
            kind="remember",
            payload={"content_type": "binary", "blob_hash": referenced, "mime": "x"},
        )
        # An orphan: bytes on disk, no event points at it. Backdate it well
        # past the grace window.
        orphan = write_object(tmp_path, b"nobody references me")
        from afair.substrate.objects import object_path

        _age_file(object_path(tmp_path, orphan), seconds_old=48 * 3600, now=now)

        stats = sweep_orphan_blobs(tmp_path, db, now=now, grace_seconds=24 * 3600)

        assert stats["orphaned"] == 1
        assert stats["quarantined"] == 1
        # Referenced blob untouched; orphan moved out of the live store.
        assert object_exists(tmp_path, referenced)
        assert not object_exists(tmp_path, orphan)
        # Quarantined, not destroyed — recoverable under .orphaned/.
        hex_part = orphan.removeprefix("sha256:")
        quarantined = tmp_path / "objects" / ".orphaned" / hex_part[:2] / hex_part[2:]
        assert quarantined.is_file()
        assert quarantined.read_bytes() == b"nobody references me"
    finally:
        db.close()


def test_sweep_spares_orphan_within_grace_window(tmp_path: Path) -> None:
    """A fresh unreferenced blob might be a mid-handshake upload whose
    remember(blob-ref) hasn't landed — never sweep it."""
    db = open_db(tmp_path)
    try:
        now = 1_000_000.0
        fresh = write_object(tmp_path, b"just uploaded, ref pending")
        from afair.substrate.objects import object_path

        _age_file(object_path(tmp_path, fresh), seconds_old=60, now=now)  # 1 min old

        stats = sweep_orphan_blobs(tmp_path, db, now=now, grace_seconds=24 * 3600)

        assert stats["within_grace"] == 1
        assert stats["quarantined"] == 0
        assert object_exists(tmp_path, fresh)
    finally:
        db.close()


def test_sweep_dry_run_reports_without_moving(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        now = 1_000_000.0
        orphan = write_object(tmp_path, b"orphan for dry run")
        from afair.substrate.objects import object_path

        _age_file(object_path(tmp_path, orphan), seconds_old=48 * 3600, now=now)

        stats = sweep_orphan_blobs(tmp_path, db, now=now, grace_seconds=24 * 3600, quarantine=False)

        assert stats["orphaned"] == 1
        assert stats["quarantined"] == 0
        assert object_exists(tmp_path, orphan)  # untouched
    finally:
        db.close()


def test_iter_stored_blobs_skips_sidecar_dirs(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        now = 1_000_000.0
        # One real blob, plus a leftover .tmp upload + an already-quarantined
        # file — neither should be reported as a stored blob.
        real = write_object(tmp_path, b"real")
        (tmp_path / "objects" / ".tmp").mkdir(parents=True, exist_ok=True)
        (tmp_path / "objects" / ".tmp" / "upload-deadbeef").write_bytes(b"partial")
        (tmp_path / "objects" / ".orphaned" / "ff").mkdir(parents=True, exist_ok=True)
        (tmp_path / "objects" / ".orphaned" / "ff" / ("0" * 62)).write_bytes(b"old")

        found = {h for h, _ in iter_stored_blobs(tmp_path)}
        assert found == {real}

        # And a sweep over this layout sees exactly one stored blob.
        stats = sweep_orphan_blobs(tmp_path, db, now=now, grace_seconds=0)
        assert stats["scanned"] == 1
    finally:
        db.close()

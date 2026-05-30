"""Content-addressed object store on the filesystem.

Layout::

    vault/objects/<aa>/<rest-of-sha256>

Where ``<aa>`` is the first two hex chars of the blob's sha256 (git-style
sharding) and ``<rest-of-sha256>`` is the remaining 62 hex chars. Each
file is named by its own hash, so write-if-absent is idempotent and a
sha256 collision (functionally impossible) would be the only failure
mode.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_HASH_PREFIX = "sha256:"
_HEX_LEN = 64


def _hash_bytes(data: bytes) -> str:
    return f"{_HASH_PREFIX}{hashlib.sha256(data).hexdigest()}"


def object_path(vault_dir: Path, blob_hash: str) -> Path:
    """Resolve a blob hash to its filesystem path. Path may not yet exist."""
    if not blob_hash.startswith(_HASH_PREFIX):
        msg = f"blob_hash must be '{_HASH_PREFIX}<hex>', got {blob_hash!r}"
        raise ValueError(msg)
    hex_part = blob_hash.removeprefix(_HASH_PREFIX)
    if len(hex_part) != _HEX_LEN:
        msg = f"sha256 hex must be {_HEX_LEN} chars, got {len(hex_part)}"
        raise ValueError(msg)
    return vault_dir / "objects" / hex_part[:2] / hex_part[2:]


def write_object(vault_dir: Path, data: bytes) -> str:
    """Write ``data`` to the object store, return its content hash.

    Idempotent: if a file with the same hash already exists, this is a no-op.
    Atomic: bytes are written to a sibling temp file first, then renamed.
    """
    blob_hash = _hash_bytes(data)
    path = object_path(vault_dir, blob_hash)
    if path.exists():
        return blob_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(path)
    return blob_hash


def read_object(vault_dir: Path, blob_hash: str) -> bytes:
    """Read bytes by content hash. Raises ``FileNotFoundError`` if missing."""
    return object_path(vault_dir, blob_hash).read_bytes()


def object_exists(vault_dir: Path, blob_hash: str) -> bool:
    """Cheap existence probe — used by ``remember`` to validate
    blob-ref content without reading the bytes."""
    return object_path(vault_dir, blob_hash).is_file()


def object_size(vault_dir: Path, blob_hash: str) -> int:
    """Return the on-disk size for an existing blob.

    Raises ``FileNotFoundError`` if missing — caller's responsibility
    to gate with :func:`object_exists` first.
    """
    return object_path(vault_dir, blob_hash).stat().st_size


# ── streaming writer ─────────────────────────────────────────────────────


# Buffer for streamed writes — 1 MB balances syscall count vs RAM. With
# 256 KB we'd issue 4x as many write()s for a 1 MB upload; with 8 MB we'd
# spike per-request RAM for no measurable throughput gain.
_STREAM_BUFFER_BYTES = 1024 * 1024


class StreamingObjectWriter:
    """Incremental writer that computes sha256 + writes to a temp file.

    Built for the streaming-upload endpoint: bytes arrive in arbitrary
    chunk sizes from an HTTP receive() callable, are buffered, the hash
    is updated, and the bytes are flushed to a temp file in the object
    store's directory tree. ``finalize()`` does the atomic rename to the
    content-addressed location and returns the final blob_hash.

    Lifecycle:

        writer = StreamingObjectWriter(vault_dir)
        async for chunk in incoming:
            writer.feed(chunk)
        blob_hash = writer.finalize()

    On exception between feed() and finalize(): call ``abort()`` to
    drop the temp file. Idempotent — safe to call abort() after
    finalize() (no-op once the rename happened).
    """

    def __init__(self, vault_dir: Path) -> None:
        import secrets

        self._vault_dir = vault_dir
        self._hash = hashlib.sha256()
        self._size = 0
        # The temp file lives under objects/.tmp/ so a partial upload
        # never collides with a real blob's sharded prefix. Random
        # filename so concurrent writers don't step on each other.
        self._tmp_dir = vault_dir / "objects" / ".tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_path = self._tmp_dir / f"upload-{secrets.token_hex(8)}"
        self._fh = self._tmp_path.open("wb", buffering=_STREAM_BUFFER_BYTES)
        self._finalized = False

    def feed(self, chunk: bytes) -> None:
        """Append a chunk. Hash + write incrementally — no buffering of
        the whole payload."""
        if self._finalized:
            msg = "cannot feed() after finalize()"
            raise RuntimeError(msg)
        if not chunk:
            return
        self._hash.update(chunk)
        self._fh.write(chunk)
        self._size += len(chunk)

    @property
    def size(self) -> int:
        return self._size

    def finalize(self) -> str:
        """Close the temp file, atomic-rename to the content-addressed
        location, and return ``sha256:<hex>``.

        If a file with the same hash already exists (dedup), the temp is
        unlinked rather than renamed.
        """
        if self._finalized:
            msg = "finalize() called twice"
            raise RuntimeError(msg)
        self._fh.close()
        self._finalized = True
        blob_hash = f"{_HASH_PREFIX}{self._hash.hexdigest()}"
        final = object_path(self._vault_dir, blob_hash)
        if final.exists():
            # Dedup — drop the temp, keep the existing.
            self._tmp_path.unlink(missing_ok=True)
            return blob_hash
        final.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_path.replace(final)
        return blob_hash

    def abort(self) -> None:
        """Drop the temp file. Safe to call repeatedly; no-op after
        finalize()."""
        import contextlib

        if not self._finalized:
            with contextlib.suppress(OSError):
                self._fh.close()
        self._tmp_path.unlink(missing_ok=True)
        self._finalized = True

"""Content-addressed object store on the filesystem.

Layout::

    vault/objects/<aa>/<rest-of-sha256>

Where ``<aa>`` is the first two hex chars of the blob's sha256 (git-style
sharding) and ``<rest-of-sha256>`` is the remaining 62 hex chars.

The sha256 is computed on the PLAINTEXT bytes (the user's actual file).
When encryption is active (Stufe 1, ``set_blob_key`` was called at boot),
the on-disk bytes are an AES-256-GCM envelope: ``magic | nonce | ct+tag``.
Two writers uploading the same source file therefore land at the SAME
content address (because the plaintext sha is the same) but with
DIFFERENT ciphertext — so dedup works on the first writer's bytes;
subsequent identical uploads no-op via the ``path.exists()`` check.

When the vault key is not configured, blobs are written and read as
plaintext (local-dev convenience). Production refuses to boot without
a key (see :mod:`afair.settings`).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from .encryption import (
    decrypt_blob,
    derive_blob_aesgcm_key,
    encrypt_blob,
    looks_encrypted,
)

if TYPE_CHECKING:
    from pathlib import Path

_HASH_PREFIX = "sha256:"
_HEX_LEN = 64

# Cached derived blob-key for the active vault. Computed lazily on
# first use from the module-level master key, then memoised. Avoids
# re-running HKDF on every blob read/write (which would add ~1ms per
# operation for nothing).
_BLOB_KEY: bytes | None = None
_BLOB_KEY_DERIVED_FROM: bytes | None = None


def _blob_key_or_none() -> bytes | None:
    """Return the derived blob key, or None if encryption isn't active.

    Reads the master key from :mod:`afair.substrate.db` (single source
    of truth — the DB module owns the boot-time setter). Derived sub-key
    is memoised so repeated callers don't pay the KDF cost.
    """
    # Imported here to avoid a cycle (db imports nothing from objects,
    # objects imports the boot-time setter from db).
    from . import db as _db_module

    master = _db_module.get_vault_key()
    if master is None:
        return None
    global _BLOB_KEY, _BLOB_KEY_DERIVED_FROM
    if _BLOB_KEY is None or master != _BLOB_KEY_DERIVED_FROM:
        _BLOB_KEY = derive_blob_aesgcm_key(master)
        _BLOB_KEY_DERIVED_FROM = master
    return _BLOB_KEY


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

    Idempotent: if a file with the same hash (computed on plaintext)
    already exists, this is a no-op. Atomic: bytes are written to a
    sibling temp file first, then renamed.

    Encrypts the on-disk bytes with AES-256-GCM when the vault key is
    set. The content hash is computed on the PLAINTEXT input so dedup
    is preserved (same input file → same content address regardless of
    encryption state).
    """
    blob_hash = _hash_bytes(data)
    path = object_path(vault_dir, blob_hash)
    if path.exists():
        return blob_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    blob_key = _blob_key_or_none()
    on_disk_bytes = encrypt_blob(data, blob_key) if blob_key is not None else data

    tmp_path.write_bytes(on_disk_bytes)
    tmp_path.replace(path)
    return blob_hash


def read_object(vault_dir: Path, blob_hash: str) -> bytes:
    """Read bytes by content hash. Raises ``FileNotFoundError`` if missing.

    Transparently decrypts encrypted blobs (those with the AES-GCM
    envelope magic). Legacy plaintext blobs (no magic) are returned
    as-is — useful during the migration window, and harmless even
    after, because every blob written through :func:`write_object`
    after key setup has the magic prefix.
    """
    raw = object_path(vault_dir, blob_hash).read_bytes()
    blob_key = _blob_key_or_none()
    if blob_key is not None and looks_encrypted(raw[:4]):
        return decrypt_blob(raw, blob_key)
    return raw


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
    chunk sizes from an HTTP receive() callable, the hash is updated on
    plaintext, and the bytes are encrypted (if a vault key is configured)
    + flushed to a temp file. ``finalize()`` writes the AES-GCM tag,
    closes the file, and atomic-renames to the content-addressed
    location.

    On-disk layout when encryption is active::

        MAGIC (4 bytes) | NONCE (12 bytes) | CIPHERTEXT | TAG (16 bytes)

    Plaintext mode (no vault key): raw bytes only, no magic.

    The content hash is always computed on PLAINTEXT so dedup keys
    survive encryption.

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
        import os
        import secrets

        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )

        from .encryption import _BLOB_MAGIC, _BLOB_NONCE_LEN

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

        # Encryption state: lazily resolved per-writer, captured at
        # construction so a mid-upload key rotation can't corrupt the
        # tag computation. Encryptor is None in plaintext mode.
        self._blob_key = _blob_key_or_none()
        self._encryptor = None
        if self._blob_key is not None:
            nonce = os.urandom(_BLOB_NONCE_LEN)
            cipher = Cipher(algorithms.AES(self._blob_key), modes.GCM(nonce))
            self._encryptor = cipher.encryptor()
            # Write header (magic + nonce) before any ciphertext.
            self._fh.write(_BLOB_MAGIC)
            self._fh.write(nonce)

    def feed(self, chunk: bytes) -> None:
        """Append a chunk. Hash on plaintext + write (encrypted if key
        is set). No buffering of the whole payload."""
        if self._finalized:
            msg = "cannot feed() after finalize()"
            raise RuntimeError(msg)
        if not chunk:
            return
        self._hash.update(chunk)
        if self._encryptor is not None:
            self._fh.write(self._encryptor.update(chunk))
        else:
            self._fh.write(chunk)
        self._size += len(chunk)

    @property
    def size(self) -> int:
        return self._size

    def finalize(self) -> str:
        """Close the temp file, atomic-rename to the content-addressed
        location, and return ``sha256:<hex>``.

        When encryption is active, the AES-GCM authentication tag is
        flushed AFTER the final ciphertext chunk and BEFORE the file
        is closed — so the on-disk layout is ``magic || nonce || ct || tag``,
        matching what :func:`decrypt_blob` expects.

        If a file with the same hash already exists (dedup), the temp
        is unlinked rather than renamed.
        """
        if self._finalized:
            msg = "finalize() called twice"
            raise RuntimeError(msg)

        # Flush any remaining ciphertext + the GCM tag BEFORE closing.
        if self._encryptor is not None:
            self._fh.write(self._encryptor.finalize())
            self._fh.write(self._encryptor.tag)

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

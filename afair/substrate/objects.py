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

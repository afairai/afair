"""Vault-encryption primitives (Stufe 1).

The single env-provided ``AFAIR_VAULT_KEY`` is used for two distinct
crypto contexts: SQLCipher whole-file encryption of the SQLite database,
and AES-256-GCM per-blob encryption of the filesystem object store.
Each context derives its own key from the master via HKDF with a
context-specific ``info`` string. This isolates the two crypto paths
so a vulnerability in one cannot be replayed against the other, even
though both ultimately derive from the same secret.

Threat model this layer covers:

  - Disk-level exfiltration (stolen volume snapshot, leaked backup)
  - Cold-storage at-rest threats
  - Casual side-channel reads of the .db file or blob bytes

Threat model this layer does NOT cover:

  - Operator with shell access on the running machine (the process has
    the key in memory and serves plaintext to the intelligence layer)
  - Deliberate KEK retrieval from Fly secrets (audit-logged but not
    blocked — Stufe 2 narrows this further)
  - Inference attacks via the FTS5 index or embeddings (which encode
    the content semantically — note: SQLCipher encrypts FTS5 since the
    index lives inside the same .db file, but anything we serve to
    LLM providers is still in plaintext over the wire)

For Stufe 2+ designs (per-event payload encryption, BYOK, TEE), see
docs/operations.md §encryption-roadmap.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# HKDF context strings — domain-separate the two crypto paths.
# DO NOT change these in place; rotating the info string makes existing
# vaults unreadable. Versioned in case we ever need to migrate.
_SQLCIPHER_CONTEXT = b"afair.vault.v1.sqlcipher"
_BLOB_AESGCM_CONTEXT = b"afair.vault.v1.blob.aesgcm"

# Magic prefix prepended to encrypted blobs so we can:
#   1. detect plaintext blobs during the migration window
#   2. fail loudly on key-mismatch ("expected magic, got <garbage>")
#   3. version-bump the on-disk format without ambiguity
#
# 4 bytes: "AF01" — ascii "AF" + version "01". Next bytes are the
# 12-byte nonce, then the ciphertext+tag. Total overhead: 4 + 12 + 16
# = 32 bytes per blob.
_BLOB_MAGIC = b"AF01"
_BLOB_NONCE_LEN = 12
_BLOB_AESGCM_KEY_LEN = 32  # AES-256


class _NoKeyError(RuntimeError):
    """Raised when an encryption operation is requested but the vault
    key has not been configured. In prod the boot validator catches
    this earlier; this guard is defense-in-depth for code paths that
    might be exercised without going through Settings."""


def derive_sqlcipher_key(master_key: bytes) -> str:
    """HKDF-derive the SQLCipher PRAGMA-key from the master.

    Returns a hex string of length 64 (256 bits as 64 hex chars).
    SQLCipher accepts a raw-hex key via ``PRAGMA key = "x'..hex..'"``
    syntax — bypassing the per-database PBKDF2 derivation that
    SQLCipher would otherwise apply to a passphrase input. We've
    already done the KDF (HKDF) at the application layer; doing it
    again at the SQLCipher layer would be wasted cycles on every
    open. Raw-key mode is documented + supported.
    """
    if not master_key:
        msg = "vault_key is required for encrypted-vault operations"
        raise _NoKeyError(msg)

    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,  # HKDF salt-less is fine when the input is already
        # high-entropy (we require >= 32 random bytes upstream)
        info=_SQLCIPHER_CONTEXT,
    ).derive(master_key)
    return derived.hex()


def derive_blob_aesgcm_key(master_key: bytes) -> bytes:
    """HKDF-derive the AES-256-GCM key for blob encryption.

    Returned as raw 32 bytes (AES-256 key length). Caller wraps it in
    an :class:`AESGCM` instance and reuses across blobs.
    """
    if not master_key:
        msg = "vault_key is required for encrypted-vault operations"
        raise _NoKeyError(msg)

    return HKDF(
        algorithm=hashes.SHA256(),
        length=_BLOB_AESGCM_KEY_LEN,
        salt=None,
        info=_BLOB_AESGCM_CONTEXT,
    ).derive(master_key)


def encrypt_blob(plaintext: bytes, blob_key: bytes) -> bytes:
    """Encrypt blob bytes for at-rest storage.

    Returns ``magic || nonce || ciphertext+tag``. The nonce is freshly
    random per call — never reuse with the same key. AES-GCM
    authenticates the ciphertext, so corruption or wrong key surfaces
    as :class:`cryptography.exceptions.InvalidTag` on decrypt.

    The blob's content_hash continues to be computed on PLAINTEXT
    (upstream of this function), which preserves dedup semantics:
    two writers uploading the same source file get the same content
    address, even though each encryption call produces a different
    ciphertext. This means we encrypt once on the first write and
    subsequent identical uploads dedup to the existing ciphertext.
    """
    nonce = os.urandom(_BLOB_NONCE_LEN)
    aesgcm = AESGCM(blob_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return _BLOB_MAGIC + nonce + ciphertext


def decrypt_blob(envelope: bytes, blob_key: bytes) -> bytes:
    """Decrypt a blob envelope written by :func:`encrypt_blob`.

    Raises:
        ValueError: envelope is malformed or unencrypted (no magic
            header).
        cryptography.exceptions.InvalidTag: wrong key or corrupted
            ciphertext.
    """
    if len(envelope) < len(_BLOB_MAGIC) + _BLOB_NONCE_LEN + 16:
        msg = (
            f"blob envelope is too short ({len(envelope)} bytes); "
            f"expected magic + nonce + ciphertext + tag"
        )
        raise ValueError(msg)
    magic = envelope[: len(_BLOB_MAGIC)]
    if magic != _BLOB_MAGIC:
        msg = (
            f"blob envelope missing magic prefix; got {magic!r}, "
            f"expected {_BLOB_MAGIC!r}. Likely an unencrypted blob "
            f"from before the encryption migration ran."
        )
        raise ValueError(msg)
    nonce = envelope[len(_BLOB_MAGIC) : len(_BLOB_MAGIC) + _BLOB_NONCE_LEN]
    ciphertext = envelope[len(_BLOB_MAGIC) + _BLOB_NONCE_LEN :]
    aesgcm = AESGCM(blob_key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def looks_encrypted(envelope_head: bytes) -> bool:
    """Cheap probe — does the first 4 bytes match our magic header?

    Used during the migration window to distinguish unencrypted legacy
    blobs from encrypted ones. Probe with a 4-byte read, not the full
    file, to avoid unnecessary I/O on the migration scan.
    """
    return envelope_head[: len(_BLOB_MAGIC)] == _BLOB_MAGIC


def key_fingerprint(master_key: bytes) -> str:
    """A short identifier derived from the key — for logging without
    leaking the key itself. Use to confirm two contexts share the
    same key (e.g., the migration script vs the running server) at
    boot, without echoing the actual secret.

    16 hex chars (64-bit truncated SHA-256). Collision-resistant
    enough for "did we open the right vault" sanity checks; NOT a
    cryptographic identifier.
    """
    if not master_key:
        return "<no-key>"
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"afair.vault.fingerprint.v1")
    digest.update(master_key)
    return digest.finalize().hex()[:16]

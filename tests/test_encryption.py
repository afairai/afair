"""Tests for the Stufe 1 encryption layer.

Covers:
  - HKDF sub-key derivation is deterministic + domain-separated
  - encrypt_blob / decrypt_blob round-trip
  - decrypt_blob refuses wrong key (InvalidTag)
  - decrypt_blob refuses unencrypted bytes (ValueError on magic miss)
  - SQLCipher round-trip via open_db with a key
  - SQLCipher reads fail when the key is wrong
  - sqlite-vec extension still loads after SQLCipher takes over
  - StreamingObjectWriter round-trips via write+read with encryption on
  - StreamingObjectWriter falls back to plaintext when no key
  - write_object / read_object transparently encrypt + decrypt
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
from cryptography.exceptions import InvalidTag

if TYPE_CHECKING:
    from pathlib import Path

from afair.substrate import open_db
from afair.substrate.db import set_vault_key
from afair.substrate.encryption import (
    decrypt_blob,
    derive_blob_aesgcm_key,
    derive_sqlcipher_key,
    encrypt_blob,
    key_fingerprint,
    looks_encrypted,
)
from afair.substrate.objects import (
    StreamingObjectWriter,
    read_object,
    write_object,
)

# SQLCipher only has wheels for Linux. The SQLite-encryption tests
# below skip on macOS / Windows (the rest of the encryption stack —
# blob AES-GCM, HKDF, StreamingObjectWriter — works everywhere).
try:
    import sqlcipher3  # type: ignore[import-untyped]  # noqa: F401

    SQLCIPHER_AVAILABLE = True
except ImportError:
    SQLCIPHER_AVAILABLE = False

needs_sqlcipher = pytest.mark.skipif(
    not SQLCIPHER_AVAILABLE,
    reason="sqlcipher3 is not installed (Linux-only wheel)",
)


def _new_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture(autouse=True)
def _reset_vault_key():
    """Reset the module-level key between tests so order doesn't matter."""
    set_vault_key(None)
    yield
    set_vault_key(None)


# ── encryption primitives ──────────────────────────────────────────────────


def test_derive_sqlcipher_key_is_deterministic_and_hex():
    master = _new_key()
    a = derive_sqlcipher_key(master)
    b = derive_sqlcipher_key(master)
    assert a == b
    assert len(a) == 64
    int(a, 16)  # raises if not valid hex


def test_derive_blob_key_is_deterministic_and_32_bytes():
    master = _new_key()
    a = derive_blob_aesgcm_key(master)
    b = derive_blob_aesgcm_key(master)
    assert a == b
    assert len(a) == 32


def test_sqlcipher_and_blob_keys_are_domain_separated():
    """Same master → DIFFERENT derived sub-keys for each context.

    Domain separation via HKDF info-string is what protects us from a
    cipher vulnerability in one context being replayed in the other.
    """
    master = _new_key()
    sqlcipher_key = bytes.fromhex(derive_sqlcipher_key(master))
    blob_key = derive_blob_aesgcm_key(master)
    assert sqlcipher_key != blob_key


def test_blob_roundtrip():
    key = derive_blob_aesgcm_key(_new_key())
    plaintext = b"Hello, world. Some sensitive memory content."
    envelope = encrypt_blob(plaintext, key)
    # Envelope has the magic prefix.
    assert looks_encrypted(envelope[:4])
    # Round-trip.
    assert decrypt_blob(envelope, key) == plaintext


def test_blob_decrypt_wrong_key_raises_invalid_tag():
    plaintext = b"Hello, world."
    envelope = encrypt_blob(plaintext, derive_blob_aesgcm_key(_new_key()))
    other_key = derive_blob_aesgcm_key(_new_key())
    with pytest.raises(InvalidTag):
        decrypt_blob(envelope, other_key)


def test_blob_decrypt_unencrypted_raises_value_error():
    """Plaintext bytes don't have the magic prefix — fail loudly."""
    key = derive_blob_aesgcm_key(_new_key())
    with pytest.raises(ValueError, match="missing magic prefix"):
        decrypt_blob(b"this is not an encrypted blob, just text" * 2, key)


def test_each_encrypt_uses_a_fresh_nonce():
    """Two calls with the same input + key must yield different ciphertext.

    AES-GCM is catastrophically broken under nonce reuse. The encrypt_blob
    contract is that nonces are random per call; this test asserts the
    contract.
    """
    key = derive_blob_aesgcm_key(_new_key())
    plaintext = b"the same content"
    e1 = encrypt_blob(plaintext, key)
    e2 = encrypt_blob(plaintext, key)
    assert e1 != e2
    # Both decrypt back to the same plaintext.
    assert decrypt_blob(e1, key) == plaintext
    assert decrypt_blob(e2, key) == plaintext


def test_key_fingerprint_is_stable_and_short():
    master = _new_key()
    fp1 = key_fingerprint(master)
    fp2 = key_fingerprint(master)
    assert fp1 == fp2
    assert len(fp1) == 16
    assert key_fingerprint(_new_key()) != fp1


# ── SQLCipher integration ─────────────────────────────────────────────────


@needs_sqlcipher
def test_sqlcipher_open_db_writes_then_reads_back(tmp_path: Path):
    """Open with key, write a row, close, reopen with same key, read."""
    key = _new_key()
    set_vault_key(key)

    db = open_db(tmp_path)
    with db:
        db.execute("CREATE TABLE smoke (k TEXT, v TEXT)")
        db.execute("INSERT INTO smoke VALUES (?, ?)", ("hello", "world"))
    db.close()

    # Reopen with the same key and check the row survived.
    db = open_db(tmp_path)
    row = db.execute("SELECT v FROM smoke WHERE k = ?", ("hello",)).fetchone()
    assert row["v"] == "world"
    db.close()


@needs_sqlcipher
def test_sqlcipher_wrong_key_fails_to_open(tmp_path: Path):
    """Open with key A, close, reopen with key B → RuntimeError."""
    key_a = _new_key()
    key_b = _new_key()

    set_vault_key(key_a)
    db = open_db(tmp_path)
    with db:
        db.execute("CREATE TABLE smoke (k TEXT)")
        db.execute("INSERT INTO smoke VALUES (?)", ("x",))
    db.close()
    set_vault_key(None)

    set_vault_key(key_b)
    with pytest.raises(RuntimeError, match="SQLCipher failed to open"):
        open_db(tmp_path)


def test_plaintext_mode_when_no_key(tmp_path: Path):
    """With no key set, open_db falls back to stdlib sqlite3."""
    set_vault_key(None)
    db = open_db(tmp_path)
    db.execute("CREATE TABLE smoke (k TEXT)")
    db.execute("INSERT INTO smoke VALUES (?)", ("x",))
    db.commit()
    db.close()

    # The file is a real SQLite database (magic header).
    header = (tmp_path / "substrate.db").read_bytes()[:16]
    assert header == b"SQLite format 3\0"


@needs_sqlcipher
def test_sqlite_vec_loads_with_sqlcipher(tmp_path: Path):
    """The sqlite-vec extension must work inside SQLCipher's SQLite."""
    set_vault_key(_new_key())
    db = open_db(tmp_path, embedding_dim=4)
    # The events_vec virtual table is created at init_db time. Verify
    # we can write + read a tiny vector through it.
    import struct

    blob = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
    with db:
        db.execute(
            "INSERT INTO events_vec(content_hash, embedding) VALUES (?, ?)",
            ("sha256:" + "0" * 64, blob),
        )
    row = db.execute(
        "SELECT embedding FROM events_vec WHERE content_hash = ?",
        ("sha256:" + "0" * 64,),
    ).fetchone()
    assert row is not None
    assert struct.unpack("<4f", row["embedding"]) == pytest.approx(
        (0.1, 0.2, 0.3, 0.4)
    )
    db.close()


# ── object store integration ──────────────────────────────────────────────


def test_write_read_object_encrypted_roundtrip(tmp_path: Path):
    """write_object encrypts, read_object decrypts. Hash is on plaintext."""
    set_vault_key(_new_key())
    plaintext = b"PDF text content or any other blob."
    blob_hash = write_object(tmp_path, plaintext)

    # The on-disk bytes are NOT plaintext.
    from afair.substrate.objects import object_path

    on_disk = object_path(tmp_path, blob_hash).read_bytes()
    assert on_disk != plaintext
    assert looks_encrypted(on_disk[:4])

    # read_object transparently decrypts.
    assert read_object(tmp_path, blob_hash) == plaintext


def test_write_read_object_plaintext_roundtrip(tmp_path: Path):
    """Without a key, blobs stay plaintext on disk."""
    set_vault_key(None)
    plaintext = b"plaintext blob"
    blob_hash = write_object(tmp_path, plaintext)

    from afair.substrate.objects import object_path

    on_disk = object_path(tmp_path, blob_hash).read_bytes()
    assert on_disk == plaintext


def test_read_object_legacy_plaintext_still_readable_with_key(tmp_path: Path):
    """During migration, plaintext blobs written before encryption was
    enabled must still be readable. The magic-probe in read_object
    passes them through untouched."""
    set_vault_key(None)
    legacy_data = b"plaintext blob from before encryption"
    blob_hash = write_object(tmp_path, legacy_data)

    # Enable encryption AFTER the write.
    set_vault_key(_new_key())
    # read_object returns the plaintext as-is (no magic = passthrough).
    assert read_object(tmp_path, blob_hash) == legacy_data


def test_streaming_writer_encrypted_roundtrip(tmp_path: Path):
    """Streaming chunks → encrypted file → read_object decrypts."""
    set_vault_key(_new_key())
    payload = b"A" * 1_500_000 + b"B" * 300_000  # 1.8 MB, multi-chunk

    writer = StreamingObjectWriter(tmp_path)
    # Feed in arbitrary-sized chunks (simulates HTTP stream).
    for i in range(0, len(payload), 64 * 1024):
        writer.feed(payload[i : i + 64 * 1024])
    blob_hash = writer.finalize()

    # On-disk has the magic prefix.
    from afair.substrate.objects import object_path

    on_disk = object_path(tmp_path, blob_hash).read_bytes()
    assert looks_encrypted(on_disk[:4])
    # On-disk size = 4 magic + 12 nonce + len(payload) + 16 tag.
    assert len(on_disk) == 4 + 12 + len(payload) + 16

    # read_object decrypts back to the original.
    assert read_object(tmp_path, blob_hash) == payload


def test_streaming_writer_plaintext_when_no_key(tmp_path: Path):
    set_vault_key(None)
    payload = b"streaming plaintext content"
    writer = StreamingObjectWriter(tmp_path)
    writer.feed(payload)
    blob_hash = writer.finalize()

    from afair.substrate.objects import object_path

    on_disk = object_path(tmp_path, blob_hash).read_bytes()
    assert on_disk == payload
    assert read_object(tmp_path, blob_hash) == payload


def test_streaming_writer_hash_is_on_plaintext(tmp_path: Path):
    """Same plaintext → same content hash regardless of encryption.

    This is the dedup invariant: an existing plaintext blob's hash
    must match a new encrypted upload of the same content.
    """
    plaintext = b"identical content"

    set_vault_key(None)
    h_plain = write_object(tmp_path / "plain", plaintext)

    set_vault_key(_new_key())
    writer = StreamingObjectWriter(tmp_path / "encrypted")
    writer.feed(plaintext)
    h_enc = writer.finalize()

    assert h_plain == h_enc

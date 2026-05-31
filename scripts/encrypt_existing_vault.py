#!/usr/bin/env python3
"""Migrate an existing plaintext vault to encrypted-at-rest.

Walks the configured vault directory (defaults to ``settings.vault_dir``)
and:

  1. Detects whether ``substrate.db`` is already encrypted; if not,
     uses SQLCipher's ``sqlcipher_export`` to write an encrypted copy
     in-place via a sibling tempfile + atomic rename.
  2. Walks every blob under ``objects/`` and, for each blob that does
     NOT begin with the AES-GCM magic header, rewrites it as an
     encrypted envelope via temp file + atomic rename.

Idempotent: re-running detects already-encrypted state and skips. Safe
to interrupt and resume — partial state is recoverable because each
file is rewritten atomically. The original vault is preserved as a
``.pre-encrypt`` backup directory at the same parent path; verify the
post-migration vault works, then ``rm -rf`` the backup.

Usage::

    AFAIR_VAULT_KEY=<base64-key> python scripts/encrypt_existing_vault.py
    # or
    AFAIR_VAULT_KEY=<base64-key> python scripts/encrypt_existing_vault.py --dry-run

Run against a live Fly machine via::

    fly ssh console -a afair -C "python scripts/encrypt_existing_vault.py"

The vault key MUST already be set as a Fly secret BEFORE running this
script there: ``fly secrets set AFAIR_VAULT_KEY=$(python -c 'import
secrets; print(secrets.token_urlsafe(32))') -a afair``. Once data is
encrypted with a key, losing the key means losing the data — back up
the key in ``.env.secrets.backup`` first.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3 as _stdlib_sqlite
import sys
from pathlib import Path

# Make project importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from afair.settings import load_settings  # noqa: E402
from afair.substrate.encryption import (  # noqa: E402
    derive_blob_aesgcm_key,
    derive_sqlcipher_key,
    encrypt_blob,
    key_fingerprint,
    looks_encrypted,
)


def _is_encrypted_sqlite(db_path: Path) -> bool:
    """Probe whether ``substrate.db`` is encrypted by SQLCipher.

    Stdlib sqlite3 can open a plaintext SQLite file directly. A SQLCipher
    file looks like garbage to stdlib (the first 16 bytes of a normal
    SQLite file are the magic header ``SQLite format 3\\0``). Encrypted
    files start with random bytes.
    """
    if not db_path.exists():
        return False
    header = db_path.read_bytes()[:16]
    return header != b"SQLite format 3\0"


def _encrypt_sqlite(db_path: Path, hex_key: str, *, dry_run: bool) -> bool:
    """Encrypt the SQLite database in-place. Returns True on action,
    False if no-op (already encrypted)."""
    if _is_encrypted_sqlite(db_path):
        print(f"  [skip] {db_path.name} is already encrypted")
        return False

    if dry_run:
        print(f"  [dry-run] would encrypt {db_path.name}")
        return True

    print(f"  [encrypt] {db_path.name}")
    # Use sqlcipher3's sqlcipher_export to copy the plaintext DB into
    # an encrypted attached DB. This preserves FTS5 + sqlite-vec virtual
    # tables since it's a logical copy at the SQLite layer.
    import sqlcipher3  # type: ignore[import-untyped]

    tmp_path = db_path.with_suffix(".db.encrypting")
    if tmp_path.exists():
        tmp_path.unlink()

    src = _stdlib_sqlite.connect(str(db_path))
    try:
        # sqlcipher3 connection to the empty target file, set its key,
        # then have the SOURCE attach the target and dump itself.
        # Order matters: target file must be opened by SQLCipher first
        # (with the key) so it's correctly cipher-initialized.
        target = sqlcipher3.connect(str(tmp_path))
        target.execute(f"PRAGMA key = \"x'{hex_key}'\"")
        # A trivial statement forces SQLCipher to write the header.
        target.execute("CREATE TABLE _seed (x INT)")
        target.execute("DROP TABLE _seed")
        target.commit()
        target.close()

        # Now from the plaintext source, attach the encrypted target +
        # invoke sqlcipher_export to copy the entire schema + data.
        src.enable_load_extension(False)  # safety: don't load any ext here
        src.execute(f"ATTACH DATABASE '{tmp_path}' AS encrypted KEY \"x'{hex_key}'\"")
        src.execute("SELECT sqlcipher_export('encrypted')")
        src.execute("DETACH DATABASE encrypted")
    finally:
        src.close()

    # Atomic rename: replace plaintext with encrypted.
    tmp_path.replace(db_path)
    return True


def _encrypt_blob_file(path: Path, blob_key: bytes, *, dry_run: bool) -> bool:
    """Encrypt one blob file in-place. Returns True on action, False if
    already encrypted."""
    with path.open("rb") as fh:
        head = fh.read(4)
    if looks_encrypted(head):
        return False
    if dry_run:
        return True
    plaintext = path.read_bytes()
    envelope = encrypt_blob(plaintext, blob_key)
    tmp_path = path.with_suffix(path.suffix + ".encrypting")
    tmp_path.write_bytes(envelope)
    tmp_path.replace(path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change, but do not modify any files.",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Don't create a .pre-encrypt copy of the vault. Faster, "
        "but if encryption corrupts something you cannot roll back.",
    )
    args = parser.parse_args()

    settings = load_settings()
    if settings.vault_key is None:
        print("ERROR: AFAIR_VAULT_KEY is not set. Generate one with:")
        print("  python -c 'import secrets; print(secrets.token_urlsafe(32))'")
        print("Then set it as a Fly secret AND in .env.secrets.backup.")
        return 2

    master_key = settings.vault_key.get_secret_value().encode("utf-8")
    sqlcipher_hex_key = derive_sqlcipher_key(master_key)
    blob_key = derive_blob_aesgcm_key(master_key)

    print(f"vault_dir: {settings.vault_dir}")
    print(f"key fingerprint: {key_fingerprint(master_key)}")
    print(f"dry-run: {args.dry_run}")
    print()

    # Step 0: backup.
    if not args.dry_run and not args.skip_backup:
        backup_path = settings.vault_dir.with_suffix(".pre-encrypt")
        if backup_path.exists():
            print(
                f"WARNING: backup path {backup_path} already exists; "
                f"skipping backup step (assume previous interrupted run)."
            )
        else:
            print(f"[backup] copying vault to {backup_path}")
            shutil.copytree(settings.vault_dir, backup_path)
    elif args.dry_run:
        print(f"[dry-run] would back up vault to {settings.vault_dir.with_suffix('.pre-encrypt')}")
    else:
        print("[skip-backup] --skip-backup was set; no rollback path")

    # Step 1: SQLite database.
    print("\n[sqlite]")
    db_path = settings.vault_dir / "substrate.db"
    if not db_path.exists():
        print(f"  [skip] {db_path} does not exist; nothing to do")
    else:
        _encrypt_sqlite(db_path, sqlcipher_hex_key, dry_run=args.dry_run)

    # Step 2: blob store.
    print("\n[blobs]")
    objects_dir = settings.vault_dir / "objects"
    if not objects_dir.exists():
        print(f"  [skip] {objects_dir} does not exist; nothing to do")
    else:
        encrypted = 0
        skipped = 0
        for blob_path in objects_dir.rglob("*"):
            if not blob_path.is_file():
                continue
            # Skip temp files under objects/.tmp/.
            if ".tmp" in blob_path.parts:
                continue
            if _encrypt_blob_file(blob_path, blob_key, dry_run=args.dry_run):
                encrypted += 1
            else:
                skipped += 1
        action = "would encrypt" if args.dry_run else "encrypted"
        print(f"  {action}: {encrypted}    already encrypted: {skipped}")

    print("\nDone. Verify the vault opens with the new key by running:")
    print("  python -m afair.admin backfill-dry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

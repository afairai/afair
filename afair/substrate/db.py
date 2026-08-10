"""SQLite connection management — open, configure pragmas, load extensions, init schema.

When the vault key is set (via :func:`set_vault_key` at boot, or via the
explicit ``vault_key`` arg), SQLCipher is used to encrypt the entire
database file (AES-256). The PRAGMA key is the HKDF-derived sub-key from
:func:`afair.substrate.encryption.derive_sqlcipher_key`, not the raw
master key.

When no key is configured, stdlib sqlite3 is used and the database
lives on disk in plaintext. Production refuses to boot without a key
(see :class:`afair.settings.Settings`).

Boot-time setter pattern: production calls :func:`set_vault_key` once
during server startup; subsequent :func:`open_db` calls pick the key up
implicitly. This avoids threading the key through every call site
(MCP handlers, agents, admin commands, OAuth routes). Tests that want
explicit control can still pass ``vault_key=...`` per call.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3 as _stdlib_sqlite
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import sqlite_vec  # type: ignore[import-untyped]

from .encryption import derive_sqlcipher_key
from .kinds import seed_bootstrap_kinds
from .schema import (
    SCHEMA_DDL,
    VEC_DDL,
    migrate_proposed_corrections_kind_check,
    migrate_proposed_corrections_open_unique,
)

if TYPE_CHECKING:
    from pathlib import Path


# Module-level vault key, set once at boot. Read by open_db when no
# explicit key is passed. None means "plaintext mode" — fine in local
# dev and tests, refused in production by the boot validator.
_VAULT_KEY: bytes | None = None
# Sentinel: distinguishes "caller passed None explicitly (force plaintext)"
# from "caller didn't pass the arg (use module default)".
_USE_MODULE_DEFAULT = object()


def set_vault_key(key: bytes | None) -> None:
    """Install the vault key for subsequent :func:`open_db` calls.

    Called once during server startup from the boot wiring. Idempotent:
    safe to call again with the same key. Calling with a DIFFERENT key
    is a programming error (different keys would mean different vaults)
    and raises.

    Tests use this to set up encrypted test vaults; per-call override
    via the ``vault_key`` arg on :func:`open_db` is the alternative.
    """
    global _VAULT_KEY
    if _VAULT_KEY is not None and key is not None and key != _VAULT_KEY:
        msg = (
            "set_vault_key called with a different key than was previously "
            "set. This would point new connections at a different cipher "
            "context than already-open ones — refusing to proceed."
        )
        raise RuntimeError(msg)
    _VAULT_KEY = key


def get_vault_key() -> bytes | None:
    """Read the currently installed vault key (or None for plaintext mode)."""
    return _VAULT_KEY


def open_db(
    vault_dir: Path,
    *,
    embedding_dim: int = 1536,
    vault_key: bytes | None | object = _USE_MODULE_DEFAULT,
) -> _stdlib_sqlite.Connection:
    """Open (and create if missing) the substrate SQLite file.

    Idempotent: safe to call on an existing populated vault. Creates the
    vault directory and the ``objects/`` subdir if they do not yet exist.
    Loads the sqlite-vec extension and creates the ``events_vec`` virtual
    table with the configured embedding dimension.

    Args:
        vault_dir: Path to the vault root directory.
        embedding_dim: Dimension of stored vectors (must match the
            embedding model in use).
        vault_key: When provided (bytes), open via SQLCipher with that
            key. When ``None``, force plaintext mode (skip the module
            default — useful for tests). When omitted, fall back to
            whatever was installed via :func:`set_vault_key`.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "objects").mkdir(parents=True, exist_ok=True)

    # Resolve which key to use for this connection.
    effective_key: bytes | None
    if vault_key is _USE_MODULE_DEFAULT:
        effective_key = _VAULT_KEY
    else:
        # mypy: the sentinel branch is handled; remaining type is
        # ``bytes | None`` by signature.
        effective_key = cast("bytes | None", vault_key)

    db_path = vault_dir / "substrate.db"

    # _open_connection installs the right row_factory for the
    # underlying module (stdlib sqlite3.Row rejects sqlcipher3 cursors
    # and vice-versa, so it must match the connection's origin).
    conn = _open_connection(db_path, vault_key=effective_key)

    # Pragmas — durability + concurrency + performance.
    #
    # busy_timeout MUST be set first: the engine consults it on every
    # subsequent lock attempt, including the next PRAGMA. Setting
    # journal_mode = WAL briefly requires an exclusive lock on the file;
    # without the busy timeout in place, two concurrent open_db calls on
    # the same vault (admin backfill running alongside the server, two
    # tests sharing a tmp_path, even just the test runner re-opening
    # during teardown on a slow disk) race and one raises
    # "database is locked" immediately. With the timeout set first the
    # second opener simply waits its turn. Was the source of an
    # occasional CI flake on test_observe_via_mcp_protocol.
    conn.execute("PRAGMA busy_timeout = 5000")
    # The delete→WAL flip needs a moment with no other active connection,
    # and it only ever happens on a vault's very FIRST open — once the file
    # is WAL, this PRAGMA is a lock-free read-back. Two concurrent first
    # opens (boot-warmup thread next to the first request) can both attempt
    # the flip, and SQLite reports the loser immediately: the busy handler
    # doesn't cover every lock acquisition inside the journal-mode
    # transition. Bounded retry — by the next attempt the winner has
    # flipped the file and this degrades to the read-back.
    for attempt in range(10):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            break
        except Exception as e:  # sqlcipher3 raises its own OperationalError class
            if "database is locked" not in str(e) or attempt == 9:
                raise
            time.sleep(0.01 * (attempt + 1))
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    # Memory-mapped reads — SQLite mmaps the database file up to this many
    # bytes. Reads skip the OS-cache→userspace copy when the page is hot.
    # 256MB is far larger than the current vault but cheap (only mapped
    # pages are actually paged in).
    conn.execute("PRAGMA mmap_size = 268435456")  # 256MB
    # Page cache — negative value means kilobytes (positive = pages).
    # -16384 ≈ 16MB of recent pages kept hot in this connection's cache.
    # This is PER-CONNECTION, and public-launch concurrency opens many
    # connections at once (the 8-thread recall pool + extractor + cold-path
    # + checkpoint + per-internal-route). At the old 64MB the worst-case
    # resident cache across ~10 connections approached the 1GB VM ceiling —
    # an OOM under a traffic spike. 16MB is ample for the small per-recall
    # working set and keeps the aggregate well within budget. (Perf NEW-6.)
    conn.execute("PRAGMA cache_size = -16384")
    # Run the planner's auto-tune. Cheap to call repeatedly; SQLite skips
    # the work when nothing has changed since the last optimize.
    conn.execute("PRAGMA optimize")

    # Load sqlite-vec extension (provides the vec0 virtual table type).
    # Must happen before the vec DDL runs. SQLCipher exposes the same
    # loadable-extension API as upstream SQLite, so this path is identical
    # in encrypted and plaintext mode.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    init_db(conn, embedding_dim=embedding_dim)
    return conn


def _open_connection(
    db_path: Path,
    *,
    vault_key: bytes | None,
) -> _stdlib_sqlite.Connection:
    """Open the underlying SQLite connection, encrypted or plaintext.

    Returns a connection that — to every caller below — is functionally
    identical to a stdlib sqlite3 connection. SQLCipher's ``sqlcipher3``
    package is a drop-in API replacement, so we type-cast for mypy.

    PRAGMA key MUST be the first command executed on a SQLCipher
    connection, before any other read/write. Even a single ``SELECT`` on
    the unkeyed connection corrupts the SQLCipher cipher state for the
    rest of the session. Hence: open, key, return — no other operations
    in between.

    Raw-hex key syntax (``PRAGMA key = "x'..hex..'"``) bypasses
    SQLCipher's PBKDF2 key derivation; we've done HKDF at the
    application layer already (see encryption.py), and double-deriving
    would waste cycles on every open without adding security.
    """
    if vault_key is not None:
        # Import lazily so local dev (no SQLCipher installed) still works
        # when vault_key is None. Production has the dep so this import
        # never raises.
        try:
            # sqlcipher3 has no PyPI stubs. Bare `type: ignore` avoids the
            # "unused-ignore" complaint that comes from per-platform mypy
            # divergence (Linux: import-untyped; macOS: import-not-found).
            import sqlcipher3  # type: ignore
        except ImportError as exc:
            msg = (
                "vault_key is set but sqlcipher3 is not installed. "
                "Run `uv sync` to install dependencies (sqlcipher3-binary "
                "ships a prebuilt wheel; no system SQLCipher required)."
            )
            raise RuntimeError(msg) from exc

        conn = cast(
            "_stdlib_sqlite.Connection",
            sqlcipher3.connect(str(db_path), check_same_thread=False),
        )
        # sqlcipher3 has its own Row class — using stdlib sqlite3.Row
        # here would raise TypeError("Row() argument 1 must be
        # sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor") on the first
        # fetch. Set the matching factory before any row is read.
        conn.row_factory = sqlcipher3.Row
        # Raw-hex key, see docstring above.
        hex_key = derive_sqlcipher_key(vault_key)
        conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
        # Reading sqlite_version (or any other table) verifies the key
        # is correct — wrong key gives "file is not a database" here.
        # Fail loud at boot rather than silently 100 queries deep.
        # Catch BOTH error classes: stdlib sqlite3.DatabaseError and
        # sqlcipher3's parallel DatabaseError (they don't share a base).
        try:
            cast("Any", conn).execute("SELECT count(*) FROM sqlite_master").fetchone()
        except (_stdlib_sqlite.DatabaseError, sqlcipher3.DatabaseError) as exc:
            msg = (
                "SQLCipher failed to open the database with the provided "
                "AFAIR_VAULT_KEY. Either the key is wrong, the file is "
                "not encrypted (run scripts/encrypt_existing_vault.py "
                "first), or the file is corrupt."
            )
            raise RuntimeError(msg) from exc
        return conn

    plain_conn = _stdlib_sqlite.connect(str(db_path), check_same_thread=False)
    plain_conn.row_factory = _stdlib_sqlite.Row
    return plain_conn


@lru_cache(maxsize=8)
def _schema_fingerprint(embedding_dim: int) -> int:
    """31-bit digest of the exact DDL this build would execute.

    Stored in ``PRAGMA user_version`` after a successful DDL pass so later
    opens can skip the pass entirely (see :func:`init_db`). Computed from
    the DDL strings themselves (with the vec templates rendered at the
    given dim), so ANY schema change — a new table in an upgrade, a
    different embedding dim — changes the digest and re-arms the guarded
    path automatically; nothing to remember to bump. Never 0: 0 is
    SQLite's default user_version, i.e. "no pass recorded yet".
    """
    rendered = "\n".join([*SCHEMA_DDL, *(t.format(dim=embedding_dim) for t in VEC_DDL)])
    digest = hashlib.sha256(rendered.encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") & 0x7FFFFFFF) or 1


def init_db(
    conn: _stdlib_sqlite.Connection,
    *,
    embedding_dim: int = 1536,
) -> None:
    """Idempotent DDL execution. Safe on fresh or populated databases.

    The vector table's dimension is bound at creation; if it doesn't match
    the embedding_dim passed here AFTER initial creation, queries against
    the existing table still work but stored vectors are at the original
    dimension. To change dim mid-life, manually drop and recreate the table.

    Fast path: a vault whose ``PRAGMA user_version`` already carries this
    build's schema fingerprint skips the DDL pass — no write lock, no
    dozens of no-op IF NOT EXISTS statements — so routine per-thread
    connects on a live vault stay read-only here.

    Slow path (fingerprint mismatch: fresh vault, upgraded build, changed
    dim) runs the DDL as a WRITER (BEGIN IMMEDIATE), not deferred: two
    concurrent first opens of the same vault (the boot-warmup thread next
    to the first request; an admin backfill next to the server) race
    deferred transactions into an instant "database is locked" — SQLite
    skips the busy handler on a read→write lock upgrade because waiting
    could deadlock, so busy_timeout never gets a say. Declaring write
    intent up front parks the second opener in the busy handler until the
    first commits; its DDL then no-ops on IF NOT EXISTS. The fingerprint
    is stamped inside the same transaction, so a failed pass re-runs in
    full on the next open.
    """
    fingerprint = _schema_fingerprint(embedding_dim)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current != fingerprint:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in SCHEMA_DDL:
                conn.execute(stmt)
            # Vector DDL is parameterized by embedding_dim — render once at boot.
            for stmt_template in VEC_DDL:
                conn.execute(stmt_template.format(dim=embedding_dim))
            # PRAGMA accepts no bind parameters; fingerprint is our own int.
            conn.execute(f"PRAGMA user_version = {fingerprint}")
        except BaseException:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    # ADR-0004: widen the proposed_corrections.kind CHECK on pre-existing vaults
    # so 'edge_review' proposals can be inserted. Guarded + idempotent; a no-op
    # on fresh vaults (SCHEMA_DDL already ships the widened CHECK) and on
    # already-migrated ones. Runs in its own transaction inside the helper.
    migrate_proposed_corrections_kind_check(conn)
    # P1-1: replace the table-level UNIQUE(kind, entity_id) with a partial
    # unique index on OPEN rows only, so a decided proposal no longer blocks
    # every future proposal for the same (kind, subject) forever (that froze
    # ADR-0004 edge-review calibration growth). Guarded + idempotent; runs AFTER
    # the kind-check widen (which may already rebuild to the final shape).
    migrate_proposed_corrections_open_unique(conn)
    # Bootstrap the kind registry (ADR-0003 Phase 1). Idempotent — a
    # fresh vault gets the seven seeded, an existing vault gains the
    # registry on its next open, an already-seeded vault no-ops.
    seed_bootstrap_kinds(conn)

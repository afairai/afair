"""SQLite connection management — open, configure pragmas, load extensions, init schema."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import sqlite_vec  # type: ignore[import-untyped]

from .schema import SCHEMA_DDL, VEC_DDL

if TYPE_CHECKING:
    from pathlib import Path


def open_db(vault_dir: Path, *, embedding_dim: int = 1536) -> sqlite3.Connection:
    """Open (and create if missing) the substrate SQLite file.

    Idempotent: safe to call on an existing populated vault. Creates the
    vault directory and the ``objects/`` subdir if they do not yet exist.
    Loads the sqlite-vec extension and creates the ``events_vec`` virtual
    table with the configured embedding dimension.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "objects").mkdir(parents=True, exist_ok=True)

    db_path = vault_dir / "substrate.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    # Memory-mapped reads — SQLite mmaps the database file up to this many
    # bytes. Reads skip the OS-cache→userspace copy when the page is hot.
    # 256MB is far larger than the current vault but cheap (only mapped
    # pages are actually paged in).
    conn.execute("PRAGMA mmap_size = 268435456")  # 256MB
    # Page cache — negative value means kilobytes (positive = pages).
    # -65536 ≈ 64MB of recent pages kept hot in this connection's cache.
    # Per-connection, so per-thread connections each get their own 64MB
    # working set (acceptable for our 1GB VM).
    conn.execute("PRAGMA cache_size = -65536")
    # Run the planner's auto-tune. Cheap to call repeatedly; SQLite skips
    # the work when nothing has changed since the last optimize.
    conn.execute("PRAGMA optimize")

    # Load sqlite-vec extension (provides the vec0 virtual table type).
    # Must happen before the vec DDL runs.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    init_db(conn, embedding_dim=embedding_dim)
    return conn


def init_db(conn: sqlite3.Connection, *, embedding_dim: int = 1536) -> None:
    """Idempotent DDL execution. Safe on fresh or populated databases.

    The vector table's dimension is bound at creation; if it doesn't match
    the embedding_dim passed here AFTER initial creation, queries against
    the existing table still work but stored vectors are at the original
    dimension. To change dim mid-life, manually drop and recreate the table.
    """
    with conn:
        for stmt in SCHEMA_DDL:
            conn.execute(stmt)
        # Vector DDL is parameterized by embedding_dim — render once at boot.
        for stmt_template in VEC_DDL:
            conn.execute(stmt_template.format(dim=embedding_dim))

"""SQLite connection management — open, configure pragmas, init schema."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .schema import SCHEMA_DDL

if TYPE_CHECKING:
    from pathlib import Path


def open_db(vault_dir: Path) -> sqlite3.Connection:
    """Open (and create if missing) the substrate SQLite file.

    Idempotent: safe to call on an existing populated vault. Creates the
    vault directory and the ``objects/`` subdir if they do not yet exist.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "objects").mkdir(parents=True, exist_ok=True)

    db_path = vault_dir / "substrate.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Pragmas — durability + concurrency
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")

    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent DDL execution. Safe on fresh or populated databases."""
    with conn:
        for stmt in SCHEMA_DDL:
            conn.execute(stmt)

"""Checkpoint-loop coverage (P2d).

`start_checkpoint_loop` runs a daemon that issues wal_checkpoint(PASSIVE)
and sweeps expired OAuth codes/login-state each cycle. The GC half is the
deterministically testable piece; the loop start is smoke-checked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from afair.substrate import open_db
from afair.substrate.checkpoint import gc_oauth_codes, start_checkpoint_loop

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _seed_code(conn: sqlite3.Connection, code: str, *, expires_at: str) -> None:
    conn.execute(
        """
        INSERT INTO oauth_codes (
            code, client_id, redirect_uri, scope, code_challenge,
            code_challenge_method, user_sub, user_email, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (code, "c", "https://x/cb", None, "chal", "S256", "u", None, expires_at, expires_at),
    )


def _seed_state(conn: sqlite3.Connection, state: str, *, expires_at: str) -> None:
    conn.execute(
        """
        INSERT INTO oauth_login_state (
            state, client_id, redirect_uri, scope, code_challenge,
            code_challenge_method, client_state, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (state, "c", "https://x/cb", None, "chal", "S256", None, expires_at, expires_at),
    )


def test_gc_oauth_codes_deletes_only_expired(tmp_path: Path) -> None:
    conn = open_db(tmp_path)
    try:
        now = datetime.now(UTC)
        past = (now - timedelta(hours=1)).isoformat()
        future = (now + timedelta(hours=1)).isoformat()
        with conn:
            _seed_code(conn, "expired-code", expires_at=past)
            _seed_code(conn, "live-code", expires_at=future)
            _seed_state(conn, "expired-state", expires_at=past)
            _seed_state(conn, "live-state", expires_at=future)

        deleted = gc_oauth_codes(conn)
        assert deleted == 2

        codes = {r["code"] for r in conn.execute("SELECT code FROM oauth_codes")}
        assert codes == {"live-code"}
        states = {r["state"] for r in conn.execute("SELECT state FROM oauth_login_state")}
        assert states == {"live-state"}
    finally:
        conn.close()


def test_start_checkpoint_loop_returns_daemon_thread(tmp_path: Path) -> None:
    thread = start_checkpoint_loop(tmp_path, interval_seconds=3600)
    assert thread.is_alive()
    assert thread.daemon is True

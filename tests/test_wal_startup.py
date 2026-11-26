"""Deterministic WAL initialization races and failure cleanup."""

from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from afair.substrate import db


class _Rows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _ScriptedConnection:
    def __init__(self, error, *, becomes_wal=False):
        self.error = error
        self.becomes_wal = becomes_wal
        self.statements = []
        self.reads = 0

    def execute(self, sql):
        self.statements.append(sql)
        if sql == "PRAGMA journal_mode":
            self.reads += 1
            if self.becomes_wal:
                return _Rows(("delete" if self.reads == 1 else "wal",))
            raise self.error
        if sql == "PRAGMA journal_mode = WAL":
            raise self.error
        return _Rows(None)


@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
@pytest.mark.parametrize(
    "message", ["database is locked", "database table is locked: sqlite_master"]
)
def test_immediate_busy_upgrade_retries_and_observes_other_opener_wal(
    monkeypatch, backend, message
):
    error = sqlite3.OperationalError(message)
    if backend == "sqlcipher":

        class CipherOperationalError(Exception):
            pass

        monkeypatch.setitem(
            sys.modules, "sqlcipher3", SimpleNamespace(OperationalError=CipherOperationalError)
        )
        error = CipherOperationalError(message)
    conn = _ScriptedConnection(error, becomes_wal=True)
    pauses = []
    monkeypatch.setattr(db.time, "sleep", pauses.append)
    db._ensure_wal(conn)
    assert conn.reads == 2
    assert conn.statements.count("PRAGMA journal_mode = WAL") == 1
    assert pauses == [db.WAL_RETRY_SECONDS]
    assert conn.statements[-1] == "PRAGMA busy_timeout = 5000"


def test_retries_have_one_deadline_and_preserve_original_busy_error(monkeypatch):
    error = sqlite3.OperationalError("database is locked")
    conn = _ScriptedConnection(error)
    now = [0.0]
    monkeypatch.setattr(db.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(db.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(db, "WAL_STARTUP_TIMEOUT_SECONDS", 0.12)
    with pytest.raises(sqlite3.OperationalError) as caught:
        db._ensure_wal(conn)
    assert caught.value is error
    assert now[0] == 0.12
    assert conn.reads == 4
    assert conn.statements[-1] == "PRAGMA busy_timeout = 5000"


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.DatabaseError("file is not a database"),
        ValueError("database is locked"),
    ],
)
def test_non_contention_errors_do_not_retry(monkeypatch, error):
    conn = _ScriptedConnection(error)
    pauses = []
    monkeypatch.setattr(db.time, "sleep", pauses.append)
    with pytest.raises(type(error)) as caught:
        db._ensure_wal(conn)
    assert caught.value is error
    assert conn.reads == 1
    assert pauses == []
    assert conn.statements[-1] == "PRAGMA busy_timeout = 5000"


def test_already_wal_does_not_request_mode_transition_with_active_writer(tmp_path):
    writer = db.open_db(tmp_path, vault_key=None)
    reader = sqlite3.connect(tmp_path / "substrate.db")
    trace = []
    reader.set_trace_callback(trace.append)
    try:
        writer.execute("BEGIN IMMEDIATE")
        db._ensure_wal(reader)
        assert "PRAGMA journal_mode = WAL" not in trace
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        writer.rollback()
        reader.close()
        writer.close()


class _FirstOpenConnection:
    """Both connections observe DELETE before either can request WAL."""

    def __init__(self, real, barrier):
        self.real = real
        self.barrier = barrier
        self.first = True

    def __getattr__(self, name):
        return getattr(self.real, name)

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *args):
        return self.real.__exit__(*args)

    def execute(self, sql, *args):
        if sql == "PRAGMA journal_mode" and self.first:
            self.first = False
            cursor = self.real.execute(sql, *args)
            row = cursor.fetchone()
            cursor.close()
            assert row[0] == "delete"
            self.barrier.wait(timeout=5)
            return _Rows(row)
        return self.real.execute(sql, *args)


@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
def test_fresh_vault_concurrent_first_connections(tmp_path, monkeypatch, backend):
    key = None
    if backend == "sqlcipher":
        pytest.importorskip("sqlcipher3")
        key = b"wal-startup-test-key-32-bytes-long"
    barrier = threading.Barrier(2)
    original = db._open_connection

    def open_connection(*args, **kwargs):
        return _FirstOpenConnection(original(*args, **kwargs), barrier)

    monkeypatch.setattr(db, "_open_connection", open_connection)

    def initialize():
        conn = db.open_db(tmp_path, vault_key=key)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert (
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='events'").fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)


@pytest.mark.parametrize("stage", ["wal", "extension", "schema"])
def test_failed_open_closes_connection_and_preserves_error(tmp_path, monkeypatch, stage):
    opened = []
    original = db._open_connection
    error = RuntimeError(f"{stage} initialization failed")

    class CloseFails:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def close(self):
            self.real.close()
            raise OSError("cleanup failed too")

    def open_connection(*args, **kwargs):
        real = original(*args, **kwargs)
        opened.append(real)
        return CloseFails(real)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(db, "_open_connection", open_connection)
    if stage == "wal":
        monkeypatch.setattr(db, "_ensure_wal", fail)
    elif stage == "extension":
        monkeypatch.setattr(db.sqlite_vec, "load", fail)
    else:
        monkeypatch.setattr(db, "init_db", fail)
    with pytest.raises(RuntimeError) as caught:
        db.open_db(tmp_path, vault_key=None)
    assert caught.value is error
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("stage", ["key", "verification"])
def test_sqlcipher_key_failure_closes_new_connection(tmp_path, monkeypatch, stage):
    class CipherDatabaseError(Exception):
        pass

    class Connection:
        closed = False
        row_factory = None

        def execute(self, sql):
            if stage == "key" or sql.startswith("SELECT"):
                raise CipherDatabaseError("invalid cipher")
            return _Rows(None)

        def close(self):
            self.closed = True

    conn = Connection()
    monkeypatch.setitem(
        sys.modules,
        "sqlcipher3",
        SimpleNamespace(
            connect=lambda *args, **kwargs: conn, Row=object, DatabaseError=CipherDatabaseError
        ),
    )
    with pytest.raises(CipherDatabaseError if stage == "key" else RuntimeError):
        db._open_connection(tmp_path / "substrate.db", vault_key=b"test-key")
    assert conn.closed


def test_busy_extended_code_is_recognized_without_relying_on_message():
    error = sqlite3.OperationalError("provider-specific busy text")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
    assert db._is_sqlite_busy(error)


def test_timeout_restore_failure_does_not_mask_initialization_error(monkeypatch):
    original = sqlite3.OperationalError("disk I/O error")

    class RestoreFails(_ScriptedConnection):
        def execute(self, sql):
            if sql == "PRAGMA busy_timeout = 5000":
                raise OSError("restore failed")
            return super().execute(sql)

    with pytest.raises(sqlite3.OperationalError) as caught:
        db._ensure_wal(RestoreFails(original))
    assert caught.value is original

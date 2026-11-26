"""Cold-path scheduler + Worker base tests (Phase 3)."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from afair.agents.cold_path import ColdPathScheduler, ColdPathWorker
from afair.settings import Settings
from afair.substrate import open_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def stop_test_schedulers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Own every daemon started by this module, including failed test paths."""
    schedulers: list[ColdPathScheduler] = []
    original_start = ColdPathScheduler.start

    def tracked_start(scheduler: ColdPathScheduler) -> threading.Thread:
        if scheduler not in schedulers:
            schedulers.append(scheduler)
        return original_start(scheduler)

    monkeypatch.setattr(ColdPathScheduler, "start", tracked_start)
    yield
    # Request shutdown for all before checking any result, so one failure cannot
    # leave its siblings polling and logging during interpreter finalization.
    for scheduler in schedulers:
        scheduler.stop(timeout=0)
    for scheduler in schedulers:
        assert scheduler.stop(timeout=5), "test leaked a cold-path scheduler"
        assert scheduler.scheduler_status()["thread_alive"] is False


class _CountingWorker(ColdPathWorker):
    """A worker that records each invocation for test assertions."""

    name = "counting"
    interval_seconds = 1

    def __init__(self) -> None:
        self.invocations = 0

    def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        self.invocations += 1
        return {"invocations_so_far": self.invocations}


class _RaisingWorker(ColdPathWorker):
    """Always raises — verifies the scheduler isolates failures."""

    name = "raiser"
    interval_seconds = 1

    def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        msg = "intentional test failure"
        raise RuntimeError(msg)


@pytest.fixture
def settings_local(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


def test_worker_invoked_at_least_once_within_short_window(
    tmp_path: Path, settings_local: Settings
) -> None:
    """With poll_seconds=1 and a 0-interval worker, the scheduler should
    invoke the worker within a couple of seconds."""
    worker = _CountingWorker()
    worker.interval_seconds = 0  # always due
    open_db(tmp_path).close()  # create the file
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[worker],
        poll_seconds=1,
    )
    sched.start()
    # Give it up to 3 seconds.
    deadline = time.monotonic() + 3.0
    while worker.invocations == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert worker.invocations >= 1


def test_worker_failure_does_not_block_others(tmp_path: Path, settings_local: Settings) -> None:
    """If one worker raises, sibling workers still run on the same cycle."""
    raiser = _RaisingWorker()
    raiser.interval_seconds = 0
    counter = _CountingWorker()
    counter.interval_seconds = 0
    open_db(tmp_path).close()
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[raiser, counter],
        poll_seconds=1,
    )
    sched.start()
    deadline = time.monotonic() + 3.0
    while counter.invocations == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert counter.invocations >= 1  # raiser's failure didn't stop counter


def test_start_is_idempotent(tmp_path: Path, settings_local: Settings) -> None:
    """Repeat start() calls return the same thread; we don't spawn a duplicate."""
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_CountingWorker()],
        poll_seconds=60,  # don't actually run for tests
    )
    t1 = sched.start()
    t2 = sched.start()
    assert t1 is t2
    assert isinstance(t1, threading.Thread)


def test_status_reports_each_worker(tmp_path: Path, settings_local: Settings) -> None:
    """The diagnostic status method returns one entry per worker."""
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_CountingWorker(), _RaisingWorker()],
        poll_seconds=60,
    )
    st = sched.status()
    assert set(st.keys()) == {"counting", "raiser"}
    assert all("interval_seconds" in v for v in st.values())


class _LeakyWriter(ColdPathWorker):
    """Opens a write on the shared connection then raises before commit."""

    name = "leaky"
    interval_seconds = 0

    def run(self, conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO oauth_codes (
                code, client_id, redirect_uri, scope, code_challenge,
                code_challenge_method, user_sub, user_email, expires_at, created_at
            ) VALUES ('leak', 'c', 'u', NULL, 'ch', 'S256', 'u', NULL, '2999-01-01', '2999-01-01')
            """
        )
        msg = "boom after an uncommitted write"
        raise RuntimeError(msg)


class _RowChecker(ColdPathWorker):
    """Records whether the leaked row is visible on the shared connection."""

    name = "checker"
    interval_seconds = 0

    def __init__(self) -> None:
        self.seen: int | None = None

    def run(self, conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        self.seen = conn.execute("SELECT COUNT(*) FROM oauth_codes WHERE code = 'leak'").fetchone()[
            0
        ]
        return {"seen": self.seen}


def test_worker_failure_rolls_back_open_transaction(
    tmp_path: Path, settings_local: Settings
) -> None:
    """A worker that raises after an uncommitted write must not leak the open
    transaction to the next worker. Same connection sees its own uncommitted
    writes, so without the rollback the checker would see the leaked row (1);
    with it, the tx is rolled back and the checker sees 0."""
    leaky = _LeakyWriter()
    checker = _RowChecker()
    open_db(tmp_path).close()
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[leaky, checker],
        poll_seconds=1,
    )
    sched.start()
    deadline = time.monotonic() + 3.0
    while checker.seen is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert checker.seen == 0


class _UncommittedWriter(ColdPathWorker):
    """Writes on the shared connection and returns without committing."""

    name = "uncommitted"
    interval_seconds = 0

    def run(self, conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        conn.execute(
            """
            INSERT OR IGNORE INTO oauth_codes (
                code, client_id, redirect_uri, scope, code_challenge,
                code_challenge_method, user_sub, user_email, expires_at, created_at
            ) VALUES ('kept', 'c', 'u', NULL, 'ch', 'S256', 'u', NULL, '2999-01-01', '2999-01-01')
            """
        )
        return {}


class _ConcurrentWriterProbe(ColdPathWorker):
    """Tries to take the writer lock from a separate connection, as an MCP
    request thread would, without waiting."""

    name = "probe"
    interval_seconds = 0

    def __init__(self, vault_dir: Path) -> None:
        self._vault_dir = vault_dir
        self.acquired: bool | None = None
        self.saw_row: int | None = None

    def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        import sqlite3 as sqlite

        other = open_db(self._vault_dir)
        try:
            other.execute("PRAGMA busy_timeout = 0")
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                self.acquired = True
            except sqlite.OperationalError:
                self.acquired = False
            self.saw_row = other.execute(
                "SELECT COUNT(*) FROM oauth_codes WHERE code = 'kept'"
            ).fetchone()[0]
        finally:
            other.close()
        return {}


def test_successful_worker_open_transaction_is_committed(
    tmp_path: Path, settings_local: Settings
) -> None:
    """A worker that returns with an uncommitted write must not keep the
    writer lock into the next worker or the poll sleep. The scheduler commits
    it, so another connection can write immediately and sees the row."""
    probe = _ConcurrentWriterProbe(tmp_path)
    open_db(tmp_path).close()
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_UncommittedWriter(), probe],
        poll_seconds=1,
    )
    sched.start()
    deadline = time.monotonic() + 3.0
    while probe.acquired is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert probe.acquired is True
    assert probe.saw_row == 1


class _UncommittableWriter(_UncommittedWriter):
    """Holds the writer lock with a pending write whose COMMIT will fail on a
    deferred foreign-key violation."""

    name = "uncommittable"

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS fk_parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS fk_child ("
            "pid INTEGER REFERENCES fk_parent(id) DEFERRABLE INITIALLY DEFERRED)"
        )
        super().run(conn, settings)
        conn.execute("INSERT INTO fk_child (pid) VALUES (1)")
        return {}


def test_failed_commit_releases_lock_and_scheduler_keeps_running(
    tmp_path: Path, settings_local: Settings
) -> None:
    """If committing a worker's leftover transaction fails, the scheduler
    rolls back and keeps going. It must never die holding the writer lock."""
    probe = _ConcurrentWriterProbe(tmp_path)
    open_db(tmp_path).close()
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_UncommittableWriter(), probe],
        poll_seconds=1,
    )
    sched.start()
    deadline = time.monotonic() + 3.0
    while probe.acquired is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert probe.acquired is True
    assert probe.saw_row == 0


class _BudgetDeferredWriter(_UncommittedWriter):
    name = "budget_deferred"

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        from afair.agents.usage import BackgroundBudgetExceeded

        super().run(conn, settings)
        raise BackgroundBudgetExceeded("daily allowance exhausted")


def test_budget_deferral_rolls_back_and_releases_writer(
    tmp_path: Path, settings_local: Settings
) -> None:
    """A denied later provider call cannot pin a worker's earlier local write."""
    probe = _ConcurrentWriterProbe(tmp_path)
    open_db(tmp_path).close()
    sched = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_BudgetDeferredWriter(), probe],
        poll_seconds=1,
    )
    sched.start()
    deadline = time.monotonic() + 3.0
    while probe.acquired is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert probe.acquired is True
    assert probe.saw_row == 0
    assert sched.status()["budget_deferred"]["seconds_since_last_success"] is None


def test_process_progress_starts_unknown_even_with_persisted_pipeline(
    tmp_path: Path,
    settings_local: Settings,
) -> None:
    """Old DB snapshots cannot bless a daemon that has not completed a cycle."""
    from afair.substrate import observability

    conn = open_db(tmp_path)
    observability.write_snapshot(
        conn, producer="expectation_checker", counters={"expectation_violations": 0}
    )
    conn.close()
    scheduler = ColdPathScheduler(
        vault_dir=tmp_path, embedding_dim=1536, settings=settings_local, workers=[_CountingWorker()]
    )
    status = scheduler.status()["counting"]
    assert status["state"] == "pending"
    assert status["seconds_since_last_success"] is None
    assert status["interval_seconds"] == 1
    assert scheduler.scheduler_status()["thread_alive"] is False
    assert scheduler.scheduler_status()["seconds_since_last_poll"] is None


class _ExplicitBudgetDeferred(ColdPathWorker):
    name = "explicit_budget_deferred"
    interval_seconds = 3600

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        from afair.agents.usage import BackgroundBudgetExceeded

        raise BackgroundBudgetExceeded(
            "private provider details must not enter health", reason="daily_budget_exhausted"
        )


def test_health_distinguishes_real_success_budget_refusal_and_thread_progress(
    tmp_path: Path,
    settings_local: Settings,
) -> None:
    settings_local.background_daily_budget_usd = 0
    counting = _CountingWorker()
    scheduler = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[_ExplicitBudgetDeferred(), counting],
        poll_seconds=1,
    )
    scheduler.start()
    deadline = time.monotonic() + 3
    while counting.invocations == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    states = scheduler.status()
    refused = states["explicit_budget_deferred"]
    assert refused["state"] == "budget_deferred"
    assert refused["seconds_since_last_run"] is not None
    assert refused["seconds_since_last_success"] is None
    assert refused["budget_deferral_reason"] == "daily_budget_exhausted"
    assert refused["budget_deferral_current_day"] is True
    assert refused["budget_configured"] is True
    assert states["counting"]["state"] == "succeeded"
    assert states["counting"]["seconds_since_last_success"] is not None
    assert scheduler.scheduler_status()["thread_alive"] is True
    assert scheduler.scheduler_status()["seconds_since_last_poll"] <= 1
    assert "private provider" not in str(states)


def test_budget_reason_is_allowlisted_and_never_derived_from_exception_text() -> None:
    from afair.agents.usage import BackgroundBudgetExceeded

    failure = BackgroundBudgetExceeded("private-model-name-and-secret", reason="private-diagnostic")
    assert failure.reason == "unspecified"


def test_stop_wakes_long_poll_and_supports_restart(
    settings_local: Settings, tmp_path: Path
) -> None:
    worker = _CountingWorker()
    scheduler = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[worker],
        poll_seconds=3600,
    )
    assert scheduler.stop()
    first = scheduler.start()
    assert scheduler.stop(timeout=1)
    assert not first.is_alive()
    assert worker.invocations == 0
    second = scheduler.start()
    assert second is not first
    assert scheduler.stop(timeout=1)
    assert not second.is_alive()


def test_stop_drains_current_worker_closes_connection_and_skips_siblings(
    settings_local: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed_by: list[int] = []

    class Connection:
        in_transaction = False

        def close(self) -> None:
            closed_by.append(threading.get_ident())

    class BlockingWorker(_CountingWorker):
        def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
            entered.set()
            assert release.wait(5), "test did not release worker"
            return super().run(_conn, _settings)

    connection = Connection()
    monkeypatch.setattr("afair.agents.cold_path.open_db", lambda *a, **kw: connection)
    first = BlockingWorker()
    sibling = _CountingWorker()
    sibling.name = "sibling"
    scheduler = ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings_local,
        workers=[first, sibling],
        poll_seconds=0,
    )
    thread = scheduler.start()
    try:
        assert entered.wait(3)
        assert scheduler.stop(timeout=0) is False
        assert thread.is_alive()
        assert closed_by == []  # never close another thread's active connection
    finally:
        release.set()
        assert scheduler.stop(timeout=3)
    assert not thread.is_alive()
    assert first.invocations == 1
    assert sibling.invocations == 0
    assert closed_by == [thread.ident]

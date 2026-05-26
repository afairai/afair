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
    from pathlib import Path


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
    open_db(tmp_path)  # create the file
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
    open_db(tmp_path)
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

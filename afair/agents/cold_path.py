"""Cold-path scheduler + Worker base class (Phase 3 — Sleep Swarm).

The Phase-3 sleep swarm runs background workers that improve memory
quality without blocking hot-path recall/remember. Three workers in
the v0 lineup:

  - Pruner            — interpretation-layer hygiene (no LLM)
  - Conflict-Resolver — flags semantically-similar events that contradict
  - Consolidator      — daily theme summaries (CLS replay; see consolidator.py)

Theoretical framing (see VISION.md §6.1a): cold-path work is the
software analog of the brain's DMN-mode (Default Mode Network) —
mind-wandering, consolidation, and ontology refinement that happens
when the system isn't actively responding to a user query. The split
between hot-path (CEN-mode, task work) and cold-path (DMN-mode,
reflection) is the architectural expression of the Triple Network
Model's mode-switching. The deeper justification is Complementary
Learning Systems: the episodic substrate cannot do its own
generalization without catastrophic interference, so a separate
semantic-abstraction pass (this scheduler's workers) runs at slower
cadence over the same data.

Architectural contract:

  - **Cold path only.** Workers MUST NOT block any handler. They run in
    a single daemon thread with their own DB connection, sequentially
    (not in parallel) to avoid LLM rate-limit races and SQLite write
    contention.
  - **Substrate is sacred (I2).** Workers may write NEW events (e.g.,
    Consolidator writes kind=consolidation rows) but MUST NOT update or
    delete existing events. Mutability is restricted to the
    Interpretation layer.
  - **Idempotent.** Each run must be safe to repeat. Workers track their
    own progress via either DB-row existence or schedule timestamps.
    The scheduler tracks last_run wall time for due-checking; workers
    track work-completed state.
  - **Bounded.** Each worker caps the work it does per run (max N pairs,
    max N clusters) so a single run can't pin the thread for hours or
    blow the LLM budget.

Idle detection (the "real sleep" Phase-2 promised) is intentionally
deferred. v0 uses fixed intervals — same pattern as the WAL checkpoint
loop. Workers themselves are cheap enough that running every N hours
regardless of activity is fine.
"""

from __future__ import annotations

import contextlib
import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate.db import open_db
from .foreground import background_should_yield
from .usage import BackgroundBudgetExceeded, background_usage

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from ..settings import Settings

log = structlog.get_logger(__name__)


class ColdPathWorker(ABC):
    """One unit of background work. Subclasses set name + interval; the
    scheduler calls ``run`` on the configured cadence.

    ``run`` returns a stats dict that gets logged structured. By convention
    the dict has integer counters describing what was done; the scheduler
    doesn't interpret it, just emits it.
    """

    name: str = "abstract"
    """Unique identifier — used in log lines and for the due-check tracker.
    Two workers with the same name would step on each other's tracking."""

    interval_seconds: int = 3600
    """How often this worker should run. The scheduler enforces the
    minimum gap between consecutive runs of the same worker."""

    @abstractmethod
    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        """Do one unit of work. Returns a stats dict for logging.

        The connection passed in is the scheduler's per-thread connection.
        Workers should NOT close it. Long-running work should check no
        external state — workers are expected to complete in seconds, not
        minutes.
        """


class ColdPathScheduler:
    """Daemon thread that invokes registered workers on their cadence.

    Sequential execution: only one worker runs at a time, even when
    multiple are simultaneously due. Avoids LLM rate-limit races and
    keeps SQLite writes from contending. The check loop wakes every
    ``poll_seconds`` (default 60) to see who's due.
    """

    def __init__(
        self,
        *,
        vault_dir: Path,
        embedding_dim: int,
        settings: Settings,
        workers: list[ColdPathWorker],
        poll_seconds: int = 60,
    ) -> None:
        self._vault_dir = vault_dir
        self._embedding_dim = embedding_dim
        self._settings = settings
        self._workers = list(workers)
        self._poll_seconds = poll_seconds
        # Last successful (or failed-but-attempted) run, monotonic clock.
        # All start at -inf so each worker fires once at the first poll.
        self._last_run: dict[str, float] = {w.name: float("-inf") for w in workers}
        # A second tracker for SUCCESSFUL completion; used for diagnostics.
        self._last_success: dict[str, float] = dict.fromkeys(self._last_run, float("-inf"))
        self._states = dict.fromkeys(self._last_run, "pending")
        self._state_at = dict.fromkeys(self._last_run, time.monotonic())
        self._deferrals: dict[str, tuple[str, str]] = {}
        self._created_at = time.monotonic()
        self._last_poll: float | None = None
        self._last_heartbeat: float | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        """Spawn the daemon. Idempotent — repeat calls return the same
        thread. The scheduler is intended to be process-global; only
        ``build_server`` should call this."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="cold-path-scheduler", daemon=True
            )
            self._thread.start()
            log.info(
                "cold_path.scheduler_started",
                workers=[w.name for w in self._workers],
                poll_seconds=self._poll_seconds,
            )
            return self._thread

    def stop(self, timeout: float | None = 5.0) -> bool:
        """Wake the poll loop and join it; return whether shutdown completed.

        An in-flight worker finishes normally (including transaction cleanup),
        but no next worker starts. A timeout does not kill a worker or close its
        connection from another thread; callers must check the result and join
        again if necessary. A fully stopped scheduler may be started again.
        """
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def _state(self, name: str, state: str) -> None:
        with self._lock:
            self._states[name] = state
            self._state_at[name] = time.monotonic()
            self._last_heartbeat = time.monotonic()

    def scheduler_status(self) -> dict[str, Any]:
        """Current process only: persisted pipeline health cannot prove a new daemon ran."""
        now = time.monotonic()
        with self._lock:
            return {
                "thread_alive": self._thread is not None and self._thread.is_alive(),
                "poll_seconds": self._poll_seconds,
                "seconds_since_last_poll": None
                if self._last_poll is None
                else int(now - self._last_poll),
                "seconds_since_last_heartbeat": None
                if self._last_heartbeat is None
                else int(now - self._last_heartbeat),
                "uptime_seconds": int(now - self._created_at),
            }

    def _loop(self) -> None:
        # Connection is opened lazily inside the loop so a startup-time
        # filesystem hiccup doesn't crash before the daemon's first poll.
        conn: sqlite3.Connection | None = None
        try:
            while not self._stop.wait(self._poll_seconds):
                now = time.monotonic()
                with self._lock:
                    self._last_poll = now
                    self._last_heartbeat = now
                due = [
                    w for w in self._workers if now - self._last_run[w.name] >= w.interval_seconds
                ]
                if not due or background_should_yield():
                    continue
                if conn is None:
                    try:
                        conn = open_db(self._vault_dir, embedding_dim=self._embedding_dim)
                    except Exception as e:
                        log.warning("cold_path.db_open_failed", error=str(e))
                        continue
                for worker in due:
                    if self._stop.is_set() or background_should_yield():
                        break
                    with self._lock:
                        self._last_run[worker.name] = time.monotonic()
                        self._deferrals.pop(worker.name, None)
                    self._state(worker.name, "running")
                    try:
                        with background_usage(self._settings, worker.name):
                            stats = worker.run(conn, self._settings)
                    except BackgroundBudgetExceeded as deferred:
                        with self._lock:
                            self._deferrals[worker.name] = (
                                deferred.reason,
                                datetime.now(UTC).date().isoformat(),
                            )
                        self._state(worker.name, "budget_deferred")
                        # No success timestamp: durable candidates stay pending.
                        log.info("cold_path.budget_deferred", worker=worker.name)
                        # A worker may have staged local writes before its next
                        # provider reservation was refused. Release that writer
                        # transaction exactly as on any other interrupted cycle.
                        with contextlib.suppress(Exception):
                            conn.rollback()
                        continue
                    except Exception as e:
                        self._state(worker.name, "failed")
                        log.warning("cold_path.worker_failed", worker=worker.name, error=str(e))
                        # Roll back any open transaction so a worker that raised
                        # between a bare execute and its commit doesn't leak an
                        # open tx that the NEXT worker's commit would absorb
                        # (attributing this worker's partial writes to it). The
                        # shared per-daemon connection is reused across workers.
                        with contextlib.suppress(Exception):
                            conn.rollback()
                        continue
                    if conn.in_transaction:
                        # A bare write without a commit keeps SQLite's writer lock
                        # through the poll sleep and every later worker, which
                        # starves remember/observe past busy_timeout. The worker
                        # succeeded, so its writes are intended: commit them.
                        log.warning("cold_path.worker_left_open_transaction", worker=worker.name)
                        try:
                            conn.commit()
                        except Exception as e:
                            # An unhandled raise here would end this thread with
                            # the lock still held, blocking every write until restart.
                            log.error(
                                "cold_path.worker_commit_failed", worker=worker.name, error=str(e)
                            )
                            with contextlib.suppress(Exception):
                                conn.rollback()
                            self._state(worker.name, "failed")
                            continue
                    with self._lock:
                        self._last_success[worker.name] = time.monotonic()
                    self._state(worker.name, "succeeded")
                    log.info("cold_path.worker_done", worker=worker.name, **stats)
        finally:
            if conn is not None:
                conn.close()

    def status(self) -> dict[str, dict[str, Any]]:
        """Diagnostic snapshot of when each worker last ran. For tests
        and the eventual /health-cold endpoint."""
        now = time.monotonic()
        with self._lock:
            return {
                w.name: {
                    "interval_seconds": w.interval_seconds,
                    "state": self._states[w.name],
                    "seconds_since_state_change": int(now - self._state_at[w.name]),
                    "budget_deferral_reason": self._deferrals.get(w.name, (None, None))[0],
                    "budget_deferral_current_day": self._deferrals.get(w.name, (None, None))[1]
                    == datetime.now(UTC).date().isoformat(),
                    "budget_configured": self._settings.background_daily_budget_usd is not None,
                    "seconds_since_last_run": (
                        None
                        if self._last_run[w.name] == float("-inf")
                        else int(now - self._last_run[w.name])
                    ),
                    "seconds_since_last_success": (
                        None
                        if self._last_success[w.name] == float("-inf")
                        else int(now - self._last_success[w.name])
                    ),
                }
                for w in self._workers
            }

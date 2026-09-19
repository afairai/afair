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
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..substrate.db import open_db

if TYPE_CHECKING:
    import sqlite3

    from ..settings import Settings

log = structlog.get_logger(__name__)


# ── Takt-Steuerung (lokaler Self-Host-Patch, Michael 2026-07-26) ───────────────
# Hintergrund: die Default-Takte sind fuer eine gehostete Instanz mit
# Anthropic-Budget ausgelegt. Auf dem Mac Mini erzeugte vor allem der
# entity_canonicalizer (alle 120s, eskaliert intern auf Sonnet) einen
# Dauerstrom bezahlter Calls — gemessen ~45-75 EUR/Monat, ohne dass die
# Arbeit mit Michaels tatsaechlicher Nutzung skaliert haette.
#
# Statt 17 Agent-Dateien zu patchen (die bei jedem Upstream-Update
# kollidieren wuerden) haengt der Takt jetzt an einer Env-Zeile:
#
#   AFAIR_WORKER_INTERVALS=entity_canonicalizer=1800,living_syntheses=172800
#
# Sekunden je Worker; ``=0`` entfernt den Worker komplett aus dem Lauf.
# Unbekannte Namen und Schrottwerte werden geloggt und ignoriert — eine
# kaputte Env-Zeile darf den Scheduler nie am Starten hindern.
#
# Damit ist der Takt zur Laufzeit steuerbar: die Jarvis-Zentrale schreibt
# die Zeile und startet den Container neu, ohne dass Code angefasst wird.
_INTERVAL_ENV = "AFAIR_WORKER_INTERVALS"


def _parse_interval_overrides(raw: str) -> dict[str, int]:
    """Parse ``name=seconds,name=seconds`` into a dict. Never raises."""
    out: dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, value = chunk.partition("=")
        if not sep:
            log.warning("cold_path.interval_override_malformed", chunk=chunk)
            continue
        try:
            seconds = int(value.strip())
        except ValueError:
            log.warning("cold_path.interval_override_not_a_number", chunk=chunk)
            continue
        if seconds < 0:
            log.warning("cold_path.interval_override_negative", chunk=chunk)
            continue
        out[name.strip()] = seconds
    return out


def _apply_interval_overrides(workers: list[ColdPathWorker], overrides: dict[str, int]) -> set[str]:
    """Apply cadence overrides to worker INSTANCES (never the class), so an
    override can't leak into another scheduler or a test.

    Returns the set of workers to PARK. Parking is tracked separately
    instead of by writing 0 into ``interval_seconds``, because 0 already
    means "always due" in the base class — overloading it would silently
    invert the meaning for any worker that legitimately uses it.

    A parked worker keeps its place in the list and its run tracker, so
    the control file can wake it again without a restart.
    """
    parked: set[str] = set()
    if not overrides:
        return parked

    known = {w.name for w in workers}
    for unknown in sorted(set(overrides) - known):
        log.warning("cold_path.interval_override_unknown_worker", worker=unknown)

    for worker in workers:
        seconds = overrides.get(worker.name)
        if seconds is None:
            continue
        if seconds == 0:
            parked.add(worker.name)
            log.info("cold_path.worker_parked", worker=worker.name)
            continue
        if seconds != worker.interval_seconds:
            log.info(
                "cold_path.interval_overridden",
                worker=worker.name,
                was=worker.interval_seconds,
                now=seconds,
            )
            worker.interval_seconds = seconds
    return parked


# ── Laufzeit-Steuerung ueber eine Kontrolldatei ────────────────────────────────
# Warum eine Datei und nicht nur die Env: Env kann man einem LAUFENDEN Prozess
# nicht von aussen aendern — jede Umstellung braeuchte einen Container-Neustart.
# Und ein Neustart ist teuer: _last_run steht danach fuer jeden Worker auf
# -inf, also feuern beim ersten Poll ALLE gleichzeitig. Gemessen am 26.07.2026
# waren das 0,28 USD in 3 Minuten. Bei einem Schalter, den Michael mehrmals am
# Tag umlegen koennen soll, kostet das Umschalten mehr als der Betrieb.
#
# Deshalb: der Scheduler prueft bei JEDEM Poll die mtime einer JSON-Datei und
# liest sie bei Aenderung neu ein. Die Laufzeit-Tracker bleiben dabei erhalten,
# es gibt also keinen Kaltstart-Schub. Schreibt die Jarvis-Zentrale die Datei,
# wirkt die Umstellung innerhalb eines Poll-Intervalls (60s) — ohne Neustart.
#
#   {"paused": false, "intervals": {"entity_canonicalizer": 1800}}
#
# Fehlende Datei, kaputtes JSON oder Schrottwerte = der zuletzt gueltige Stand
# bleibt bestehen. Eine verunglueckte Datei darf den Scheduler nie anhalten.
_CONTROL_ENV = "AFAIR_WORKER_CONTROL_FILE"

# Muss mit entity_canonicalizer.ESCALATION_ENV uebereinstimmen. Bewusst als
# Literal dupliziert statt importiert: der Canonicalizer importiert seinerseits
# ColdPathWorker von hier, ein Modul-Import waere also zirkulaer. Der Scheduler
# kennt seine Worker ohnehin nur ueber die Basisklasse — diese Schichtung soll
# eine Kostenoption nicht aufweichen. tests/test_cold_path.py haelt beide
# Konstanten gegeneinander, damit sie nicht auseinanderlaufen.
_ESCALATION_ENV = "AFAIR_SONNET_ESCALATION"


@dataclass(frozen=True)
class ColdPathControl:
    """One parsed snapshot of the control file."""

    paused: bool = False
    intervals: dict[str, int] = field(default_factory=dict)
    # None = not mentioned in the file, leave whatever is configured alone.
    sonnet_escalation: bool | None = None


def _parse_control(raw: str) -> ColdPathControl | None:
    """Parse the control-file body. Returns None if it is unusable."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("cold_path.control_file_bad_json", error=str(e))
        return None
    if not isinstance(data, dict):
        log.warning("cold_path.control_file_not_an_object", got=type(data).__name__)
        return None

    intervals: dict[str, int] = {}
    for name, value in (data.get("intervals") or {}).items():
        # bool is an int subclass — exclude it explicitly so `true` doesn't
        # silently become a 1-second cadence.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            log.warning("cold_path.control_file_bad_interval", worker=name, value=value)
            continue
        intervals[str(name)] = value

    esc = data.get("sonnet_escalation")
    return ColdPathControl(
        paused=bool(data.get("paused", False)),
        intervals=intervals,
        sonnet_escalation=None if esc is None else bool(esc),
    )


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
        # Cadence comes from two layers: the env var sets the boot default,
        # the control file (re-read on every poll) can retune it at runtime.
        self._parked = _apply_interval_overrides(
            self._workers, _parse_interval_overrides(os.environ.get(_INTERVAL_ENV, ""))
        )
        control_path = os.environ.get(_CONTROL_ENV, "").strip()
        self._control_path: Path | None = Path(control_path) if control_path else None
        self._control_mtime: float | None = None
        self._paused = False
        # Last successful (or failed-but-attempted) run, monotonic clock.
        # All start at -inf so each worker fires once at the first poll.
        self._last_run: dict[str, float] = {w.name: float("-inf") for w in self._workers}
        # A second tracker for SUCCESSFUL completion; used for diagnostics.
        self._last_success: dict[str, float] = dict.fromkeys(self._last_run, float("-inf"))
        # Outcome of each worker's most recent cycle. "Completed" is not the
        # same as "clean": a cycle can finish while individual LLM calls
        # failed (llm_errors in the stats dict). /health surfaces this so a
        # green scheduler cannot hide red calls.
        self._last_stats: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Set by stop(): wakes the poll sleep immediately and ends the loop
        # so the daemon can be joined at lifespan shutdown instead of being
        # abandoned mid-sleep (it would otherwise keep polling a vault that
        # a test teardown already deleted).
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        """Spawn the daemon. Idempotent — repeat calls return the same
        thread. The scheduler is intended to be process-global; only the
        app lifespan (via ``_start_background``) should call this."""
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

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the daemon to exit and join it (bounded).

        Sleeping loops wake immediately; a worker mid-cycle finishes its
        current step and the batch aborts at the next between-worker check.
        The thread stays a daemon, so a join that outlasts ``timeout``
        (e.g. an LLM call in flight) never blocks process exit — the
        daemon then dies with the process exactly as before.
        """
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        log.info("cold_path.scheduler_stopped", joined=thread is None or not thread.is_alive())

    def refresh_control(self) -> bool:
        """Re-read the control file if it changed since the last check.

        Returns True when a new snapshot was applied. Cheap enough to call
        on every poll: one stat() unless the file actually changed. Any
        failure leaves the last good state in place — a broken file must
        never stop the scheduler.
        """
        if self._control_path is None:
            return False
        try:
            mtime = self._control_path.stat().st_mtime
        except OSError:
            return False  # not there (yet) — env defaults stand
        if mtime == self._control_mtime:
            return False
        try:
            raw = self._control_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("cold_path.control_file_unreadable", error=str(e))
            return False

        control = _parse_control(raw)
        # Claim the mtime even on a bad parse, so a broken file is reported
        # once instead of on every single poll.
        self._control_mtime = mtime
        if control is None:
            return False

        with self._lock:
            self._parked = _apply_interval_overrides(self._workers, control.intervals)
            if control.paused != self._paused:
                log.info("cold_path.paused_changed", paused=control.paused)
            self._paused = control.paused
            if control.sonnet_escalation is not None:
                # The canonicalizer reads this at call time, so flipping it
                # here takes effect on its next run — no restart needed.
                # Mirrored into the environment rather than passed down the
                # call chain because the escalation sits several frames deep
                # inside a worker the scheduler doesn't otherwise configure.
                os.environ[_ESCALATION_ENV] = "on" if control.sonnet_escalation else "off"
        log.info(
            "cold_path.control_file_applied",
            paused=control.paused,
            intervals=control.intervals,
        )
        return True

    def _loop(self) -> None:
        # Connection is opened lazily inside the loop so a startup-time
        # filesystem hiccup doesn't crash before the daemon's first poll.
        conn: sqlite3.Connection | None = None
        while not self._stop.wait(self._poll_seconds):
            self.refresh_control()
            if self._paused:
                continue
            now = time.monotonic()
            # Parked workers keep their tracker (so the control file can wake
            # them without a restart) but are never scheduled.
            due = [
                w
                for w in self._workers
                if w.name not in self._parked and now - self._last_run[w.name] >= w.interval_seconds
            ]
            if not due:
                continue
            if conn is None:
                try:
                    conn = open_db(self._vault_dir, embedding_dim=self._embedding_dim)
                except Exception as e:
                    log.warning("cold_path.db_open_failed", error=str(e))
                    continue
            self._run_due_batch(conn, due)

    def _run_due_batch(self, conn: sqlite3.Connection, due: list[ColdPathWorker]) -> None:
        """Run one batch of due workers, sequentially.

        Split out of ``_loop`` so the mid-batch control semantics can be
        tested without spawning a daemon thread.
        """
        for worker in due:
            # Re-check between workers, not just between cycles. A batch of
            # due workers can occupy the thread for minutes (a single
            # living_syntheses pass writes several syntheses), and a "stop"
            # that only lands after the batch finished is not a stop button.
            # One stat() per worker is a rounding error next to an LLM call.
            if self._stop.is_set():
                log.info("cold_path.batch_interrupted_by_stop", remaining=len(due))
                return
            self.refresh_control()
            if self._paused:
                log.info("cold_path.batch_interrupted_by_pause", remaining=len(due))
                return
            if worker.name in self._parked:
                continue
            self._last_run[worker.name] = time.monotonic()
            try:
                stats = worker.run(conn, self._settings)
            except Exception as e:
                log.warning("cold_path.worker_failed", worker=worker.name, error=str(e))
                with self._lock:
                    self._last_stats[worker.name] = {"cycle_failed": True, "llm_errors": None}
                # Roll back any open transaction so a worker that raised
                # between a bare execute and its commit doesn't leak an
                # open tx that the NEXT worker's commit would absorb
                # (attributing this worker's partial writes to it). The
                # shared per-daemon connection is reused across workers.
                with contextlib.suppress(Exception):
                    conn.rollback()
                continue
            self._last_success[worker.name] = time.monotonic()
            with self._lock:
                self._last_stats[worker.name] = {
                    "cycle_failed": False,
                    "llm_errors": int(stats.get("llm_errors") or 0),
                }
            log.info("cold_path.worker_done", worker=worker.name, **stats)

    @property
    def paused(self) -> bool:
        """Whether the control file currently parks the whole swarm."""
        return self._paused

    def status(self) -> dict[str, dict[str, Any]]:
        """Diagnostic snapshot of when each worker last ran. For tests
        and the eventual /health-cold endpoint."""
        now = time.monotonic()
        with self._lock:
            return {
                w.name: {
                    "interval_seconds": w.interval_seconds,
                    "parked": w.name in self._parked,
                    "last_cycle_failed": self._last_stats.get(w.name, {}).get("cycle_failed"),
                    "last_llm_errors": self._last_stats.get(w.name, {}).get("llm_errors"),
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

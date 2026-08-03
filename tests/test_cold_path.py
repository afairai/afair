"""Cold-path scheduler + Worker base tests (Phase 3)."""

from __future__ import annotations

import os
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
    open_db(tmp_path)
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


# ── Env-driven cadence (AFAIR_WORKER_INTERVALS) ──────────────────────────────
# Self-host operators need to retune worker cadence without patching every
# agent file (which would collide on each upstream merge). The override is
# read once at scheduler construction; a malformed entry must never keep the
# scheduler from starting.


class _AlphaWorker(ColdPathWorker):
    name = "alpha"
    interval_seconds = 120

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        return {}


class _BetaWorker(ColdPathWorker):
    name = "beta"
    interval_seconds = 300

    def run(self, conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
        return {}


def _sched(tmp_path: Path, settings: Settings, workers: list[ColdPathWorker]) -> ColdPathScheduler:
    open_db(tmp_path)
    return ColdPathScheduler(
        vault_dir=tmp_path,
        embedding_dim=1536,
        settings=settings,
        workers=workers,
        poll_seconds=60,
    )


def test_interval_override_retunes_named_worker(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named worker picks up the env cadence; unnamed ones keep their default."""
    monkeypatch.setenv("AFAIR_WORKER_INTERVALS", "alpha=1800")
    sched = _sched(tmp_path, settings_local, [_AlphaWorker(), _BetaWorker()])
    status = sched.status()
    assert status["alpha"]["interval_seconds"] == 1800
    assert status["beta"]["interval_seconds"] == 300


def test_interval_override_zero_parks_worker(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``name=0`` parks the worker: it stays visible (and re-wakeable) but
    is never scheduled. Its interval_seconds is left ALONE — 0 already means
    "always due" in the base class."""
    monkeypatch.setenv("AFAIR_WORKER_INTERVALS", "alpha=0")
    sched = _sched(tmp_path, settings_local, [_AlphaWorker(), _BetaWorker()])
    status = sched.status()
    assert status["alpha"]["parked"] is True
    assert status["alpha"]["interval_seconds"] == 120
    assert status["beta"]["parked"] is False


def test_interval_override_does_not_mutate_the_class(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override sets the INSTANCE attribute — a second scheduler built
    without the env var must see the pristine class default."""
    monkeypatch.setenv("AFAIR_WORKER_INTERVALS", "alpha=1800")
    _sched(tmp_path, settings_local, [_AlphaWorker()])
    assert _AlphaWorker.interval_seconds == 120

    monkeypatch.delenv("AFAIR_WORKER_INTERVALS")
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    assert sched.status()["alpha"]["interval_seconds"] == 120


@pytest.mark.parametrize(
    "raw",
    [
        "alpha",  # no '='
        "alpha=abc",  # not a number
        "alpha=-5",  # negative
        "ghost=60",  # unknown worker
        ",,,",  # empty chunks
    ],
)
def test_malformed_override_never_blocks_startup(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Garbage in the env var is logged and ignored — the scheduler still
    starts and every worker keeps its default cadence."""
    monkeypatch.setenv("AFAIR_WORKER_INTERVALS", raw)
    sched = _sched(tmp_path, settings_local, [_AlphaWorker(), _BetaWorker()])
    status = sched.status()
    assert status["alpha"]["interval_seconds"] == 120
    assert status["beta"]["interval_seconds"] == 300


# ── Runtime control file (switch without restart) ────────────────────────────
# The point of the control file is that Michael's Jarvis dashboard can retune
# or park the swarm WITHOUT a container restart, because a restart resets every
# last_run tracker and makes all workers fire at once (measured: 0.28 USD in
# 3 minutes on 2026-07-26). These tests pin exactly that property.


def _control(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "control.json"
    path.write_text(body, encoding="utf-8")
    return path


def _touch_later(path: Path) -> None:
    """Bump mtime into the future so the change is visible even on a
    coarse-grained filesystem clock."""
    future = time.time() + 1
    os.utime(path, (future, future))


def test_control_file_retunes_without_restart(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed control file is picked up by the running scheduler."""
    path = _control(tmp_path, '{"intervals": {"alpha": 1800}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])

    assert sched.refresh_control() is True
    assert sched.status()["alpha"]["interval_seconds"] == 1800

    path.write_text('{"intervals": {"alpha": 60}}', encoding="utf-8")
    _touch_later(path)
    assert sched.refresh_control() is True
    assert sched.status()["alpha"]["interval_seconds"] == 60


def test_control_file_preserves_run_trackers(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retuning must NOT reset last_run — that reset is exactly the
    cold-start burst the control file exists to avoid."""
    path = _control(tmp_path, '{"intervals": {"alpha": 1800}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    sched.refresh_control()

    sched._last_run["alpha"] = time.monotonic()  # pretend it just ran
    assert sched.status()["alpha"]["seconds_since_last_run"] is not None

    path.write_text('{"intervals": {"alpha": 60}}', encoding="utf-8")
    _touch_later(path)
    sched.refresh_control()

    assert sched.status()["alpha"]["seconds_since_last_run"] is not None


def test_control_file_can_park_and_wake_a_worker(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 parks a worker and a later non-zero value wakes it again — the
    round trip a UI switch needs."""
    path = _control(tmp_path, '{"intervals": {"alpha": 0}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    sched.refresh_control()
    assert sched.status()["alpha"]["parked"] is True

    path.write_text('{"intervals": {"alpha": 900}}', encoding="utf-8")
    _touch_later(path)
    sched.refresh_control()
    assert sched.status()["alpha"]["parked"] is False
    assert sched.status()["alpha"]["interval_seconds"] == 900


def test_control_file_pause_flag(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paused`` parks the whole swarm without touching any cadence."""
    path = _control(tmp_path, '{"paused": true}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    sched.refresh_control()
    assert sched.paused is True
    assert sched.status()["alpha"]["interval_seconds"] == 120  # untouched

    path.write_text('{"paused": false}', encoding="utf-8")
    _touch_later(path)
    sched.refresh_control()
    assert sched.paused is False


def test_unchanged_control_file_is_not_reapplied(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling every 60s must not re-parse an unchanged file."""
    path = _control(tmp_path, '{"intervals": {"alpha": 1800}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    assert sched.refresh_control() is True
    assert sched.refresh_control() is False


@pytest.mark.parametrize(
    "body",
    [
        "{not json at all",
        "[1, 2, 3]",
        '{"intervals": {"alpha": "soon"}}',
        '{"intervals": {"alpha": -5}}',
        '{"intervals": {"alpha": true}}',
    ],
)
def test_broken_control_file_keeps_last_good_state(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """A garbled file never changes cadence and never raises — the running
    swarm keeps whatever was last valid."""
    path = _control(tmp_path, '{"intervals": {"alpha": 1800}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    sched.refresh_control()

    path.write_text(body, encoding="utf-8")
    _touch_later(path)
    sched.refresh_control()
    assert sched.status()["alpha"]["interval_seconds"] == 1800


def test_missing_control_file_is_not_an_error(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing at a file that doesn't exist yet leaves env defaults in place."""
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(tmp_path / "nope.json"))
    monkeypatch.setenv("AFAIR_WORKER_INTERVALS", "alpha=900")
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])
    assert sched.refresh_control() is False
    assert sched.status()["alpha"]["interval_seconds"] == 900


class _SlowWorker(ColdPathWorker):
    """Simulates a worker that occupies the thread for a while (living_syntheses
    writing several syntheses), so we can flip the control file mid-batch."""

    name = "slow"
    interval_seconds = 0  # always due

    def __init__(self, control_path: Path, flip_to: str) -> None:
        self.invocations = 0
        self._control_path = control_path
        self._flip_to = flip_to

    def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        self.invocations += 1
        # While "working", the dashboard writes a new control file.
        self._control_path.write_text(self._flip_to, encoding="utf-8")
        _touch_later(self._control_path)
        return {}


class _AfterWorker(ColdPathWorker):
    """Scheduled after _SlowWorker in the same due batch."""

    name = "after"
    interval_seconds = 0

    def __init__(self) -> None:
        self.invocations = 0

    def run(self, _conn: sqlite3.Connection, _settings: Settings) -> dict[str, Any]:
        self.invocations += 1
        return {}


def test_pause_lands_mid_batch(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause written WHILE a batch is running stops the rest of that batch.
    Without the mid-batch re-check, 'after' would still run.

    Driven synchronously via _run_due_batch: spawning a real daemon thread
    here would outlive the test and poll a deleted tmp vault for the rest of
    the session.
    """
    path = _control(tmp_path, '{"paused": false}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    slow = _SlowWorker(path, '{"paused": true}')
    after = _AfterWorker()
    sched = _sched(tmp_path, settings_local, [slow, after])
    conn = open_db(tmp_path)

    sched._run_due_batch(conn, [slow, after])

    assert slow.invocations == 1
    assert after.invocations == 0, "pause did not interrupt the running batch"


def test_park_lands_mid_batch(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parking a not-yet-reached worker mid-batch skips it in that same batch."""
    path = _control(tmp_path, '{"paused": false}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    slow = _SlowWorker(path, '{"intervals": {"after": 0}}')
    after = _AfterWorker()
    sched = _sched(tmp_path, settings_local, [slow, after])
    conn = open_db(tmp_path)

    sched._run_due_batch(conn, [slow, after])

    assert slow.invocations == 1
    assert after.invocations == 0, "parking did not take effect within the batch"


def test_batch_runs_everything_when_nothing_changes(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control-safety must not cost throughput: an untouched control file
    leaves the whole batch running."""
    path = _control(tmp_path, '{"paused": false}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    first, second = _AfterWorker(), _AfterWorker()
    second.name = "second"
    sched = _sched(tmp_path, settings_local, [first, second])
    conn = open_db(tmp_path)

    sched._run_due_batch(conn, [first, second])

    assert first.invocations == 1
    assert second.invocations == 1


# ── Sonnet-Eskalation als Laufzeit-Schalter ──────────────────────────────────
# Die Eskalation (ein zweiter, teurerer Anlauf bei unsicherer Namenszuordnung)
# war auf Michaels Vault 40% der Tageskosten bei 11% der Aufrufe. Sie haengt
# jetzt an der Steuerdatei, damit sie fuer einen gezielten Tiefenlauf wieder
# eingeschaltet werden kann, ohne den Container anzufassen.


def test_escalation_constant_matches_canonicalizer() -> None:
    """Der Env-Name ist in cold_path bewusst dupliziert (Zirkelimport). Dieser
    Test faengt ab, dass die beiden Konstanten auseinanderlaufen."""
    from afair.agents.cold_path import _ESCALATION_ENV
    from afair.agents.entity_canonicalizer import ESCALATION_ENV

    assert _ESCALATION_ENV == ESCALATION_ENV


def test_control_file_switches_escalation(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An/Aus wirkt auf die Entscheidung des Canonicalizers, ohne Neustart."""
    from afair.agents.entity_canonicalizer import _sonnet_for

    monkeypatch.delenv("AFAIR_SONNET_ESCALATION", raising=False)
    path = _control(tmp_path, '{"sonnet_escalation": false}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])

    sched.refresh_control()
    assert _sonnet_for("anthropic/claude-haiku-4-5") is None, "Eskalation nicht aus"

    path.write_text('{"sonnet_escalation": true}', encoding="utf-8")
    _touch_later(path)
    sched.refresh_control()
    assert _sonnet_for("anthropic/claude-haiku-4-5") == "anthropic/claude-sonnet-4-6"


def test_escalation_untouched_when_not_in_control_file(
    tmp_path: Path, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eine Steuerdatei ohne das Feld darf eine bestehende Einstellung nicht
    ueberschreiben — sonst wuerde jedes Takt-Update sie stillschweigend
    zuruecksetzen."""
    monkeypatch.setenv("AFAIR_SONNET_ESCALATION", "off")
    path = _control(tmp_path, '{"intervals": {"alpha": 900}}')
    monkeypatch.setenv("AFAIR_WORKER_CONTROL_FILE", str(path))
    sched = _sched(tmp_path, settings_local, [_AlphaWorker()])

    sched.refresh_control()
    assert os.environ["AFAIR_SONNET_ESCALATION"] == "off"


def test_escalation_defaults_to_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne Konfiguration verhaelt sich der Upstream unveraendert; ein
    Tippfehler senkt die Genauigkeit nicht stillschweigend."""
    from afair.agents.entity_canonicalizer import escalation_enabled

    monkeypatch.delenv("AFAIR_SONNET_ESCALATION", raising=False)
    assert escalation_enabled() is True
    monkeypatch.setenv("AFAIR_SONNET_ESCALATION", "vielleicht")
    assert escalation_enabled() is True
    for aus in ("off", "0", "false", "aus", "NEIN"):
        monkeypatch.setenv("AFAIR_SONNET_ESCALATION", aus)
        assert escalation_enabled() is False, aus

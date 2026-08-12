"""Lifespan-scoped background daemons (regression for the suite thread leak).

Building a server used to start four process-long daemon threads
(cold-path-scheduler, wal-checkpoint, export-purge, boot-warmup) as a
construction side effect: after three TestClient lifecycles ~12 threads
outlived their already-deleted tmp_path vaults, and the boot-warmup
thread's mid-suite litellm import could poison process state for later
tests. Startup now lives in the app lifespan and teardown stops/joins
every thread, so a closed lifespan leaves nothing behind — and merely
BUILDING a server (tests, the golden-surface dump) starts no threads
at all.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from starlette.testclient import TestClient

from afair.mcp.server import build_app, build_server
from afair.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

_LOOP_NAMES = {"cold-path-scheduler", "wal-checkpoint", "export-purge"}
_ALL_NAMES = _LOOP_NAMES | {"boot-warmup"}


def _settings(tmp_path: Path) -> Settings:
    # Cold path stays enabled: the scheduler thread must be part of the
    # start/stop roundtrip this file locks down.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


def _live_background_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name in _ALL_NAMES and t.is_alive()}


def test_lifespan_exit_stops_background_threads(tmp_path: Path) -> None:
    """The TestClient context (= one lifespan) must start the daemons and
    leave none of them running after exit."""
    before = _live_background_threads()

    with TestClient(build_app(_settings(tmp_path))) as client:
        assert client.get("/health").status_code == 200
        started = {t.name for t in _live_background_threads() - before}
        # boot-warmup is one-shot and may legitimately have finished already;
        # the three loops must be up.
        assert started >= _LOOP_NAMES

    leaked = _live_background_threads() - before
    # The teardown joins are bounded (a warmup mid-embedding-import can
    # outlast them); grant a generous grace here before calling it a leak.
    for thread in leaked:
        thread.join(timeout=10)
    assert {t.name for t in leaked if t.is_alive()} == set()


def test_build_server_spawns_no_threads(tmp_path: Path) -> None:
    """Constructing a server for inspection (tests, scripts/dump_mcp_surface)
    must not start any background daemon."""
    before = _live_background_threads()
    build_server(_settings(tmp_path))
    assert _live_background_threads() == before


def test_second_lifespan_on_same_app_restarts_daemons(tmp_path: Path) -> None:
    """A stopped scheduler must come back on the next lifespan of the same
    app object (stop() may not poison start())."""
    app = build_app(_settings(tmp_path))
    before = _live_background_threads()

    with TestClient(app):
        pass
    with TestClient(app):
        assert {t.name for t in _live_background_threads() - before} >= _LOOP_NAMES

    leaked = _live_background_threads() - before
    for thread in leaked:
        thread.join(timeout=10)
    assert {t.name for t in leaked if t.is_alive()} == set()

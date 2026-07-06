"""Regression: a failed interpretation appended after a success must not shadow
the success on the recall path (§3d).

Before the fix, ``read_latest_interpretation`` fetched only the single latest
extractor row and returned ``None`` when it was ``status: failed`` — hiding an
earlier success from recall. A retry against an already-succeeded event, or a
re-extraction on a model upgrade, could append such a later failure. The batch
variant encoded the same bug with a sentinel short-circuit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from afair.agents.interpretation import (
    read_latest_interpretation,
    read_latest_interpretations_batch,
)
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from pathlib import Path


def _insert_interp(
    conn,
    *,
    interp_id: str,
    event_id: str,
    event_hash: str,
    produced_by: str,
    produced_at: str,
    status: str,
) -> None:
    extraction = (
        {"status": "failed", "error_type": "llm_timeout", "error_message": "boom"}
        if status == "failed"
        else {"status": "success", "summary": "the real distillation"}
    )
    with conn:
        conn.execute(
            """
            INSERT INTO interpretations (
                id, event_id, event_hash, version, produced_at, produced_by, extraction
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (interp_id, event_id, event_hash, produced_at, produced_by, json.dumps(extraction)),
        )


@pytest.fixture
def conn(tmp_path: Path):
    db = open_db(tmp_path)
    try:
        yield db
    finally:
        db.close()


def test_failed_row_after_success_does_not_shadow_single(conn) -> None:
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "hi"}
    )
    # Older success, then a NEWER failed row for the same hash (distinct producer
    # to satisfy the UNIQUE constraint; both LIKE 'extractor:%').
    _insert_interp(
        conn,
        interp_id="01AAA",
        event_id=event.id,
        event_hash=event.content_hash,
        produced_by="extractor:v0",
        produced_at="2026-01-01T00:00:00+00:00",
        status="success",
    )
    _insert_interp(
        conn,
        interp_id="01BBB",
        event_id=event.id,
        event_hash=event.content_hash,
        produced_by="extractor:v0#retry1",
        produced_at="2026-02-01T00:00:00+00:00",
        status="failed",
    )

    interp = read_latest_interpretation(conn, event.content_hash)
    assert interp is not None, "the earlier success must not be shadowed by the later failure"
    assert interp.extraction["status"] == "success"
    assert interp.extraction["summary"] == "the real distillation"


def test_failed_row_after_success_does_not_shadow_batch(conn) -> None:
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "hey"}
    )
    _insert_interp(
        conn,
        interp_id="01CCC",
        event_id=event.id,
        event_hash=event.content_hash,
        produced_by="extractor:v0",
        produced_at="2026-01-01T00:00:00+00:00",
        status="success",
    )
    _insert_interp(
        conn,
        interp_id="01DDD",
        event_id=event.id,
        event_hash=event.content_hash,
        produced_by="extractor:v0#retry1",
        produced_at="2026-02-01T00:00:00+00:00",
        status="failed",
    )

    out = read_latest_interpretations_batch(conn, [event.content_hash])
    assert event.content_hash in out
    assert out[event.content_hash].extraction["status"] == "success"


def test_all_failed_still_yields_no_interpretation(conn) -> None:
    # A hash with only failed rows is still absent (single → None, batch → not present).
    event = write_event(
        conn, origin="user", kind="remember", payload={"content_type": "text", "text": "x"}
    )
    _insert_interp(
        conn,
        interp_id="01EEE",
        event_id=event.id,
        event_hash=event.content_hash,
        produced_by="extractor:v0",
        produced_at="2026-01-01T00:00:00+00:00",
        status="failed",
    )
    assert read_latest_interpretation(conn, event.content_hash) is None
    assert read_latest_interpretations_batch(conn, [event.content_hash]) == {}

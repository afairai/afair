"""Worker tests for the TemporalWorker (relevance-decay Phase 1).

Drive the worker by hand with the LLM mocked. Asserts against both the returned
stats dict and the substrate row it writes. Also guards idempotency (an
already-classified event is not re-sent to the LLM) and the eligibility query
(an event without an extractor interpretation is skipped).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import afair.agents.temporal as tw
from afair.agents.interpretation import write_interpretation
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, read_event_temporal, write_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tw, "_maybe_sleep", lambda _last: 0.0)


def _seed(db: sqlite3.Connection, text: str) -> str:
    """An event plus its extractor interpretation — the worker's input shape."""
    event = write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": text},
    )
    write_interpretation(
        db,
        event=event,
        version=1,
        produced_by="extractor:v1",
        extraction={"summary": text},
    )
    return event.content_hash


def _llm(data: dict[str, Any]) -> Callable[..., LLMResult]:
    def _fake(**kwargs: Any) -> LLMResult:
        return LLMResult(data=data, model=kwargs.get("model", "test"), raw="")

    return _fake


def _boom(**_kwargs: Any) -> LLMResult:
    raise AssertionError("the LLM should not have been called")


def test_worker_classifies_and_writes(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_hash = _seed(db, "dentist appointment next Friday")
    monkeypatch.setattr(
        tw,
        "call_tool",
        _llm(
            {
                "temporal_class": "one_off",
                "confidence": 0.85,
                "event_time": "2026-07-03",
                "relevance_horizon": "2026-07-04",
            }
        ),
    )
    stats = tw.TemporalWorker().run(db, settings)
    assert stats["events_classified"] == 1
    assert stats["llm_calls"] == 1
    assert stats["by_class"] == {"one_off": 1}

    row = read_event_temporal(db, event_hash)
    assert row is not None
    assert row.temporal_class == "one_off"
    assert row.event_time == "2026-07-03"
    assert row.computed_by == tw.TEMPORAL_VERSION


def test_llm_error_blocks_watermark_advance(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2a drain-blocker: a cycle where the LLM errors leaves the candidate
    unclassified — even though the batch is small (drained by size), the
    watermark must NOT advance, or that event would be skipped next cycle."""
    from afair.agents.llm import LLMError
    from afair.substrate import watermarks

    # Disable the concurrency lag so a CLEAN cycle *would* advance — isolating
    # the blocker as the reason it doesn't here.
    monkeypatch.setattr(watermarks, "FRONTIER_LAG_SECONDS", -3600)
    _seed(db, "some dated thing")

    def _raise(**_kwargs: Any) -> LLMResult:
        raise LLMError("provider down")

    monkeypatch.setattr(tw, "call_tool", _raise)
    stats = tw.TemporalWorker().run(db, settings)
    assert stats["llm_errors"] == 1
    assert stats["events_classified"] == 0
    assert watermarks.read_watermark_id(db, watermarks.WORKER_TEMPORAL) is None


def test_worker_is_idempotent(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db, "my birthday is March 3")
    monkeypatch.setattr(
        tw,
        "call_tool",
        _llm(
            {
                "temporal_class": "recurring",
                "confidence": 0.9,
                "recurrence_rule": "FREQ=YEARLY",
            }
        ),
    )
    tw.TemporalWorker().run(db, settings)

    # Second run: the row already exists, so the eligibility query returns
    # nothing and the LLM must not be touched.
    monkeypatch.setattr(tw, "call_tool", _boom)
    stats = tw.TemporalWorker().run(db, settings)
    assert stats["events_classified"] == 0
    assert stats["llm_calls"] == 0


def test_unknown_class_coerced_to_evergreen(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_hash = _seed(db, "I take my coffee as a flat white")
    monkeypatch.setattr(tw, "call_tool", _llm({"temporal_class": "nonsense", "confidence": 0.4}))
    tw.TemporalWorker().run(db, settings)
    row = read_event_temporal(db, event_hash)
    assert row is not None
    assert row.temporal_class == "evergreen"


def test_confidence_is_clamped(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_hash = _seed(db, "flight to Lisbon on the 12th")
    monkeypatch.setattr(tw, "call_tool", _llm({"temporal_class": "one_off", "confidence": 1.7}))
    tw.TemporalWorker().run(db, settings)
    row = read_event_temporal(db, event_hash)
    assert row is not None
    assert row.confidence == 1.0


def test_event_without_extraction_is_skipped(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An event with NO extractor interpretation is not eligible yet.
    write_event(
        db,
        origin="user",
        kind="remember",
        payload={"content_type": "text", "text": "not yet extracted"},
    )
    monkeypatch.setattr(tw, "call_tool", _boom)
    stats = tw.TemporalWorker().run(db, settings)
    assert stats["events_classified"] == 0
    assert stats["llm_calls"] == 0


# ── field sanitization (defense-in-depth behind llm's markup-leak repair) ───


def test_contaminated_optional_fields_are_dropped_not_stored(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production bug verbatim: a bridge leaked raw tool-call markup into
    closure_state. Even when such a value reaches the worker (i.e. the central
    repair in call_tool did not catch it), it must never be written."""
    event_hash = _seed(db, "promised to send the report next week")
    monkeypatch.setattr(
        tw,
        "call_tool",
        _llm(
            {
                "temporal_class": "commitment",
                "confidence": 0.8,
                "closure_state": (
                    'open</closure_state>\n<parameter name="relevance_horizon">2026-07-27T08:36:25Z'
                ),
                "event_time": "sometime next week",
                "relevance_horizon": "shortly after the event",
                "recurrence_rule": "every year <probably>",
            }
        ),
    )
    tw.TemporalWorker().run(db, settings)
    row = read_event_temporal(db, event_hash)
    assert row is not None
    assert row.temporal_class == "commitment"
    assert row.closure_state is None
    assert row.event_time is None
    assert row.relevance_horizon is None
    assert row.recurrence_rule is None


def test_valid_optional_fields_pass_through(
    db: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_hash = _seed(db, "dentist appointment on 2026-09-01 at 10:00")
    monkeypatch.setattr(
        tw,
        "call_tool",
        _llm(
            {
                "temporal_class": "one_off",
                "confidence": 0.9,
                "event_time": "2026-09-01T10:00:00Z",
                "relevance_horizon": "2026-09-01",
                "recurrence_rule": "FREQ=YEARLY;BYMONTH=9",
                "closure_state": "open",
            }
        ),
    )
    tw.TemporalWorker().run(db, settings)
    row = read_event_temporal(db, event_hash)
    assert row is not None
    assert row.event_time == "2026-09-01T10:00:00Z"
    assert row.relevance_horizon == "2026-09-01"
    assert row.recurrence_rule == "FREQ=YEARLY;BYMONTH=9"
    assert row.closure_state == "open"


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-27",
        "2026-07-27T08:36:25Z",
        "2026-07-27T08:36:25.123456+02:00",
        "2026-07-27 08:36",
        "2026-07-27T08:36:25+0200",
    ],
)
def test_opt_iso_accepts_iso_shapes(value: str) -> None:
    assert tw._opt_iso(value, field="event_time") == value


@pytest.mark.parametrize(
    "value",
    [
        "next Friday",
        "2026-07-27T08:36:25Z</relevance_horizon>",
        'open</closure_state>\n<parameter name="relevance_horizon">2026-07-27T08:36:25Z',
        "07/27/2026",
        "2026-7-27",
        "",
        None,
        42,
    ],
)
def test_opt_iso_rejects_non_iso(value: object) -> None:
    assert tw._opt_iso(value, field="event_time") is None


def test_opt_closure_whitelist() -> None:
    assert tw._opt_closure("open") == "open"
    assert tw._opt_closure("fulfilled") == "fulfilled"
    assert tw._opt_closure("superseded") == "superseded"
    assert tw._opt_closure("done") is None
    assert tw._opt_closure("open</closure_state>") is None
    assert tw._opt_closure(None) is None


def test_opt_rrule_sanity() -> None:
    assert tw._opt_rrule("FREQ=YEARLY") == "FREQ=YEARLY"
    assert tw._opt_rrule("FREQ=MONTHLY;BYMONTHDAY=27") == "FREQ=MONTHLY;BYMONTHDAY=27"
    assert tw._opt_rrule("every year") is None
    assert tw._opt_rrule("FREQ=YEARLY <x>") is None
    assert tw._opt_rrule(None) is None

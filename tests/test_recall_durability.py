"""Durability-rationale surfacing at verbosity="full" (W2).

Salience is computed by the cold-path worker into a separate ``salience:*``
interpretation row (invisible to the extractor-only recall read). W2 surfaces
it — salience / salience_components / why_durable — merged into the hit's
``interpretation`` dict, but ONLY at verbosity="full" (and by_id, which serves
full). Compact and standard stay on their existing shape and add ZERO queries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.interpretation import (
    read_latest_salience_batch,
    write_interpretation,
)
from afair.agents.salience import SALIENCE_PRODUCED_BY, SALIENCE_VERSION
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.handlers import _render_why_durable
from afair.mcp.schemas import TextContent
from afair.substrate import open_db, read_event_by_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _event_id: None)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db, vault_dir=tmp_path, inline_text_max_bytes=64 * 1024, semantic_recall_enabled=False
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


_SALIENCE = {
    "salience": 0.6234,
    "salience_components": {"entity_density": 0.5, "recency": 0.9, "has_conflict": 0.0},
    "status": "success",
}


def _seed_with_salience(ctx: ServerContext, text: str) -> str:
    written = handlers.remember(content=TextContent(type="text", text=text))
    event = read_event_by_id(ctx.db, written.event_id)
    assert event is not None
    write_interpretation(
        ctx.db,
        event=event,
        version=SALIENCE_VERSION,
        produced_by=SALIENCE_PRODUCED_BY,
        extraction=_SALIENCE,
    )
    return written.event_id


# ── merge behavior across verbosity ─────────────────────────────────────────


def test_full_hit_carries_durability(ctx: ServerContext) -> None:
    _seed_with_salience(ctx, "durabilitytoken alpha")
    res = handlers.recall(query="durabilitytoken", depth="shallow", verbosity="full")
    interp = res.hits[0].interpretation
    assert interp is not None
    assert interp["salience"] == 0.623  # rounded to 3dp
    assert interp["salience_components"] == _SALIENCE["salience_components"]
    assert "why_durable" in interp
    assert "salience 0.62" in interp["why_durable"]


def test_by_id_carries_durability(ctx: ServerContext) -> None:
    event_id = _seed_with_salience(ctx, "lookup durability")
    res = handlers.recall(by_id=event_id)
    interp = res.hits[0].interpretation
    assert interp is not None
    assert interp["salience"] == 0.623
    assert "why_durable" in interp


def test_compact_and_standard_omit_durability_and_add_no_query(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_with_salience(ctx, "durabilitytoken beta")

    calls = {"n": 0}
    real = handlers.read_latest_salience_batch

    def _counting(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(handlers, "read_latest_salience_batch", _counting)

    for verbosity in ("compact", "standard"):
        res = handlers.recall(query="durabilitytoken", depth="shallow", verbosity=verbosity)
        interp = res.hits[0].interpretation or {}
        assert "salience" not in interp, verbosity
        assert "why_durable" not in interp, verbosity
    # Zero added queries on the compact/standard hot path.
    assert calls["n"] == 0


def test_missing_salience_row_is_no_error(ctx: ServerContext) -> None:
    """An event with no salience row serves a full hit without the durability
    keys, never an error."""
    written = handlers.remember(content=TextContent(type="text", text="no salience here"))
    res = handlers.recall(by_id=written.event_id)
    interp = res.hits[0].interpretation or {}
    assert "salience" not in interp
    assert "why_durable" not in interp


# ── read helper: latest-wins ────────────────────────────────────────────────


def test_read_latest_salience_batch_latest_wins(ctx: ServerContext) -> None:
    written = handlers.remember(content=TextContent(type="text", text="latest wins"))
    event = read_event_by_id(ctx.db, written.event_id)
    assert event is not None
    # Two salience rows for the same event, older then newer (distinct producer
    # suffix so the UNIQUE(event_hash, version, produced_by) allows both).
    for produced_by, produced_at, sal in (
        ("salience:v0", "2026-01-01T00:00:00+00:00", 0.10),
        ("salience:v1", "2026-06-01T00:00:00+00:00", 0.90),
    ):
        ctx.db.execute(
            "INSERT INTO interpretations (id, event_id, event_hash, version, produced_at, "
            "produced_by, extraction) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"i-{produced_by}",
                event.id,
                event.content_hash,
                0,
                produced_at,
                produced_by,
                f'{{"salience": {sal}, "status": "success"}}',
            ),
        )
    ctx.db.commit()
    out = read_latest_salience_batch(ctx.db, [event.content_hash])
    assert out[event.content_hash]["salience"] == 0.90  # newest produced_at wins
    assert read_latest_salience_batch(ctx.db, []) == {}


# ── _render_why_durable unit cases ──────────────────────────────────────────


def test_why_durable_full() -> None:
    why = _render_why_durable(
        {"salience": 0.62, "salience_components": {"entity_density": 0.5, "recency": 0.9}},
        {"temporal_class": "deadline", "surprise_score": 0.8},
    )
    assert why is not None
    # Top-2 drivers by value (recency 0.9 > entity_density 0.5), both parts present.
    assert why == "salience 0.62 (recency, entity_density); temporal:deadline; surprise 0.8"


def test_why_durable_missing_temporal() -> None:
    why = _render_why_durable({"salience": 0.4}, {"surprise_score": 0.3})
    assert why == "salience 0.4; surprise 0.3"


def test_why_durable_missing_surprise() -> None:
    why = _render_why_durable({"salience": 0.4}, {"temporal_class": "recurring"})
    assert why == "salience 0.4; temporal:recurring"


def test_why_durable_all_missing_is_none() -> None:
    assert _render_why_durable({}, None) is None
    assert _render_why_durable({"salience_components": {}}, {}) is None

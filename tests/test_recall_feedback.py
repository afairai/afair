"""Tests for the optional ``feedback`` argument on ``recall``.

The feedback channel is the explicit user-correction signal the tuner
reads to validate parameter changes (per
analysis/2026-06-03-recursive-self-improvement.md §2.1).

Per I1, ``feedback`` is an additive optional argument on the existing
``recall`` tool — no new MCP verb. Tests verify:
  * recall(feedback=None) is unaffected (backwards-compatible).
  * recall(feedback=...) writes a tuner_state observation row.
  * Bounded payloads (cap on IDs per call, topic length).
  * Empty feedback is a no-op (no DB write).
"""

from __future__ import annotations

import pytest

from afair.mcp import handlers
from afair.mcp.schemas import (
    MAX_FEEDBACK_IDS_PER_CALL,
    MAX_FEEDBACK_TOPIC_CHARS,
    RecallFeedback,
)
from afair.substrate import open_db, tuner_state


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """Minimal ServerContext for handlers.recall."""
    from afair.mcp.context import ServerContext, set_context

    vault = tmp_path / "vault"
    vault.mkdir()
    db = open_db(vault)
    server_ctx = ServerContext(
        db=db,
        vault_dir=vault,
        inline_text_max_bytes=64 * 1024,
        embedding_dim=1024,
        embedding_model="stub",
        surprise_context_window=20,
        semantic_recall_enabled=False,
    )
    set_context(server_ctx)
    # Patch connect_for_thread so handlers.recall reuses this connection.
    monkeypatch.setattr(handlers, "connect_for_thread", lambda: db)
    yield server_ctx
    db.close()


# ─── absence keeps recall unchanged ───────────────────────────────────────


def test_recall_no_feedback_writes_no_tuner_row(ctx) -> None:
    handlers.recall(query="nothing in this vault")
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert rows == []


def test_recall_explicit_none_feedback_no_op(ctx) -> None:
    handlers.recall(query="anything", feedback=None)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert rows == []


def test_recall_empty_feedback_no_op(ctx) -> None:
    """All fields empty → no signal → no DB row."""
    fb = RecallFeedback()
    handlers.recall(query="anything", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert rows == []


# ─── valid feedback persists ──────────────────────────────────────────────


def test_recall_useful_ids_persisted(ctx) -> None:
    fb = RecallFeedback(useful_event_ids=["sgn_a", "sgn_b"])
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    assert rows[0].kind == "observation"
    assert rows[0].evidence == {
        "useful_event_ids": ["sgn_a", "sgn_b"],
        "not_useful_event_ids": [],
        "missing_topic": None,
    }


def test_recall_not_useful_ids_persisted(ctx) -> None:
    fb = RecallFeedback(not_useful_event_ids=["sgn_x"])
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    assert rows[0].evidence["not_useful_event_ids"] == ["sgn_x"]


def test_recall_missing_topic_persisted(ctx) -> None:
    fb = RecallFeedback(missing_topic="expected info about Project X")
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    assert rows[0].evidence["missing_topic"] == "expected info about Project X"


def test_recall_combined_feedback_persisted(ctx) -> None:
    fb = RecallFeedback(
        useful_event_ids=["sgn_a"],
        not_useful_event_ids=["sgn_b", "sgn_c"],
        missing_topic="schema migration story",
    )
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    e = rows[0].evidence
    assert e["useful_event_ids"] == ["sgn_a"]
    assert e["not_useful_event_ids"] == ["sgn_b", "sgn_c"]
    assert e["missing_topic"] == "schema migration story"


# ─── bounds are enforced ──────────────────────────────────────────────────


def test_id_list_truncated_to_cap(ctx) -> None:
    too_many = [f"sgn_{i}" for i in range(MAX_FEEDBACK_IDS_PER_CALL + 25)]
    fb = RecallFeedback(useful_event_ids=too_many)
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    assert len(rows[0].evidence["useful_event_ids"]) == MAX_FEEDBACK_IDS_PER_CALL


def test_topic_truncated(ctx) -> None:
    long_topic = "a" * (MAX_FEEDBACK_TOPIC_CHARS + 200)
    fb = RecallFeedback(missing_topic=long_topic)
    handlers.recall(query="x", feedback=fb)
    rows = tuner_state.history(ctx.db, worker="recall", tunable="feedback")
    assert len(rows) == 1
    assert len(rows[0].evidence["missing_topic"]) == MAX_FEEDBACK_TOPIC_CHARS


# ─── persistence failure must NOT break recall ────────────────────────────


def test_recall_returns_results_even_if_feedback_persist_fails(ctx, monkeypatch) -> None:
    """If tuner_state.write raises, recall still returns hits cleanly."""
    from afair.substrate import tuner_state as ts

    def boom(*args, **kwargs):
        raise RuntimeError("substrate hiccup")

    monkeypatch.setattr(ts, "write", boom)
    fb = RecallFeedback(useful_event_ids=["sgn_a"])
    # Must not raise — recall is durable against signal-write failures.
    result = handlers.recall(query="x", feedback=fb)
    assert result.hits == []  # empty vault → no hits

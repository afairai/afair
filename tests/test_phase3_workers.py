"""Phase 3 worker tests — Pruner, Conflict-Resolver, Consolidator."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from afair.agents.binder import BINDER_PRODUCED_BY
from afair.agents.conflict_resolver import (
    ConflictResolver,
    read_conflicts_batch,
)
from afair.agents.consolidator import (
    CONSOLIDATION_KIND,
    Consolidator,
)
from afair.agents.interpretation import write_interpretation
from afair.agents.invalidation import write_invalidation
from afair.agents.llm import LLMResult
from afair.agents.pruner import Pruner
from afair.settings import Settings
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path)


@pytest.fixture
def settings_local(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
    )


# ── Pruner ────────────────────────────────────────────────────────────────


def _insert_pipeline_event(db: sqlite3.Connection, *, pid: str, recorded_at: str) -> None:
    with db:
        db.execute(
            "INSERT INTO pipeline_events (id, event_id, stage, status, recorded_at) "
            "VALUES (?, '01EV', 'event.written', 'ok', ?)",
            (pid, recorded_at),
        )


def test_pruner_ages_out_old_telemetry_keeps_recent(db, settings_local: Settings) -> None:
    """ADR-0005: pipeline_events + observability_snapshots older than
    telemetry_retention_days are deleted; recent rows are kept. The retired
    triggers no longer ABORT the delete."""
    from datetime import UTC, datetime, timedelta

    from afair.substrate import observability

    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    _insert_pipeline_event(db, pid="pe_old", recorded_at=old)
    _insert_pipeline_event(db, pid="pe_recent", recorded_at=recent)
    # observability_snapshots: write two, then back-date one to the old window.
    observability.write_snapshot(db, producer="test", counters={"a": 1})
    observability.write_snapshot(db, producer="test", counters={"b": 2})
    with db:
        rows = db.execute("SELECT id FROM observability_snapshots ORDER BY id").fetchall()
        db.execute(
            "UPDATE observability_snapshots SET recorded_at = ? WHERE id = ?", (old, rows[0]["id"])
        )

    stats = Pruner().run(db, settings_local)  # default retention 90 days

    # One old pipeline_event + one old snapshot pruned; both recent rows kept.
    assert stats["telemetry_rows_deleted"] == 2
    pe_ids = {r["id"] for r in db.execute("SELECT id FROM pipeline_events").fetchall()}
    assert pe_ids == {"pe_recent"}
    assert db.execute("SELECT COUNT(*) AS n FROM observability_snapshots").fetchone()["n"] == 1


def test_pruner_telemetry_respects_retention_setting(db, settings_local: Settings) -> None:
    """A shorter telemetry_retention_days prunes more aggressively; a longer one
    keeps rows the default would have dropped."""
    from datetime import UTC, datetime, timedelta

    recorded = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    _insert_pipeline_event(db, pid="pe30", recorded_at=recorded)

    # 30-day-old row survives the default 90-day window.
    assert Pruner().run(db, settings_local)["telemetry_rows_deleted"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM pipeline_events").fetchone()["n"] == 1

    # With a 7-day window it is pruned.
    tight = settings_local.model_copy(update={"telemetry_retention_days": 7})
    assert Pruner().run(db, tight)["telemetry_rows_deleted"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM pipeline_events").fetchone()["n"] == 0


def test_pruner_deletes_expired_oauth_rows(db, settings_local: Settings) -> None:
    """Past expires_at → row goes. Future expires_at → row stays."""
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    now = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO oauth_codes(
            code, client_id, redirect_uri, scope, code_challenge,
            code_challenge_method, user_sub, user_email, expires_at, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("expired", "c", "r", "s", "ch", "S256", "u", "e", past, now),
    )
    db.execute(
        """INSERT INTO oauth_codes(
            code, client_id, redirect_uri, scope, code_challenge,
            code_challenge_method, user_sub, user_email, expires_at, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("fresh", "c", "r", "s", "ch", "S256", "u", "e", future, now),
    )
    db.commit()

    stats = Pruner().run(db, settings_local)
    assert stats["oauth_codes_deleted"] == 1
    remaining = db.execute("SELECT code FROM oauth_codes").fetchall()
    assert [r["code"] for r in remaining] == ["fresh"]


def test_pruner_does_not_touch_events_table(db, settings_local: Settings) -> None:
    """I2 — Pruner MUST NEVER delete from events."""
    write_event(db, origin="u", kind="remember", payload={"content_type": "text", "text": "x"})
    write_event(db, origin="u", kind="remember", payload={"content_type": "text", "text": "y"})
    before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    Pruner().run(db, settings_local)
    after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert before == after


def _pc_entity(db, name: str, kind: str) -> str:
    from afair.substrate import write_entity

    ev = write_event(
        db, origin="u", kind="remember", payload={"content_type": "text", "text": name}
    )
    return write_entity(
        db, canonical_name=name, kind=kind, created_by="t", source_event_id=ev.id, confidence=0.8
    ).id


def _pc_insert(
    db, *, pid: str, kind: str, entity_id: str, status: str, decided_at: str | None
) -> None:
    db.execute(
        """
        INSERT INTO proposed_corrections (
            id, kind, entity_id, detail, evidence, confidence, tier,
            detected_by, detected_at, status, decided_at
        ) VALUES (?, ?, ?, '{}', 'ev', 0.5, 'review', 'test', ?, ?, ?)
        """,
        (pid, kind, entity_id, "2026-01-01T00:00:00+00:00", status, decided_at),
    )
    db.commit()


def test_pruner_ages_out_only_decided_edge_reviews(db, settings_local: Settings) -> None:
    """P1-1 hygiene: decided edge_review rows past the retention window are
    deleted; open edge_reviews, fresh decided edge_reviews, and decided
    non-edge_review proposals (anti-re-nag memory) are all kept."""
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    recent = (datetime.now(UTC) - timedelta(days=5)).isoformat()

    e1 = _pc_entity(db, "SubjOldDecided", "product")
    e2 = _pc_entity(db, "SubjOpen", "product")
    e3 = _pc_entity(db, "SubjFreshDecided", "product")
    e4 = _pc_entity(db, "SubjMergeReview", "product")

    _pc_insert(
        db, pid="old_dec", kind="edge_review", entity_id=e1, status="applied", decided_at=old
    )
    _pc_insert(db, pid="open", kind="edge_review", entity_id=e2, status="proposed", decided_at=None)
    _pc_insert(
        db, pid="fresh_dec", kind="edge_review", entity_id=e3, status="applied", decided_at=recent
    )
    _pc_insert(
        db, pid="mr_dec", kind="merge_review", entity_id=e4, status="confirmed", decided_at=old
    )

    stats = Pruner().run(db, settings_local)
    assert stats["decided_edge_reviews_deleted"] == 1

    remaining = {r["id"] for r in db.execute("SELECT id FROM proposed_corrections").fetchall()}
    assert remaining == {"open", "fresh_dec", "mr_dec"}  # old decided edge_review gone

    # End-to-end recycle: after prune, a fresh OPEN edge_review for the pruned
    # subject inserts cleanly (its old decided row no longer exists / never
    # blocked; the partial index only counts open rows).
    _pc_insert(
        db, pid="recycled", kind="edge_review", entity_id=e1, status="proposed", decided_at=None
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM proposed_corrections WHERE entity_id = ? AND status = 'proposed'",
            (e1,),
        ).fetchone()[0]
        == 1
    )


# ── Conflict-Resolver ─────────────────────────────────────────────────────


def _make_bound_pair(db) -> tuple[Any, Any]:
    """Create two events with a binder:v0 row linking them — preset
    fixture mimicking what the Bind agent produces in production."""
    a = write_event(
        db, origin="u", kind="remember", payload={"content_type": "text", "text": "Sajinth is CEO"}
    )
    b = write_event(
        db, origin="u", kind="remember", payload={"content_type": "text", "text": "Sajinth is CTO"}
    )
    # Manually write a binder row linking the two.
    write_interpretation(
        db,
        event=a,
        version=1,
        produced_by=BINDER_PRODUCED_BY,
        extraction={
            "status": "success",
            "type": "bind",
            "links": [{"event_hash": b.content_hash, "distance": 0.1}],
        },
    )
    return a, b


def test_conflict_resolver_judges_bound_pair_and_writes_verdict(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: pair exists, LLM returns a 'conflicts' verdict, a
    conflict_flag row gets written keyed on the pair, and the unresolved-
    conflict rollup the caveats layer reads is incremented."""
    a, b = _make_bound_pair(db)

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={
                "verdict": "conflicts",
                "reason": "CEO and CTO at the same time are mutually exclusive.",
                "confidence": 0.92,
            },
            model="mock",
            raw="",
        )

    monkeypatch.setattr("afair.agents.conflict_resolver.call_tool", fake_call)

    stats = ConflictResolver().run(db, settings_local)
    assert stats["pairs_examined"] == 1
    assert stats["conflicts"] == 1
    assert stats["unresolved_conflicts"] == 1

    # The interpretation row is keyed on the anchor with a producer
    # encoding the other side's hash.
    row = db.execute(
        """SELECT extraction FROM interpretations
           WHERE event_hash = ? AND produced_by LIKE 'conflict_resolver:v0:%'""",
        (a.content_hash,),
    ).fetchone()
    assert row is not None
    data = json.loads(row["extraction"])
    assert data["verdict"] == "conflicts"
    assert data["event_b_hash"] == b.content_hash
    assert "verdict_taxonomy_version" in data


def test_conflict_flag_points_at_the_counterpart_event_id(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3c: the symmetric flag's with_event_id must point at the OTHER side of
    the pair. Recall on A flags B's id; recall on B flags A's id. Before the fix
    with_event_id was hard-wired to event_b_id, so a flag surfaced on anchor B
    pointed back at B itself."""
    a, b = _make_bound_pair(db)

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={"verdict": "conflicts", "reason": "clash", "confidence": 0.9},
            model="mock",
            raw="",
        )

    monkeypatch.setattr("afair.agents.conflict_resolver.call_tool", fake_call)
    ConflictResolver().run(db, settings_local)

    flags = read_conflicts_batch(db, [a.content_hash, b.content_hash])

    a_flag = flags[a.content_hash][0]
    assert a_flag["with_event_id"] == b.id
    assert a_flag["with_content_hash"] == b.content_hash

    b_flag = flags[b.content_hash][0]
    assert b_flag["with_event_id"] == a.id  # was b.id before the fix
    assert b_flag["with_content_hash"] == a.content_hash


def test_conflict_resolver_normalizes_legacy_verdict_and_enforces_floor(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model returning the historical 'contradicts' string is normalized to
    'conflicts'; a low-confidence conflict is downgraded to 'unsure' (the
    confidence floor, double-enforced in code)."""
    a, _b = _make_bound_pair(db)

    def low_conf_legacy(**_: Any) -> LLMResult:
        return LLMResult(
            data={"verdict": "contradicts", "reason": "maybe", "confidence": 0.4},
            model="mock",
            raw="",
        )

    monkeypatch.setattr("afair.agents.conflict_resolver.call_tool", low_conf_legacy)
    stats = ConflictResolver().run(db, settings_local)
    # 'contradicts' → 'conflicts', then floored to 'unsure' at conf 0.4.
    assert stats.get("conflicts", 0) == 0
    assert stats["unsure"] == 1
    assert stats["unresolved_conflicts"] == 0
    row = db.execute(
        """SELECT extraction FROM interpretations
           WHERE event_hash = ? AND produced_by LIKE 'conflict_resolver:v0:%'""",
        (a.content_hash,),
    ).fetchone()
    assert json.loads(row["extraction"])["verdict"] == "unsure"


def test_conflict_resolver_skips_already_judged_pairs(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second cycle on the same pair → skipped (no LLM call)."""
    _make_bound_pair(db)
    calls = {"n": 0}

    def fake_call(**_: Any) -> LLMResult:
        calls["n"] += 1
        return LLMResult(
            data={"verdict": "compatible", "reason": "x", "confidence": 0.7}, model="m", raw=""
        )

    monkeypatch.setattr("afair.agents.conflict_resolver.call_tool", fake_call)

    ConflictResolver().run(db, settings_local)
    ConflictResolver().run(db, settings_local)
    assert calls["n"] == 1, "second cycle should skip the already-judged pair"


def test_conflict_resolver_skips_invalidate_events(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invalidation event linked to its target should NOT be judged
    — invalidation has its own semantics, not contradiction-judgment."""
    a = write_event(
        db, origin="u", kind="remember", payload={"content_type": "text", "text": "fact"}
    )
    inv = write_invalidation(db, target_hash=a.content_hash, reason="superseded", origin="u")
    # Manually link them via binder (artificial — but simulates the case)
    write_interpretation(
        db,
        event=a,
        version=1,
        produced_by=BINDER_PRODUCED_BY,
        extraction={
            "status": "success",
            "type": "bind",
            "links": [{"event_hash": inv.content_hash, "distance": 0.1}],
        },
    )

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={"verdict": "contradicts", "reason": "x", "confidence": 0.9}, model="m", raw=""
        )

    monkeypatch.setattr("afair.agents.conflict_resolver.call_tool", fake_call)

    stats = ConflictResolver().run(db, settings_local)
    assert stats["pairs_examined"] == 0


# ── Consolidator ───────────────────────────────────────────────────────────


def test_consolidator_skips_day_below_min_threshold(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day with 2 events doesn't trigger consolidation (min is 3)."""
    write_event(db, origin="u", kind="remember", payload={"content_type": "text", "text": "one"})
    write_event(db, origin="u", kind="remember", payload={"content_type": "text", "text": "two"})

    def fake_call(**_: Any) -> LLMResult:
        msg = "consolidator should not have called the LLM"
        raise AssertionError(msg)

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call)

    stats = Consolidator().run(db, settings_local)
    assert stats["days_consolidated"] == 0
    assert stats["days_skipped_few_events"] >= 1


def test_consolidator_writes_consolidation_event_for_full_day(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Day with ≥3 events → LLM is called and a consolidation event
    with kind='consolidation' lands in the substrate."""
    for i in range(4):
        write_event(
            db,
            origin="u",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={
                "narrative": "Today you wrote four small notes.",
                "themes": ["Notes", "Experimentation"],
                "open_threads": ["follow up tomorrow"],
            },
            model="m",
            raw="",
        )

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call)

    stats = Consolidator().run(db, settings_local)
    assert stats["days_consolidated"] >= 1
    row = db.execute("SELECT payload FROM events WHERE kind = ?", (CONSOLIDATION_KIND,)).fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert "four small notes" in payload["text"]
    assert payload["themes"] == ["Notes", "Experimentation"]
    assert payload["event_count"] == 4


def test_consolidator_idempotent_skips_already_consolidated_days(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running twice on the same day creates one consolidation, not two."""
    for i in range(4):
        write_event(
            db,
            origin="u",
            kind="remember",
            payload={"content_type": "text", "text": f"e{i}"},
        )

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={"narrative": "x", "themes": ["t"], "open_threads": []}, model="m", raw=""
        )

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call)

    Consolidator().run(db, settings_local)
    Consolidator().run(db, settings_local)
    count = db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = ?", (CONSOLIDATION_KIND,)
    ).fetchone()[0]
    assert count == 1


def test_consolidator_does_not_consume_its_own_output(
    db, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consolidation event itself MUST NOT be picked up by the next
    Consolidator cycle — otherwise we'd get infinite recursive summaries."""
    for i in range(4):
        write_event(
            db,
            origin="u",
            kind="remember",
            payload={"content_type": "text", "text": f"x{i}"},
        )

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={"narrative": "n", "themes": ["t"], "open_threads": []}, model="m", raw=""
        )

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call)

    Consolidator().run(db, settings_local)
    # Use _events_for_day directly to confirm the consolidation is excluded.
    from datetime import UTC, datetime

    from afair.agents.consolidator import _events_for_day

    events = _events_for_day(db, datetime.now(UTC).date())
    kinds = {e.kind for e in events}
    assert CONSOLIDATION_KIND not in kinds


_ = time  # used implicitly elsewhere; keep linter happy


# ── consolidator: LLM-returns-string-instead-of-list defense ─────────────


def test_consolidator_handles_llm_returning_themes_as_string(
    db: sqlite3.Connection, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: bug discovered 2026-05-26 via vault audit (observe event
    01KSJX608Q63GJ6TP9TY5A8XZ7). Consolidation event 01KSHN1BRJ8TX4HZ1VNNFSNV3M
    had a corrupted ``context`` field where individual characters were joined
    by ', ' — root cause was Haiku returning ``themes`` as a single STRING
    instead of a JSON array. The old code did
    ``[str(t) for t in (data.get("themes") or [])]`` which iterates a string
    over its characters and silently produces a list-of-single-chars.

    With the _coerce_to_string_list type-guard, a string value should be
    wrapped as a single-element list (with a structured warning), NOT
    iterated character-by-character. The resulting consolidation event's
    ``themes`` field must therefore be a real list of full strings.
    """
    for i in range(4):
        write_event(
            db,
            origin="u",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )

    def fake_call_returning_string_themes(**_: Any) -> LLMResult:
        # Note: themes returned as a STRING, not a list.
        return LLMResult(
            data={
                "narrative": "a real narrative",
                "themes": "vendor neutrality, OAuth complete, Sprint A shipped",
                "open_threads": "auto-linking refinement needed",
            },
            model="m",
            raw="",
        )

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call_returning_string_themes)

    Consolidator().run(db, settings_local)

    # Find the consolidation event and inspect its themes + context.
    rows = db.execute(f"SELECT payload FROM events WHERE kind = '{CONSOLIDATION_KIND}'").fetchall()
    assert len(rows) == 1
    import json as _json

    payload = _json.loads(rows[0]["payload"])
    themes = payload["themes"]

    # Critical assertion: themes is a list of WHOLE strings, not a list
    # of single-character strings.
    assert isinstance(themes, list)
    assert len(themes) == 1, f"expected single-element wrap, got {len(themes)}: {themes[:5]}"
    assert themes[0] == "vendor neutrality, OAuth complete, Sprint A shipped"

    # And the derived ``context`` field must look coherent — no
    # comma-separated single characters.
    context = payload["context"]
    assert "v, e, n, d, o, r" not in context, (
        "context contains character-iteration corruption — type guard regressed"
    )
    assert "vendor neutrality" in context


def test_consolidator_handles_llm_returning_themes_as_list(
    db: sqlite3.Connection, settings_local: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: when the LLM correctly returns themes as a list, the
    coercion is a no-op and themes are stored unchanged."""
    for i in range(4):
        write_event(
            db,
            origin="u",
            kind="remember",
            payload={"content_type": "text", "text": f"event {i}"},
        )

    def fake_call(**_: Any) -> LLMResult:
        return LLMResult(
            data={
                "narrative": "n",
                "themes": ["theme one", "theme two", "theme three"],
                "open_threads": ["open one"],
            },
            model="m",
            raw="",
        )

    monkeypatch.setattr("afair.agents.consolidator.call_tool", fake_call)

    Consolidator().run(db, settings_local)

    import json as _json

    rows = db.execute(f"SELECT payload FROM events WHERE kind = '{CONSOLIDATION_KIND}'").fetchall()
    payload = _json.loads(rows[0]["payload"])
    assert payload["themes"] == ["theme one", "theme two", "theme three"]
    assert payload["open_threads"] == ["open one"]


def test_coerce_to_string_list_unit_cases() -> None:
    """Unit-level cases for ``_coerce_to_string_list``: list, tuple,
    string, None, empty string, and unexpected types all handled safely."""
    from afair.agents.consolidator import _coerce_to_string_list

    # Lists pass through (with str-cast for safety).
    assert _coerce_to_string_list(["a", "b", "c"], field="themes") == ["a", "b", "c"]
    assert _coerce_to_string_list([1, 2, 3], field="themes") == ["1", "2", "3"]

    # Tuples treated like lists.
    assert _coerce_to_string_list(("a", "b"), field="themes") == ["a", "b"]

    # Single string wrapped as one element.
    assert _coerce_to_string_list("hello world", field="themes") == ["hello world"]

    # Empty/None fall back to empty list.
    assert _coerce_to_string_list(None, field="themes") == []
    assert _coerce_to_string_list("", field="themes") == []
    assert _coerce_to_string_list("   ", field="themes") == []

    # Unexpected types fall back to empty list (not crash).
    assert _coerce_to_string_list(42, field="themes") == []
    assert _coerce_to_string_list({"key": "val"}, field="themes") == []

"""Content gates preserve useful results and leave all observations stored."""

import pytest

from afair.agents.binder import BINDER_PRODUCED_BY
from afair.agents.conflict_resolver import _enumerate_candidate_pairs
from afair.agents.evidence_quality import is_routine_observation
from afair.agents.interpretation import write_interpretation
from afair.substrate import open_db, write_event


@pytest.mark.parametrize(
    "result,extra,routine",
    [
        ("ok", {}, True),
        ("tests passed", {}, False),
        ("The budget changed from 150 to 160", {}, False),
        ("failed", {}, False),
        ("ok", {"correction": "The owner is Ana"}, False),
        ("ok", {"context": "User corrected the release date"}, False),
    ],
)
def test_only_content_free_results_are_routine(tmp_path, result, extra, routine):
    conn = open_db(tmp_path)
    try:
        event = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Atlas",
                "result": result,
                **extra,
            },
        )
        assert is_routine_observation(event) is routine
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("result,expected", [("ok", 0), ("Owner changed to Ana", 1)])
def test_conflict_pairs_use_outcome_not_action_name(tmp_path, result, expected):
    conn = open_db(tmp_path)
    try:
        anchor = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Atlas",
                "result": result,
            },
        )
        linked = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Atlas",
                "result": "Owner is Bruno",
            },
        )
        write_interpretation(
            conn,
            event=anchor,
            version=1,
            produced_by=BINDER_PRODUCED_BY,
            extraction={
                "links": [{"event_hash": linked.content_hash, "distance": 0.1}],
            },
        )
        assert len(_enumerate_candidate_pairs(conn, max_pairs=20)) == expected
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    finally:
        conn.close()


def test_actual_observe_shape_with_null_result_is_routine(tmp_path):
    from afair.mcp.schemas import ObserveEvent

    conn = open_db(tmp_path)
    try:
        payload = ObserveEvent(action="read", subject="Atlas").model_dump()
        payload["content_type"] = "event"
        event = write_event(conn, origin="agent", kind="observe", payload=payload)
        assert event.payload["result"] is None
        assert is_routine_observation(event)
    finally:
        conn.close()


@pytest.mark.parametrize("result", [0, False])
def test_falsey_factual_result_remains_evidence(tmp_path, result):
    conn = open_db(tmp_path)
    try:
        event = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Atlas budget",
                "result": result,
            },
        )
        assert not is_routine_observation(event)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (
            {"subject": "Atlas", "result": "Owner is Ana"},
            {"subject": "Borealis", "result": "Owner is Bruno"},
            False,
        ),
        (
            {"subject": "Atlas", "result": "Owner is Ana"},
            {"subject": "Atlas", "result": "Owner is Bruno"},
            True,
        ),
        (
            {"subject": "Atlas budget", "result": "Budget is 150"},
            {"subject": "Atlas owner", "result": "Owner is Ana"},
            True,
        ),
        (
            {"subject": "tests", "result": "14 failing"},
            {"subject": "tests", "result": "14 passing"},
            False,
        ),
        (
            {"subject": "run tests", "result": "14 failing"},
            {"subject": "run tests", "result": "14 passing"},
            False,
        ),
        (
            {"subject": "README.md", "result": "Owner is Ana"},
            {"subject": "README.md", "result": "Owner is Bruno"},
            False,
        ),
        (
            {"subject": "src/index.ts", "result": "Owner is Ana"},
            {"subject": "src/index.ts", "result": "Owner is Bruno"},
            False,
        ),
        (
            {"subject": "/projects/atlas/src/index.ts", "result": "14 failing"},
            {"subject": "/projects/atlas/src/index.ts", "result": "14 passing"},
            True,
        ),
        (
            {"subject": "README.md", "project": "Atlas", "result": "Owner is Ana"},
            {"subject": "README.md", "project": "Borealis", "result": "Owner is Bruno"},
            False,
        ),
        (
            {"subject": "README.md", "project": "Atlas", "result": "Owner is Ana"},
            {"subject": "README.md", "project": "Atlas", "result": "Owner is Bruno"},
            True,
        ),
        (
            {"subject": "tests", "task_id": "ABC-42", "result": "14 failing"},
            {"subject": "build", "task_id": "ABC-42", "result": "14 passing"},
            True,
        ),
        (
            {"subject": "tests", "context": "Atlas pricing migration", "result": "14 failing"},
            {"subject": "build", "context": "Atlas pricing migration", "result": "14 passing"},
            True,
        ),
        (
            {"subject": "tests", "context": "run unit tests", "result": "14 failing"},
            {"subject": "build", "context": "run unit tests", "result": "14 passing"},
            False,
        ),
    ],
)
def test_paid_comparison_needs_plausible_shared_reference(tmp_path, a, b, expected):
    from afair.agents.evidence_quality import should_compare_observations

    conn = open_db(tmp_path)
    try:
        left = write_event(conn, origin="agent", kind="observe", payload={"action": "check", **a})
        right = write_event(conn, origin="agent", kind="observe", payload={"action": "check", **b})
        assert should_compare_observations(left, right) is expected
    finally:
        conn.close()


def test_explicit_lineage_and_graph_preserve_useful_pairs(tmp_path):
    from afair.agents.evidence_quality import should_compare_observations

    conn = open_db(tmp_path)
    try:
        left = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "result": "Budget is 150",
            },
        )
        right = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "result": "Budget is 151",
            },
            parent_hashes=[left.content_hash],
        )
        assert should_compare_observations(left, right)
        right = right.model_copy(update={"parent_hashes": None})
        assert not should_compare_observations(left, right)
        assert should_compare_observations(
            left,
            right,
            entity_ids_a=frozenset({"ent_atlas"}),
            entity_ids_b=frozenset({"ent_atlas"}),
        )
        assert not should_compare_observations(
            left,
            right,
            entity_ids_a=frozenset({"ent_atlas"}),
            entity_ids_b=frozenset({"ent_borealis"}),
        )
    finally:
        conn.close()


def test_prior_unresolved_flags_remain_visible_when_future_pair_is_filtered(tmp_path):
    from afair.agents.conflict_resolver import (
        ConflictPair,
        _write_verdict,
        read_conflicts_batch,
    )

    conn = open_db(tmp_path)
    try:
        left = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Atlas",
                "result": "Owner is Ana",
            },
        )
        right = write_event(
            conn,
            origin="agent",
            kind="observe",
            payload={
                "action": "check",
                "subject": "Borealis",
                "result": "Owner is Bruno",
            },
        )
        _write_verdict(
            conn,
            event_a=left,
            pair=ConflictPair(
                event_a_hash=left.content_hash,
                event_b_hash=right.content_hash,
                event_a_id=left.id,
                event_b_id=right.id,
                verdict="conflicts",
                reason="historical judgment",
                confidence=0.9,
            ),
        )
        flags = read_conflicts_batch(conn, [left.content_hash, right.content_hash])
        assert flags[left.content_hash]
        assert flags[right.content_hash]
        assert flags[left.content_hash][0]["verdict"] == "conflicts"
    finally:
        conn.close()

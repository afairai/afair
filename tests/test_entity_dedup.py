"""Tests for the entity deduplicator (same-name cross-kind merge).

The LLM judge is mocked via monkeypatch on agents.entity_dedup.call_tool.
"""

from __future__ import annotations

import pytest

from afair.agents import entity_dedup as ed
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.corrections import DECIDED_BY_OPERATOR
from afair.substrate.entities import (
    resolve_canonical,
    write_entity,
    write_entity_mention,
    write_entity_merge,
    write_merge_invalidation,
)
from afair.substrate.payload import derive_searchable_text


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _seed_entity(conn, *, name: str, kind: str, n_mentions: int) -> str:
    """Create an entity under (name, kind) with n_mentions source events.

    write_entity is idempotent on (name, kind), so the mentions all attach
    to the one entity. Returns its id.
    """
    entity_id = ""
    for i in range(n_mentions):
        event = write_event(
            conn,
            origin="agent",
            kind="remember",
            payload={"content_type": "text", "text": f"{name} {kind} note {i}"},
        )
        entity = write_entity(
            conn,
            canonical_name=name,
            kind=kind,
            created_by="test",
            source_event_id=event.id,
            confidence=0.9,
        )
        entity_id = entity.id
        write_entity_mention(
            conn,
            entity_id=entity.id,
            event_id=event.id,
            event_hash=event.content_hash,
            surface_form=name,
            canonicalized_by="test",
            match_method="exact",
            confidence=0.9,
        )
    return entity_id


def _stub_judge(monkeypatch, *, same: bool, confidence: float, counter: dict | None = None):
    def _call(**kwargs):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        return LLMResult(
            data={"same_entity": same, "reason": "stub", "confidence": confidence},
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ed, "call_tool", _call)


def test_merges_same_name_cross_kind_when_judge_says_same(conn, monkeypatch) -> None:
    product = _seed_entity(conn, name="smoke.py", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="smoke.py", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.9)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["clusters_merged"] == 1
    assert stats["entities_merged"] == 1
    # Both resolve to the densest member (product, 3 mentions).
    assert resolve_canonical(conn, product) == product
    assert resolve_canonical(conn, project) == product


def test_keeps_separate_when_judge_says_different(conn, monkeypatch) -> None:
    counter: dict = {}
    org = _seed_entity(conn, name="Apple", kind="organization", n_mentions=2)
    concept = _seed_entity(conn, name="Apple", kind="concept", n_mentions=2)
    _stub_judge(monkeypatch, same=False, confidence=0.95, counter=counter)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert counter["n"] == 1  # judged once
    assert stats["clusters_merged"] == 0
    assert stats["skipped_not_same"] == 1
    assert resolve_canonical(conn, org) == org  # unchanged
    assert resolve_canonical(conn, concept) == concept


def test_does_not_merge_below_confidence_threshold(conn, monkeypatch) -> None:
    product = _seed_entity(conn, name="vague", kind="product", n_mentions=2)
    project = _seed_entity(conn, name="vague", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.5)  # below MERGE_CONFIDENCE_THRESHOLD

    stats = ed.EntityDeduplicator().run(conn, Settings())

    assert stats["clusters_merged"] == 0
    assert resolve_canonical(conn, product) == product
    assert resolve_canonical(conn, project) == project


def test_keep_separate_decision_skips_rejudge_until_cluster_grows(conn, monkeypatch) -> None:
    counter: dict = {}
    org_id = _seed_entity(conn, name="Apple", kind="organization", n_mentions=2)
    _seed_entity(conn, name="Apple", kind="concept", n_mentions=2)
    _stub_judge(monkeypatch, same=False, confidence=0.95, counter=counter)

    ed.EntityDeduplicator().run(conn, Settings())  # judges once → kept separate + marker
    stats2 = ed.EntityDeduplicator().run(conn, Settings())  # unchanged → skip, no LLM

    assert counter["n"] == 1
    assert stats2["skipped_recent_decision"] >= 1
    assert stats2["clusters_examined"] == 0

    # Grow the cluster with a genuinely new mention — warrants a fresh judgment.
    event = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": "Apple unique growth note"},
    )
    write_entity_mention(
        conn,
        entity_id=org_id,
        event_id=event.id,
        event_hash=event.content_hash,
        surface_form="Apple",
        canonicalized_by="test",
        match_method="exact",
        confidence=0.9,
    )
    stats3 = ed.EntityDeduplicator().run(conn, Settings())
    assert counter["n"] == 2  # re-judged
    assert stats3["clusters_examined"] == 1


def test_keep_separate_marker_is_invisible_to_recall(conn) -> None:
    # The marker carries no FTS-indexed key, so recall never surfaces it.
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = ?", (ed.DEDUP_DECISION_KIND,)
    ).fetchall()
    # (none yet in this fresh conn — assert the payload shape directly)
    import json

    sample = {
        "entity_key": "apple",
        "decision": "keep_separate",
        "mention_total": 4,
        "confidence": 0.9,
        "rationale": "company vs concept, different things",
        "produced_by": ed.DEDUP_PRODUCED_BY,
    }
    assert derive_searchable_text(sample) == ""
    assert rows == [] or all(derive_searchable_text(json.loads(r["payload"])) == "" for r in rows)


def _dedup_merge_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_merges WHERE merged_by = ?",
        (ed.DEDUP_PRODUCED_BY,),
    ).fetchone()
    return int(row["n"])


def test_dedup_defers_to_operator_after_revert(conn, monkeypatch) -> None:
    """Regression (ADR-0002 entrenchment): the Graphiti cycle.

    The dedup worker merged project→product; the operator rejected it
    (invalidation + counter-merge product→project). The operator merge and
    the invalidated dedup merge form a resolution cycle, so resolve_canonical
    yields >1 canonical and the already-collapsed guard alone does not fire.
    Pre-fix the worker re-judged and silently re-merged the pair every cycle,
    overriding the operator. Post-fix it defers entirely.
    """
    counter: dict = {}
    product = _seed_entity(conn, name="Graphiti", kind="product", n_mentions=3)
    project = _seed_entity(conn, name="Graphiti", kind="project", n_mentions=2)
    other = _seed_entity(conn, name="Graphiti", kind="other", n_mentions=1)

    # 1) The dedup worker's original merge: project → product.
    dedup_merge = write_entity_merge(
        conn,
        from_entity_id=project,
        into_entity_id=product,
        merged_by=ed.DEDUP_PRODUCED_BY,
        reason="same-name cross-kind dedup (project → product)",
        confidence=0.9,
    )
    # 2) The operator rejects it: invalidate the dedup merge and write the
    #    counter-merge product → project.
    assert write_merge_invalidation(
        conn,
        merge_id=dedup_merge.id,
        invalidated_by=DECIDED_BY_OPERATOR,
        reason="operator rejected the dedup merge",
    )
    write_entity_merge(
        conn,
        from_entity_id=product,
        into_entity_id=project,
        merged_by=DECIDED_BY_OPERATOR,
        reason="operator counter-merge",
        confidence=1.0,
    )

    # The cycle defeats the already-collapsed guard: >1 distinct canonical.
    canonicals = {resolve_canonical(conn, e) for e in (product, project, other)}
    assert len(canonicals) >= 2

    dedup_rows_before = _dedup_merge_count(conn)
    _stub_judge(monkeypatch, same=True, confidence=0.95, counter=counter)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    # 3) The worker must NOT re-merge over the operator's decision.
    assert _dedup_merge_count(conn) == dedup_rows_before
    assert stats["clusters_merged"] == 0
    assert stats["skipped_operator_governed"] >= 1
    assert counter.get("n", 0) == 0  # deferred before the LLM judge


def test_dedup_idempotent_no_duplicate_live_merge(conn, monkeypatch) -> None:
    """Guard 2: a member with a live merge out is never merged again.

    B (densest) already absorbed A via a live dedup merge; C is a fresh
    split. The cluster is still open (B vs C), so the worker runs — but it
    must only write C→B, not a duplicate A→B row.
    """
    b = _seed_entity(conn, name="VISION.md", kind="product", n_mentions=3)
    a = _seed_entity(conn, name="VISION.md", kind="project", n_mentions=1)
    c = _seed_entity(conn, name="VISION.md", kind="other", n_mentions=2)
    write_entity_merge(
        conn,
        from_entity_id=a,
        into_entity_id=b,
        merged_by=ed.DEDUP_PRODUCED_BY,
        reason="same-name cross-kind dedup (project → product)",
        confidence=0.9,
    )
    _stub_judge(monkeypatch, same=True, confidence=0.9)

    stats = ed.EntityDeduplicator().run(conn, Settings())

    # Only the fresh member merged; no duplicate row out of the already-merged one.
    assert stats["entities_merged"] == 1
    merges_from_a = conn.execute(
        "SELECT COUNT(*) AS n FROM entity_merges WHERE from_entity_id = ?", (a,)
    ).fetchone()["n"]
    assert merges_from_a == 1
    assert resolve_canonical(conn, c) == b
    assert resolve_canonical(conn, a) == b


def test_idempotent_skips_already_merged_cluster(conn, monkeypatch) -> None:
    counter: dict = {}
    _seed_entity(conn, name="dup", kind="product", n_mentions=2)
    _seed_entity(conn, name="dup", kind="project", n_mentions=1)
    _stub_judge(monkeypatch, same=True, confidence=0.9, counter=counter)

    ed.EntityDeduplicator().run(conn, Settings())  # merges
    stats2 = ed.EntityDeduplicator().run(conn, Settings())  # already merged → skip

    assert stats2["skipped_already_merged"] >= 1
    assert stats2["clusters_merged"] == 0
    assert counter["n"] == 1  # judge not called again on the merged cluster

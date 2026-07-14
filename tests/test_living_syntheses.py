"""Tests for automatic, revisable living syntheses."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from afair.agents import living_syntheses as ls
from afair.agents.binder import BINDER_PRODUCED_BY
from afair.agents.interpretation import write_interpretation
from afair.agents.invalidation import INVALIDATE_KIND, write_invalidation
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.entities import write_entity, write_entity_mention

if TYPE_CHECKING:
    from afair.substrate.events import Event


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    connection = open_db(vault)
    yield connection
    connection.close()


def _event(conn, text: str, *, parent_hashes: list[str] | None = None) -> Event:
    return write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": text},
        parent_hashes=parent_hashes,
    )


def _mention(conn, event: Event, name: str) -> str:
    entity = write_entity(
        conn,
        canonical_name=name,
        kind="concept",
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
    )
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
    return entity.id


def _stub_llm(monkeypatch, *, counter: dict[str, int] | None = None) -> None:
    def call(**_: Any) -> LLMResult:
        if counter is not None:
            counter["calls"] = counter.get("calls", 0) + 1
        return LLMResult(
            data={
                "title": "Project Atlas",
                "summary": "Atlas is moving from research into a tested product.",
                "key_points": [
                    {"point": "The work is active", "mode": "fact", "sources": [1, 2]},
                    {"point": "This point has no evidence", "mode": "fact", "sources": [99]},
                ],
                "open_questions": ["Which user segment should go first?"],
                "conflict_notes": [],
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ls, "call_tool", call)


def _syntheses(conn) -> list:
    return conn.execute(
        "SELECT * FROM events WHERE kind = ? ORDER BY created_at, id",
        (ls.LIVING_SYNTHESIS_KIND,),
    ).fetchall()


def _bind(conn, source: Event, target: Event, distance: float) -> None:
    write_interpretation(
        conn,
        event=source,
        version=1,
        produced_by=BINDER_PRODUCED_BY,
        extraction={
            "status": "success",
            "type": "bind",
            "links": [{"event_hash": target.content_hash, "distance": distance}],
        },
    )


def test_discovers_entity_cluster_without_a_user_category(conn, monkeypatch) -> None:
    sources = [_event(conn, f"Atlas note {index}") for index in range(3)]
    for source in sources:
        _mention(conn, source, "Atlas")
    _stub_llm(monkeypatch)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 1
    payload = json.loads(_syntheses(conn)[0]["payload"])
    assert payload["title"] == "Project Atlas"
    assert payload["signals"] == ["entity_recurrence"]
    assert set(payload["member_hashes"]) == {source.content_hash for source in sources}
    assert payload["citations"] == payload["member_hashes"]
    assert payload["key_points"][0]["citations"]
    assert len(payload["key_points"]) == 1
    assert payload["citation_coverage"] == 1.0
    assert "category" not in payload
    assert "type" not in payload


def test_unchanged_cluster_is_a_no_op(conn, monkeypatch) -> None:
    counter: dict[str, int] = {}
    for index in range(3):
        source = _event(conn, f"Atlas note {index}")
        _mention(conn, source, "Atlas")
    _stub_llm(monkeypatch, counter=counter)

    ls.LivingSynthesisWorker().run(conn, Settings())
    second = ls.LivingSynthesisWorker().run(conn, Settings())

    assert counter["calls"] == 1
    assert second["skipped_unchanged"] == 1
    assert len(_syntheses(conn)) == 1


def test_new_evidence_updates_same_cluster_and_supersedes_prior(conn, monkeypatch) -> None:
    for index in range(3):
        source = _event(conn, f"Atlas note {index}")
        _mention(conn, source, "Atlas")
    _stub_llm(monkeypatch)
    ls.LivingSynthesisWorker().run(conn, Settings())
    first = _syntheses(conn)[0]
    first_payload = json.loads(first["payload"])

    new_source = _event(conn, "Atlas has a fourth piece of evidence")
    _mention(conn, new_source, "Atlas")
    ls.LivingSynthesisWorker().run(conn, Settings())

    rows = _syntheses(conn)
    assert len(rows) == 2
    second_payload = json.loads(rows[1]["payload"])
    assert second_payload["cluster_id"] == first_payload["cluster_id"]
    assert second_payload["previous_synthesis_hashes"] == [first["content_hash"]]
    invalidation = conn.execute(
        """
        SELECT 1 FROM events
        WHERE kind = ? AND json_extract(payload, '$.target_hash') = ?
        """,
        (INVALIDATE_KIND, first["content_hash"]),
    ).fetchone()
    assert invalidation is not None


def test_strong_semantic_chain_forms_cluster_without_entities(conn, monkeypatch) -> None:
    first = _event(conn, "Reducing onboarding friction")
    second = _event(conn, "The setup flow has too many choices")
    third = _event(conn, "A one-click import would improve setup")
    _bind(conn, second, first, 0.12)
    _bind(conn, third, second, 0.14)
    _stub_llm(monkeypatch)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 1
    payload = json.loads(_syntheses(conn)[0]["payload"])
    assert payload["entity_ids"] == []
    assert payload["signals"] == ["semantic_proximity"]


def test_weak_one_way_semantic_links_do_not_form_cluster(conn, monkeypatch) -> None:
    first = _event(conn, "One")
    second = _event(conn, "Two")
    third = _event(conn, "Three")
    _bind(conn, second, first, 0.4)
    _bind(conn, third, second, 0.4)
    counter: dict[str, int] = {}
    _stub_llm(monkeypatch, counter=counter)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 0
    assert counter.get("calls", 0) == 0


def test_explicit_lineage_can_form_a_cluster(conn, monkeypatch) -> None:
    root = _event(conn, "Original decision")
    children = [
        _event(conn, f"Follow-up {index}", parent_hashes=[root.content_hash]) for index in range(2)
    ]
    _stub_llm(monkeypatch)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 1
    payload = json.loads(_syntheses(conn)[0]["payload"])
    assert payload["signals"] == ["explicit_lineage"]
    assert set(payload["member_hashes"]) == {
        root.content_hash,
        *(child.content_hash for child in children),
    }


def test_invalidated_evidence_is_excluded(conn, monkeypatch) -> None:
    sources = [_event(conn, f"Atlas note {index}") for index in range(3)]
    for source in sources:
        _mention(conn, source, "Atlas")
    write_invalidation(
        conn,
        target_hash=sources[0].content_hash,
        reason="outdated",
        origin="user",
    )
    counter: dict[str, int] = {}
    _stub_llm(monkeypatch, counter=counter)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 0
    assert counter.get("calls", 0) == 0


def test_existing_synthesis_retires_when_current_evidence_drops_below_threshold(
    conn, monkeypatch
) -> None:
    sources = [_event(conn, f"Atlas note {index}") for index in range(3)]
    for source in sources:
        _mention(conn, source, "Atlas")
    _stub_llm(monkeypatch)
    ls.LivingSynthesisWorker().run(conn, Settings())
    synthesis = _syntheses(conn)[0]

    write_invalidation(
        conn,
        target_hash=sources[0].content_hash,
        reason="not current",
        origin="user",
    )
    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["retired"] == 1
    retired = conn.execute(
        """
        SELECT 1 FROM events
        WHERE kind = ? AND json_extract(payload, '$.target_hash') = ?
        """,
        (INVALIDATE_KIND, synthesis["content_hash"]),
    ).fetchone()
    assert retired is not None


def test_unresolved_conflict_is_given_to_synthesizer_and_kept_as_cited_note(
    conn, monkeypatch
) -> None:
    sources = [_event(conn, f"Atlas claim {index}") for index in range(3)]
    for source in sources:
        _mention(conn, source, "Atlas")
    write_interpretation(
        conn,
        event=sources[0],
        version=1,
        produced_by=f"conflict_resolver:v0:{sources[1].content_hash}",
        extraction={
            "status": "success",
            "event_a_hash": sources[0].content_hash,
            "event_a_id": sources[0].id,
            "event_b_hash": sources[1].content_hash,
            "event_b_id": sources[1].id,
            "verdict": "conflicts",
            "reason": "The records give different current states.",
            "confidence": 0.9,
        },
    )
    captured: dict[str, str] = {}

    def call(**kwargs: Any) -> LLMResult:
        captured["user"] = kwargs["user"]
        return LLMResult(
            data={
                "title": "Atlas",
                "summary": "Atlas has an unresolved current-state conflict.",
                "key_points": [],
                "open_questions": [],
                "conflict_notes": [{"note": "Two current states disagree.", "sources": [1, 2]}],
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ls, "call_tool", call)
    ls.LivingSynthesisWorker().run(conn, Settings())

    payload = json.loads(_syntheses(conn)[0]["payload"])
    assert "unresolved_conflicts" in captured["user"]
    assert payload["conflict_notes"][0]["citations"]


def test_model_text_is_normalized_without_em_dashes(conn, monkeypatch) -> None:
    for index in range(3):
        source = _event(conn, f"Atlas note {index}")
        _mention(conn, source, "Atlas")

    def call(**_: Any) -> LLMResult:
        return LLMResult(
            data={
                "title": "Atlas — launch",
                "summary": "Research — then product.",
                "key_points": [{"point": "Prototype — ready", "mode": "fact", "sources": [1]}],
                "open_questions": [],
                "conflict_notes": [],
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ls, "call_tool", call)
    ls.LivingSynthesisWorker().run(conn, Settings())

    payload = json.loads(_syntheses(conn)[0]["payload"])
    assert "—" not in json.dumps(payload, ensure_ascii=False)


def test_mature_hub_entity_does_not_make_a_vault_wide_cluster(conn, monkeypatch) -> None:
    for index in range(15):
        source = _event(conn, f"Unrelated memory {index}")
        if index < 12:
            _mention(conn, source, "The user")
    counter: dict[str, int] = {}
    _stub_llm(monkeypatch, counter=counter)

    stats = ls.LivingSynthesisWorker().run(conn, Settings())

    assert stats["written"] == 0
    assert counter.get("calls", 0) == 0


def test_split_and_merge_lineage_is_explicit() -> None:
    prior = ls._Prior(
        event_hash="sha256:prior",
        cluster_id="cluster:old",
        member_hashes=frozenset({"a", "b", "c", "d"}),
        entity_ids=frozenset({"entity:atlas"}),
        created_at="2026-07-01T00:00:00+00:00",
    )
    left = ls._Candidate(
        member_hashes={"a", "b", "c"},
        entity_ids={"entity:atlas"},
    )
    right = ls._Candidate(
        member_hashes={"c", "d", "e"},
        entity_ids={"entity:atlas"},
    )

    ls._assign_lineage([left, right], [prior])

    assert left.cluster_id == "cluster:old"
    assert right.cluster_id != "cluster:old"
    assert right.ancestor_cluster_ids == ["cluster:old"]
    assert right.previous_synthesis_hashes == ["sha256:prior"]


def test_uncited_key_points_are_dropped_regardless_of_mode(conn) -> None:
    """Every key point must resolve to a source (ADR-0007 hardening). An uncited
    claim, even an inference/uncertain one, could be an injected instruction and
    must never reach recall or the mirror ungrounded."""
    events = [_event(conn, "Atlas note A"), _event(conn, "Atlas note B")]
    raw = [
        {"point": "Cited fact.", "mode": "fact", "sources": [1]},
        {"point": "Uncited inference.", "mode": "inference", "sources": []},
        {"point": "Uncited uncertain.", "mode": "uncertain"},
        {"point": "Cited inference.", "mode": "inference", "sources": [2]},
    ]
    resolved = ls._resolve_key_points(raw, events)
    assert {p["point"] for p in resolved} == {"Cited fact.", "Cited inference."}
    assert all(p["citations"] for p in resolved)


def test_skip_path_reconciles_crash_created_duplicate(conn, monkeypatch) -> None:
    """A crash between a write and its supersession can leave two live syntheses
    for one cluster. On the unchanged path the worker reconciles: keep the
    current synthesis, supersede the stale same-cluster duplicate."""
    sources = [_event(conn, f"Atlas note {index}") for index in range(3)]
    for source in sources:
        _mention(conn, source, "Atlas")
    _stub_llm(monkeypatch)

    ls.LivingSynthesisWorker().run(conn, Settings())
    priors = ls._live_priors(conn)
    assert len(priors) == 1
    cluster_id = priors[0].cluster_id
    members = list(priors[0].member_hashes)

    # Simulate the crash duplicate: a second live synthesis, same cluster and
    # members, never superseded.
    write_event(
        conn,
        origin="agent",
        kind=ls.LIVING_SYNTHESIS_KIND,
        payload={
            "cluster_id": cluster_id,
            "member_hashes": members,
            "citations": members,
            "title": "duplicate",
        },
    )
    assert len(ls._live_priors(conn)) == 2

    stats = ls.LivingSynthesisWorker().run(conn, Settings())
    assert stats["skipped_unchanged"] == 1
    assert stats["reconciled"] == 1
    assert len(ls._live_priors(conn)) == 1

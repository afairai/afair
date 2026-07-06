"""`asserted_by` remember field (W3).

An additive, optional ``remember`` field: "user" | "model" | omitted. It is
caller-supplied assertion metadata = content, so it lives IN the payload (in the
content hash). Its ONLY trust effect is advisory and lower-not-raise: a
self-reported "user" maps to USER_STATED, which is served but — by construction
at the sole auto-confirm consumer — buys NOTHING above agent-derived. These
tests are the tripwire: if a future change ever lets "user" privilege the gate,
the lower-not-raise assertions go red.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError

import afair.agents.entity_canonicalizer as ec
from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import AssertedBy, TextContent
from afair.settings import Settings
from afair.substrate import open_db, read_event_by_id, write_event
from afair.substrate.belief import (
    _MIN_AUTO_CONFIRM_CONFIDENCE,
    Entrenchment,
    assertion_entrenchment,
    auto_confirm,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)
    monkeypatch.setattr(ec, "_maybe_sleep", lambda _last: 0.0)


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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="local",
        vault_dir=tmp_path,
        cold_path_enabled=False,
    )


# ── param validation ────────────────────────────────────────────────────────


def test_asserted_by_accepts_user_model_and_none() -> None:
    adapter = TypeAdapter(AssertedBy | None)
    assert adapter.validate_python("user") == "user"
    assert adapter.validate_python("model") == "model"
    assert adapter.validate_python(None) is None


def test_asserted_by_rejects_other_values() -> None:
    adapter = TypeAdapter(AssertedBy | None)
    for bad in ("operator", "human", "USER", "", "assistant"):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


# ── persistence + hashing ───────────────────────────────────────────────────


def test_asserted_by_persisted_in_payload(ctx: ServerContext) -> None:
    res = handlers.remember(content=TextContent(type="text", text="fact"), asserted_by="user")
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert event.payload.get("asserted_by") == "user"


def test_omitted_asserted_by_absent_from_payload(ctx: ServerContext) -> None:
    res = handlers.remember(content=TextContent(type="text", text="fact"))
    event = read_event_by_id(ctx.db, res.event_id)
    assert event is not None
    assert "asserted_by" not in event.payload


def test_asserted_by_changes_content_hash(ctx: ServerContext) -> None:
    """In-hash by design: the same sentence asserted as user vs unmarked is a
    different assertion → a different event."""
    plain = handlers.remember(content=TextContent(type="text", text="same sentence"))
    user = handlers.remember(
        content=TextContent(type="text", text="same sentence"), asserted_by="user"
    )
    model = handlers.remember(
        content=TextContent(type="text", text="same sentence"), asserted_by="model"
    )
    hashes = {plain.content_hash, user.content_hash, model.content_hash}
    assert len(hashes) == 3  # all distinct


def test_same_text_and_assertion_dedups(ctx: ServerContext) -> None:
    first = handlers.remember(
        content=TextContent(type="text", text="dedupe me"), asserted_by="user"
    )
    second = handlers.remember(
        content=TextContent(type="text", text="dedupe me"), asserted_by="user"
    )
    assert second.deduplicated is True
    assert second.content_hash == first.content_hash


# ── serving ─────────────────────────────────────────────────────────────────


def test_asserted_by_served_by_id_and_in_summary(ctx: ServerContext) -> None:
    res = handlers.remember(
        content=TextContent(type="text", text="servetoken assertion"), asserted_by="model"
    )
    by_id = handlers.recall(by_id=res.event_id)
    assert by_id.hits[0].payload.get("asserted_by") == "model"
    # Also survives the truncated summary view (compact), like type_hint.
    compact = handlers.recall(query="servetoken", depth="shallow", verbosity="compact")
    assert compact.hits[0].payload.get("asserted_by") == "model"


# ── lower-not-raise: assertion_entrenchment mapping ─────────────────────────


def test_assertion_entrenchment_mapping() -> None:
    assert assertion_entrenchment("user") == Entrenchment.USER_STATED
    assert assertion_entrenchment("model") == Entrenchment.AGENT_DERIVED
    assert assertion_entrenchment(None) == Entrenchment.AGENT_DERIVED
    # Anything unexpected floors to agent-derived — never above.
    assert assertion_entrenchment("operator") == Entrenchment.AGENT_DERIVED
    # The helper can NEVER return anything above USER_STATED.
    assert assertion_entrenchment("user") <= Entrenchment.USER_STATED


# ── lower-not-raise: USER_STATED buys NOTHING at the auto_confirm gate ───────


def _auto(entrenchment: Entrenchment, *, confidence: float, predicate: str) -> bool:
    return auto_confirm(
        confidence=confidence,
        predicate=predicate,
        source_entrenchment=entrenchment,
        has_evidence=True,
    )


def test_user_stated_below_floor_is_not_auto_confirmed() -> None:
    """A self-reported user assertion does NOT rescue a below-floor edge."""
    assert _auto(Entrenchment.USER_STATED, confidence=0.10, predicate="runs") is False


def test_user_stated_vague_predicate_is_not_auto_confirmed() -> None:
    vague = "is tech person in circle of friends who works with"
    assert _auto(Entrenchment.USER_STATED, confidence=0.95, predicate=vague) is False


def test_user_stated_equals_agent_derived_everywhere() -> None:
    """The core tripwire: across the whole input matrix, USER_STATED and
    AGENT_DERIVED produce IDENTICAL auto_confirm outcomes. If this ever fails, a
    self-report has started to privilege the gate — a W3 invariant violation."""
    below = _MIN_AUTO_CONFIRM_CONFIDENCE - 0.01
    above = _MIN_AUTO_CONFIRM_CONFIDENCE + 0.01
    for confidence in (0.0, below, _MIN_AUTO_CONFIRM_CONFIDENCE, above, 1.0):
        for predicate in ("runs", "is design partner for", "is vaguely associated with lots of"):
            assert _auto(
                Entrenchment.USER_STATED, confidence=confidence, predicate=predicate
            ) == _auto(Entrenchment.AGENT_DERIVED, confidence=confidence, predicate=predicate)


# ── e2e: user-asserted edge serves the SAME trust as an unmarked edge ────────


def _seed_relation_event(
    ctx: ServerContext,
    *,
    text: str,
    entities: list[dict[str, str]],
    relations: list[dict[str, str]] | None = None,
    asserted_by: str | None = None,
) -> str:
    payload: dict[str, object] = {"content_type": "text", "text": text}
    if asserted_by is not None:
        payload["asserted_by"] = asserted_by
    event = write_event(ctx.db, origin="user", kind="remember", payload=payload)
    grounded = [{**r, "evidence": r.get("evidence", text)} for r in (relations or [])]
    write_interpretation(
        ctx.db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": text[:200],
            "entities": entities,
            "relations": grounded,
        },
    )
    return event.id


def test_user_asserted_edge_serves_same_trust_as_unmarked(
    ctx: ServerContext, settings: Settings
) -> None:
    # Strong pre-seeded endpoints so both edges clear the auto-confirm floor.
    _seed_relation_event(
        ctx, text="Sajinth introduced himself", entities=[{"name": "Sajinth", "type": "person"}]
    )
    _seed_relation_event(
        ctx, text="Athara launched", entities=[{"name": "Athara", "type": "organization"}]
    )
    EntityCanonicalizer().run(ctx.db, settings)

    both = [{"name": "Sajinth", "type": "person"}, {"name": "Athara", "type": "organization"}]
    rel = [{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}]
    _seed_relation_event(
        ctx, text="Sajinth runs Athara now", entities=both, relations=rel, asserted_by="user"
    )
    _seed_relation_event(ctx, text="Sajinth runs Athara today", entities=both, relations=rel)
    EntityCanonicalizer().run(ctx.db, settings)

    result = handlers.recall(query="Sajinth", depth="shallow", verbosity="standard")
    trusts = {
        edge["trust"]
        for h in result.hits
        if h.interpretation
        for edge in (h.interpretation.get("entity_edges") or [])
    }
    assert trusts, "expected at least one served edge"
    # The user-asserted edge and the unmarked edge serve the SAME trust.
    assert len(trusts) == 1


def test_entrenchment_lookup_is_called_with_source_claim(
    ctx: ServerContext, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-edge entrenchment lookup replaced the old constant (TODO
    discharged): the recall path calls assertion_entrenchment with the source
    event's asserted_by claim."""
    _seed_relation_event(
        ctx, text="Sajinth introduced himself", entities=[{"name": "Sajinth", "type": "person"}]
    )
    _seed_relation_event(
        ctx, text="Athara launched", entities=[{"name": "Athara", "type": "organization"}]
    )
    EntityCanonicalizer().run(ctx.db, settings)
    _seed_relation_event(
        ctx,
        text="Sajinth runs Athara",
        entities=[
            {"name": "Sajinth", "type": "person"},
            {"name": "Athara", "type": "organization"},
        ],
        relations=[{"subject": "Sajinth", "predicate": "runs", "object": "Athara"}],
        asserted_by="user",
    )
    EntityCanonicalizer().run(ctx.db, settings)

    seen: list[str | None] = []
    real = handlers.assertion_entrenchment

    def _spy(value: str | None) -> Entrenchment:
        seen.append(value)
        return real(value)

    monkeypatch.setattr(handlers, "assertion_entrenchment", _spy)
    handlers.recall(query="Sajinth", depth="shallow", verbosity="standard")
    assert "user" in seen  # the source event's claim reached the lookup

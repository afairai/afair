"""P1-2 §5.2.6: the recall size drift alarm.

Seeds worst-case hits (2 KB payload texts + verbose historical interpretations
written directly, ≥8 entities + edges each) and asserts the compact default
stays under a hard per-hit ceiling. The budget is deliberately generous (see the
canonical-id length math in the spec) — the value is the ALARM, not the target.
Also pins compact < standard < full on the same seed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from afair.agents.entity_canonicalizer import EntityCanonicalizer
from afair.agents.interpretation import write_interpretation
from afair.mcp import handlers
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.settings import Settings
from afair.substrate import open_db, write_event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("afair.mcp.handlers.schedule_extraction", lambda _id: None)
    import afair.agents.entity_canonicalizer as ec

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


def _seed_bloated(ctx: ServerContext, idx: int) -> None:
    # ~2 KB text carrying the shared FTS token 'aurora' — made unique per event
    # (a distinct idx word) so identical-content dedup doesn't collapse them.
    text = (f"aurora borealis event{idx} over the northern sky " * 50)[:2000]
    entities = [{"name": f"E{idx}_{k}", "type": "person"} for k in range(8)]
    relations = [
        {
            "subject": f"E{idx}_{k}",
            "predicate": "is connected to",
            "object": f"E{idx}_{k + 1}",
            "evidence": text[:40],
        }
        for k in range(7)
    ]
    event = write_event(
        ctx.db, origin="user", kind="remember", payload={"content_type": "text", "text": text}
    )
    # A deliberately verbose historical interpretation (pre-cap): compact must
    # shrink this at serve time (I3 — the stored row is untouched).
    write_interpretation(
        ctx.db,
        event=event,
        version=1,
        produced_by="extractor:anthropic/claude-haiku-4-5",
        extraction={
            "status": "success",
            "best_guess_kind": "fact",
            "summary": "S" * 2000,
            "salient_facts": [f"fact {i} " + "y" * 200 for i in range(12)],
            "entities": entities,
            "relations": relations,
            "language": "en",
            "confidence": 0.9,
            "source_attribution": "a long provenance string " * 5,
        },
    )
    # Worst case for compact: many caveat-bearing conflicts (8 seeded → the
    # compact cap serves the top COMPACT_MAX_CONFLICTS, each ~160-char reason +
    # two ids). Stored as conflict_resolver interpretation rows keyed by the
    # other side's hash (how read_conflicts_batch finds them).
    for j in range(8):
        other_hash = f"sha256:{idx:03d}{j:061d}"
        write_interpretation(
            ctx.db,
            event=event,
            version=1,
            produced_by=f"conflict_resolver:v0:{other_hash}",
            extraction={
                "status": "success",
                "verdict": "conflicts",
                "reason": "R" * 300,
                "confidence": 0.85,
                "event_a_hash": event.content_hash,
                "event_b_hash": other_hash,
                "event_b_id": f"ev{idx}_{j}",
            },
        )


def test_compact_recall_stays_within_size_budget(ctx: ServerContext, settings: Settings) -> None:
    for i in range(10):
        _seed_bloated(ctx, i)
    EntityCanonicalizer().run(ctx.db, settings)  # materialize canonical entities + edges

    result = handlers.recall(query="aurora", depth="shallow", limit=10)
    assert len(result.hits) == 10
    # Compact bounds every unbounded vector: conflicts are capped, not just
    # reason-truncated — so even a hit in tension with 8 others serves <= 5.
    from afair.mcp.handlers import COMPACT_MAX_CONFLICTS

    assert all(len(h.conflicts) <= COMPACT_MAX_CONFLICTS for h in result.hits)

    raw = result.model_dump_json(exclude_none=True)
    # Alarm = the capped worst case (5 conflicts + 5 entities + 5 edges + capped
    # text/summary ~3.3KB/hit) with headroom; it fires if a change inflates
    # compact past the known dense-hit ceiling. Typical hits are ~1-1.5KB.
    assert len(raw) < 40_000
    assert len(raw) / len(result.hits) < 3_800


def test_verbosity_sizes_are_ordered(ctx: ServerContext, settings: Settings) -> None:
    for i in range(10):
        _seed_bloated(ctx, i)
    EntityCanonicalizer().run(ctx.db, settings)

    compact = handlers.recall(query="aurora", depth="shallow", limit=10, verbosity="compact")
    standard = handlers.recall(query="aurora", depth="shallow", limit=10, verbosity="standard")
    full = handlers.recall(query="aurora", depth="shallow", limit=10, verbosity="full")
    c = len(compact.model_dump_json(exclude_none=True))
    s = len(standard.model_dump_json(exclude_none=True))
    f = len(full.model_dump_json(exclude_none=True))
    assert c < s < f

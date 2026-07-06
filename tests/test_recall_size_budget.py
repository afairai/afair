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


def test_compact_recall_stays_within_size_budget(ctx: ServerContext, settings: Settings) -> None:
    for i in range(10):
        _seed_bloated(ctx, i)
    EntityCanonicalizer().run(ctx.db, settings)  # materialize canonical entities + edges

    result = handlers.recall(query="aurora", depth="shallow", limit=10)
    assert len(result.hits) == 10
    raw = result.model_dump_json(exclude_none=True)
    assert len(raw) < 30_000  # ~3KB/hit hard ceiling incl. envelope
    assert len(raw) / len(result.hits) < 2_500


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

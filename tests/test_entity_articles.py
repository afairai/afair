"""Tests for the entity-article worker (living per-topic synthesis).

The LLM is mocked via monkeypatch on agents.entity_articles.call_tool, so
no network is touched. Substrate fixtures seed events + entities + mentions
the way the canonicalizer would have produced them.
"""

from __future__ import annotations

import json
import time

import pytest

from afair.agents import entity_articles as ea
from afair.agents.invalidation import INVALIDATE_KIND
from afair.agents.llm import LLMResult
from afair.settings import Settings
from afair.substrate import open_db, write_event
from afair.substrate.entities import write_entity, write_entity_mention


@pytest.fixture()
def conn(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    c = open_db(vault)
    yield c
    c.close()


def _seed_mention(conn, *, name: str, kind: str, text: str, surface: str | None = None) -> None:
    """One source event + one entity + one mention linking them."""
    event = write_event(
        conn,
        origin="agent",
        kind="remember",
        payload={"content_type": "text", "text": text},
    )
    entity = write_entity(
        conn,
        canonical_name=name,
        kind=kind,
        created_by="test",
        source_event_id=event.id,
        confidence=0.9,
    )
    write_entity_mention(
        conn,
        entity_id=entity.id,
        event_id=event.id,
        event_hash=event.content_hash,
        surface_form=surface or name,
        canonicalized_by="test",
        match_method="exact",
        confidence=0.9,
    )


def _stub_llm(monkeypatch, *, counter: dict | None = None):
    def _call(**kwargs):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        return LLMResult(
            data={
                "summary": "A synthesized article about the entity.",
                "aliases": ["alt name"],
                "key_facts": ["fact one", "fact two"],
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ea, "call_tool", _call)


def _articles(conn) -> list:
    return conn.execute(
        "SELECT * FROM events WHERE kind = ? ORDER BY created_at",
        (ea.ENTITY_ARTICLE_KIND,),
    ).fetchall()


def test_writes_article_for_entity_above_threshold(conn, monkeypatch) -> None:
    for i in range(3):
        _seed_mention(conn, name="MCP", kind="concept", text=f"MCP note {i}")
    _stub_llm(monkeypatch)

    stats = ea.EntityArticleWorker().run(conn, Settings())

    assert stats["written"] == 1
    rows = _articles(conn)
    assert len(rows) == 1
    import json

    payload = json.loads(rows[0]["payload"])
    assert payload["entity_key"] == "mcp"
    assert payload["canonical_name"] == "MCP"
    assert payload["mention_count"] == 3
    assert payload["text"] == "A synthesized article about the entity."
    assert payload["produced_by"] == ea.ENTITY_ARTICLE_PRODUCER


def test_skips_entity_below_threshold(conn, monkeypatch) -> None:
    counter: dict = {}
    for i in range(2):  # below MIN_MENTIONS_FOR_ARTICLE (3)
        _seed_mention(conn, name="Mem0", kind="product", text=f"Mem0 note {i}")
    _stub_llm(monkeypatch, counter=counter)

    stats = ea.EntityArticleWorker().run(conn, Settings())

    assert stats["written"] == 0
    assert counter.get("n", 0) == 0  # no LLM call for a sub-threshold entity
    assert _articles(conn) == []


def test_second_run_skips_unchanged_entity(conn, monkeypatch) -> None:
    counter: dict = {}
    for i in range(3):
        _seed_mention(conn, name="Graphiti", kind="product", text=f"Graphiti note {i}")
    _stub_llm(monkeypatch, counter=counter)

    ea.EntityArticleWorker().run(conn, Settings())
    stats2 = ea.EntityArticleWorker().run(conn, Settings())

    assert counter["n"] == 1  # LLM fired once, not on the unchanged second run
    assert stats2["skipped_unchanged"] == 1
    assert len(_articles(conn)) == 1


def test_resynthesizes_and_supersedes_after_new_mention(conn, monkeypatch) -> None:
    for i in range(3):
        _seed_mention(conn, name="Letta", kind="product", text=f"Letta note {i}")
    _stub_llm(monkeypatch)
    ea.EntityArticleWorker().run(conn, Settings())
    v1 = _articles(conn)
    assert len(v1) == 1

    time.sleep(0.01)  # ensure the new mention's timestamp is strictly later
    _seed_mention(conn, name="Letta", kind="product", text="Letta got a new fact")
    ea.EntityArticleWorker().run(conn, Settings())

    v2 = _articles(conn)
    assert len(v2) == 2  # a new article version was written

    invalidations = conn.execute(
        """
        SELECT * FROM events
        WHERE kind = ?
          AND json_extract(payload, '$.target_hash') = ?
        """,
        (INVALIDATE_KIND, v1[0]["content_hash"]),
    ).fetchall()
    assert len(invalidations) == 1  # the prior article was superseded


def _live_articles(conn) -> list:
    """Articles with no invalidation pointing at them."""
    return conn.execute(
        """
        SELECT e.* FROM events e
        WHERE e.kind = ?
          AND NOT EXISTS (
            SELECT 1 FROM events inv
            WHERE inv.kind = ?
              AND json_extract(inv.payload, '$.target_hash') = e.content_hash
          )
        ORDER BY e.created_at
        """,
        (ea.ENTITY_ARTICLE_KIND, INVALIDATE_KIND),
    ).fetchall()


def test_per_fact_inline_citations_resolve_to_source_hashes(conn, monkeypatch) -> None:
    """BUILD #3 step 2 — each key fact cites the record number(s) behind it,
    which resolve to real source-event content_hashes."""
    for i in range(3):
        _seed_mention(conn, name="Letta", kind="product", text=f"Letta fact number {i}")

    def structured(**_: object) -> LLMResult:
        # cite record #1 (the newest mention; mentions are ordered newest-first)
        return LLMResult(
            data={
                "summary": "Letta is a product.",
                "aliases": [],
                "key_facts": [
                    {"fact": "Letta is a product", "sources": [1]},
                    {"fact": "uncited claim", "sources": []},
                    {"fact": "ignores hallucinated source", "sources": [99]},
                ],
            },
            model="stub",
            raw="{}",
        )

    monkeypatch.setattr(ea, "call_tool", structured)
    ea.EntityArticleWorker().run(conn, Settings())

    payload = json.loads(_articles(conn)[0]["payload"])
    cited = payload["cited_facts"]
    assert [c["fact"] for c in cited] == [
        "Letta is a product",
        "uncited claim",
        "ignores hallucinated source",
    ]
    # first fact cites a real source event; the others cite nothing
    source_hashes = {
        r["content_hash"]
        for r in conn.execute("SELECT content_hash FROM events WHERE kind='remember'").fetchall()
    }
    assert len(cited[0]["citations"]) == 1
    assert cited[0]["citations"][0] in source_hashes
    assert cited[1]["citations"] == []
    assert cited[2]["citations"] == []  # [#99] is out of range → no citation
    # flat key_facts still derived for back-compat
    assert payload["key_facts"][0] == "Letta is a product"


def test_article_carries_source_citations(conn, monkeypatch) -> None:
    """BUILD #3 — a synthesized article cites the source events it drew from,
    so a recalled article is a *cited* answer (provenance back to records)."""
    for i in range(3):
        _seed_mention(conn, name="Letta", kind="product", text=f"Letta fact number {i}")
    _stub_llm(monkeypatch)
    ea.EntityArticleWorker().run(conn, Settings())

    article = _articles(conn)[0]
    payload = json.loads(article["payload"])
    citations = payload.get("citations")
    assert citations, "article must carry source citations"

    # Every citation is the content_hash of a real source (remember) event.
    source_hashes = {
        r["content_hash"]
        for r in conn.execute("SELECT content_hash FROM events WHERE kind = 'remember'").fetchall()
    }
    assert set(citations) <= source_hashes
    assert len(citations) == 3  # the three seeded source events


def test_supersession_heals_a_crash_orphaned_article(conn, monkeypatch) -> None:
    """Race M1 — if a past cycle crashed between writing a new article and
    invalidating the prior, the orphan would otherwise live forever. The next
    re-synthesis must invalidate ALL prior-live articles, not just the newest,
    so the state self-heals to exactly one live article.
    """
    for i in range(3):
        _seed_mention(conn, name="Letta", kind="product", text=f"Letta note {i}")
    _stub_llm(monkeypatch)
    ea.EntityArticleWorker().run(conn, Settings())

    # Simulate the crash aftermath: a SECOND article exists for the same
    # entity_key with NO invalidation pointing at it (the invalidate write
    # never landed). Two live articles — the bug state.
    orphan_v1 = _articles(conn)[0]
    payload = dict(json.loads(orphan_v1["payload"]))
    payload["text"] = "an orphaned second article version"
    write_event(
        conn,
        origin="agent",
        kind=ea.ENTITY_ARTICLE_KIND,
        payload=payload,
        parent_hashes=payload.get("entity_ids"),
    )
    assert len(_live_articles(conn)) == 2  # two live articles — the orphan bug

    # New mention triggers re-synthesis → supersede ALL prior-live.
    time.sleep(0.01)
    _seed_mention(conn, name="Letta", kind="product", text="Letta got a new fact")
    ea.EntityArticleWorker().run(conn, Settings())

    # Self-healed: exactly one live article (the freshly synthesized one).
    live = _live_articles(conn)
    assert len(live) == 1


def test_coerce_cleans_model_mangled_list_fields() -> None:
    # Clean list passes through.
    assert ea._coerce_to_string_list(["a", "b"]) == ["a", "b"]
    # Single string → one element.
    assert ea._coerce_to_string_list("solo") == ["solo"]
    # Each element a stringified JSON array (observed from Haiku) → flattened.
    assert ea._coerce_to_string_list(['["fact a"]', '["fact b"]']) == ["fact a", "fact b"]
    # XML-ish list markup → tags stripped, pure-tag artifact dropped.
    assert ea._coerce_to_string_list(["<item>foo</item>", "</key_facts>", "<item>bar</item>"]) == [
        "foo",
        "bar",
    ]
    # Non-string / empty inputs.
    assert ea._coerce_to_string_list(None) == []
    assert ea._coerce_to_string_list(["", "   "]) == []


def test_groups_same_name_across_kinds_into_one_article(conn, monkeypatch) -> None:
    # Same canonical_name under two kinds — the canonicalizer does not merge
    # across kinds, but the article worker groups by name.
    _seed_mention(conn, name="smoke.py", kind="product", text="smoke product a")
    _seed_mention(conn, name="smoke.py", kind="product", text="smoke product b")
    _seed_mention(conn, name="smoke.py", kind="project", text="smoke project c")
    _stub_llm(monkeypatch)

    stats = ea.EntityArticleWorker().run(conn, Settings())

    assert stats["written"] == 1  # ONE article, not two
    import json

    payload = json.loads(_articles(conn)[0]["payload"])
    assert payload["entity_key"] == "smoke.py"
    assert payload["entity_kind"] == "mixed"
    assert payload["mention_count"] == 3
    assert len(payload["entity_ids"]) == 2  # both kind-variants aggregated

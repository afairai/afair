"""Substrate tests — Invariants I2 (append-only) and I3 (forever readable).

Covers: canonical JSON determinism, content-hash determinism, idempotent
dedup, inline-vs-spill threshold, object-store write-if-absent, FTS5
search, append-only enforcement at the DB level, and schema init
idempotency.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from neverforget.substrate import (
    Event,
    build_binary_payload,
    build_text_payload,
    canonical_json,
    content_hash,
    iter_events,
    object_path,
    open_db,
    read_event_by_hash,
    read_event_by_id,
    read_object,
    search_fts,
    write_event,
    write_object,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    """Fresh substrate in a tmp vault — closed after the test."""
    db = open_db(tmp_path)
    try:
        yield db, tmp_path
    finally:
        db.close()


# ── canonical JSON ──────────────────────────────────────────────────────────


def test_canonical_json_is_deterministic() -> None:
    a = canonical_json({"b": 2, "a": 1, "nested": {"y": 1, "x": 2}})
    b = canonical_json({"a": 1, "nested": {"x": 2, "y": 1}, "b": 2})
    assert a == b == '{"a":1,"b":2,"nested":{"x":2,"y":1}}'


def test_canonical_json_preserves_utf8() -> None:
    s = canonical_json({"name": "Ångström", "emoji": "🧠"})
    assert "Ångström" in s
    assert "🧠" in s


# ── content hashing ─────────────────────────────────────────────────────────


def test_content_hash_is_deterministic_and_prefixed() -> None:
    h1 = content_hash(kind="remember", origin="user", payload={"x": 1}, parent_hashes=None)
    h2 = content_hash(kind="remember", origin="user", payload={"x": 1}, parent_hashes=None)
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64


def test_content_hash_parent_order_does_not_matter() -> None:
    h1 = content_hash(kind="remember", origin="user", payload={}, parent_hashes=["a", "b"])
    h2 = content_hash(kind="remember", origin="user", payload={}, parent_hashes=["b", "a"])
    assert h1 == h2


def test_content_hash_differs_on_kind() -> None:
    h1 = content_hash(kind="remember", origin="u", payload={"a": 1}, parent_hashes=None)
    h2 = content_hash(kind="observe", origin="u", payload={"a": 1}, parent_hashes=None)
    assert h1 != h2


# ── object store ────────────────────────────────────────────────────────────


def test_object_store_write_is_idempotent(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    data = b"hello binary world"
    h1 = write_object(vault_dir, data)
    h2 = write_object(vault_dir, data)
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert read_object(vault_dir, h1) == data


def test_object_path_uses_2char_sharding(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    h = "sha256:" + "ab" + "cd" + "0" * 60  # 64 hex chars
    p = object_path(vault_dir, h)
    assert p.parent.name == "ab"
    assert p.name == "cd" + "0" * 60
    assert p.parent.parent.name == "objects"


def test_object_path_rejects_malformed_hash(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    with pytest.raises(ValueError, match="sha256:"):
        object_path(vault_dir, "deadbeef")
    with pytest.raises(ValueError, match="64 chars"):
        object_path(vault_dir, "sha256:short")


# ── payload builders (inline vs spill) ──────────────────────────────────────


def test_text_payload_inlines_when_small(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    p = build_text_payload(
        text="x" * 100,
        context="test",
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    assert p["content_type"] == "text"
    assert p["text"] == "x" * 100
    assert "blob_hash" not in p


def test_text_payload_spills_when_large(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    p = build_text_payload(
        text="x" * 200_000,
        context="test",
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    assert p["content_type"] == "text-large"
    assert "text" not in p
    assert p["blob_hash"].startswith("sha256:")
    assert p["size_bytes"] == 200_000
    # Blob is reachable from disk by its hash
    assert read_object(vault_dir, p["blob_hash"]) == b"x" * 200_000


def test_binary_payload_always_spills(vault: tuple[sqlite3.Connection, Path]) -> None:
    _, vault_dir = vault
    tiny = b"\x89PNG\r\n\x1a\n"  # 8 bytes — still spills
    p = build_binary_payload(
        data=tiny,
        mime="image/png",
        filename_hint="screenshot.png",
        context="bug hunt",
        vault_dir=vault_dir,
    )
    assert p["content_type"] == "binary"
    assert p["mime"] == "image/png"
    assert p["blob_hash"].startswith("sha256:")
    assert read_object(vault_dir, p["blob_hash"]) == tiny


# ── write / read round-trip ─────────────────────────────────────────────────


def test_write_read_round_trip(vault: tuple[sqlite3.Connection, Path]) -> None:
    db, vault_dir = vault
    payload = build_text_payload(
        text="hello world",
        context="test",
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    e = write_event(db, origin="user", kind="remember", payload=payload)
    assert isinstance(e, Event)
    assert e.kind == "remember"
    assert e.schema_version == 1

    re_id = read_event_by_id(db, e.id)
    assert re_id is not None
    assert re_id.content_hash == e.content_hash
    assert re_id.payload["text"] == "hello world"

    re_hash = read_event_by_hash(db, e.content_hash)
    assert re_hash is not None
    assert re_hash.id == e.id


def test_write_is_idempotent_on_content_hash(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault
    payload = build_text_payload(
        text="same content",
        context=None,
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    e1 = write_event(db, origin="user", kind="remember", payload=payload)
    e2 = write_event(db, origin="user", kind="remember", payload=payload)
    # Same logical event → dedup'd
    assert e1.id == e2.id
    assert e1.content_hash == e2.content_hash
    # Exactly one row in events
    count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert count == 1


def test_different_payloads_get_different_hashes(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault
    p1 = build_text_payload(
        text="a",
        context=None,
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    p2 = build_text_payload(
        text="b",
        context=None,
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    e1 = write_event(db, origin="user", kind="remember", payload=p1)
    e2 = write_event(db, origin="user", kind="remember", payload=p2)
    assert e1.content_hash != e2.content_hash
    assert e1.id != e2.id


# ── append-only enforcement ─────────────────────────────────────────────────


def test_substrate_is_append_only_no_update(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault
    payload = build_text_payload(
        text="immutable",
        context=None,
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    e = write_event(db, origin="user", kind="remember", payload=payload)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE events SET origin = 'attacker' WHERE id = ?", (e.id,))


def test_substrate_is_append_only_no_delete(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault
    payload = build_text_payload(
        text="immutable",
        context=None,
        type_hint=None,
        vault_dir=vault_dir,
        inline_text_max_bytes=64 * 1024,
    )
    e = write_event(db, origin="user", kind="remember", payload=payload)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM events WHERE id = ?", (e.id,))


# ── iteration & filtering ───────────────────────────────────────────────────


def test_iter_events_filters_and_orders(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault

    def text_payload(t: str) -> dict[str, object]:
        return build_text_payload(
            text=t,
            context=None,
            type_hint=None,
            vault_dir=vault_dir,
            inline_text_max_bytes=64 * 1024,
        )

    write_event(db, origin="user", kind="remember", payload=text_payload("a"))
    write_event(db, origin="agent:claude-code", kind="observe", payload=text_payload("b"))
    write_event(db, origin="user", kind="remember", payload=text_payload("c"))

    user_remembers = list(iter_events(db, kind="remember", origin="user"))
    assert len(user_remembers) == 2
    # Default order is desc — "c" was inserted last
    assert user_remembers[0].payload["text"] == "c"
    assert user_remembers[1].payload["text"] == "a"

    asc = list(iter_events(db, order="asc"))
    assert [e.payload["text"] for e in asc] == ["a", "b", "c"]


def test_iter_events_limit(vault: tuple[sqlite3.Connection, Path]) -> None:
    db, vault_dir = vault
    for i in range(5):
        write_event(
            db,
            origin="user",
            kind="remember",
            payload=build_text_payload(
                text=f"event {i}",
                context=None,
                type_hint=None,
                vault_dir=vault_dir,
                inline_text_max_bytes=64 * 1024,
            ),
        )
    got = list(iter_events(db, limit=2))
    assert len(got) == 2


# ── FTS search ──────────────────────────────────────────────────────────────


def test_fts_search_finds_inline_text(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    db, vault_dir = vault
    write_event(
        db,
        origin="user",
        kind="remember",
        payload=build_text_payload(
            text="Sajinth proposed a new roadmap focused on memory",
            context="email thread",
            type_hint=None,
            vault_dir=vault_dir,
            inline_text_max_bytes=64 * 1024,
        ),
    )
    write_event(
        db,
        origin="user",
        kind="remember",
        payload=build_text_payload(
            text="unrelated lunch plans",
            context=None,
            type_hint=None,
            vault_dir=vault_dir,
            inline_text_max_bytes=64 * 1024,
        ),
    )

    results = search_fts(db, "Sajinth")
    assert len(results) == 1
    assert "Sajinth" in results[0].payload["text"]


def test_fts_search_finds_context_terms(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    """Context strings are indexed so 'recall the email about X' works."""
    db, vault_dir = vault
    write_event(
        db,
        origin="user",
        kind="remember",
        payload=build_text_payload(
            text="Body content here",
            context="email from Sajinth",
            type_hint=None,
            vault_dir=vault_dir,
            inline_text_max_bytes=64 * 1024,
        ),
    )
    results = search_fts(db, "email")
    assert len(results) == 1


def test_fts_search_partial_token_match_returns_hits(
    vault: tuple[sqlite3.Connection, Path],
) -> None:
    """Regression test for a real Claude.ai-observed quality issue (2026-05-25):
    a multi-token natural-language query should still find documents that
    contain only SOME of the tokens. Previously the helper joined tokens
    with implicit AND so a 4-token query against a document containing
    only 2 of those tokens returned 0 hits — surprising the AI and the
    user. Now OR + rank: the document still appears, ranked by coverage.
    """
    db, vault_dir = vault
    # Document contains "cross-vendor" + "verification" but not "I5" or
    # "neutrality". Should still match a query that mentions all four.
    write_event(
        db,
        origin="user",
        kind="remember",
        payload=build_text_payload(
            text="first successful cross-vendor verification, 2026-05-25",
            context=None,
            type_hint="milestone",
            vault_dir=vault_dir,
            inline_text_max_bytes=64 * 1024,
        ),
    )
    results = search_fts(db, "cross-vendor verification I5 vendor neutrality")
    assert len(results) >= 1, "long natural-language query should not return empty"
    assert "cross-vendor" in results[0].payload["text"]


# ── init idempotency ────────────────────────────────────────────────────────


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    """open_db twice on the same vault must not error or duplicate state."""
    db1 = open_db(tmp_path)
    db1.close()
    db2 = open_db(tmp_path)
    try:
        # All schema objects still present after second init
        tables = {
            r["name"]
            for r in db2.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
        }
        assert "events" in tables
        assert "events_no_update" in tables
        assert "events_no_delete" in tables
        assert "interpretations" in tables
    finally:
        db2.close()


def test_objects_directory_created(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    try:
        assert (tmp_path / "objects").is_dir()
    finally:
        db.close()

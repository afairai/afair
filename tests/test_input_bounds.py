"""Input-validation bounds — Security audit findings I3, I4, I5.

Closes:
  - I3 — ``parent_hashes`` and ``invalidates`` lists on ``remember`` are
         unbounded (a million-entry list can DOS the per-target loop).
  - I4 — ``context`` and ``type_hint`` strings (from ``remember``) are
         unbounded; a 10 MB context blob inflates the FTS index.
  - I5 — ``observe`` accepts arbitrary ``extras`` without size or
         depth bound — deeply nested or huge property dicts DOS the
         serializer.

The MCP v1 surface (I1) is otherwise stable; these tighten by adding
bounds the surface never documented as unbounded, which is an additive
change (no compliant caller depended on infinite-length lists).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from afair.mcp import handlers, schemas
from afair.mcp.context import ServerContext, clear_context, set_context
from afair.mcp.schemas import BinaryContent, ObserveEvent, TextContent
from afair.substrate import open_db, read_event_by_hash

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _disable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "afair.mcp.handlers.schedule_extraction",
        lambda _event_id: None,
    )


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[ServerContext]:
    db = open_db(tmp_path)
    sc = ServerContext(
        db=db,
        vault_dir=tmp_path,
        inline_text_max_bytes=64 * 1024,
        semantic_recall_enabled=False,
    )
    set_context(sc)
    try:
        yield sc
    finally:
        db.close()
        clear_context()


# ── I3 — parent_hashes + invalidates list-length bound ─────────────────────


def test_parent_hashes_above_cap_rejected() -> None:
    too_many = ["sha256:" + "0" * 64] * (schemas.MAX_PARENT_HASHES_PER_CALL + 1)
    with pytest.raises(ValueError, match="parent_hashes must be"):
        handlers.remember(
            content=TextContent(type="text", text="ok"),
            parent_hashes=too_many,
        )


def test_invalidates_above_cap_rejected() -> None:
    too_many = ["sha256:" + "0" * 64] * (schemas.MAX_INVALIDATES_PER_CALL + 1)
    with pytest.raises(ValueError, match="invalidates must be"):
        handlers.remember(
            content=TextContent(type="text", text="ok"),
            invalidates=too_many,
        )


# ── I4 — context + type_hint string-length bound ───────────────────────────


def test_context_above_cap_rejected() -> None:
    huge = "x" * (schemas.MAX_CONTEXT_CHARS + 1)
    with pytest.raises(ValueError, match="context must be"):
        handlers.remember(
            content=TextContent(type="text", text="ok"),
            context=huge,
        )


def test_type_hint_above_cap_rejected() -> None:
    huge = "x" * (schemas.MAX_TYPE_HINT_CHARS + 1)
    with pytest.raises(ValueError, match="type_hint must be"):
        handlers.remember(
            content=TextContent(type="text", text="ok"),
            type_hint=huge,
        )


def test_legitimate_context_passes(ctx: ServerContext) -> None:
    # Reasonable context (~100 chars) is fine.
    handlers.remember(
        content=TextContent(type="text", text="ok"),
        context="this is a perfectly reasonable bit of context",
    )


# ── I4 — BinaryContent mime + filename_hint bound by Pydantic ──────────────


def test_binary_mime_above_cap_rejected_by_pydantic() -> None:
    with pytest.raises(ValueError, match="mime"):
        BinaryContent(
            type="binary",
            data_b64="aGVsbG8=",
            mime="x" * (schemas.MAX_MIME_CHARS + 1),
        )


def test_binary_filename_hint_above_cap_rejected_by_pydantic() -> None:
    with pytest.raises(ValueError, match="filename_hint"):
        BinaryContent(
            type="binary",
            data_b64="aGVsbG8=",
            mime="text/plain",
            filename_hint="A" * (schemas.MAX_FILENAME_HINT_CHARS + 1),
        )


# ── I5 — observe.extras size + nesting bound (write-first: truncate, not reject)


def test_observe_extras_huge_serialized_truncated_not_rejected() -> None:
    """CONTRACT CHANGE (P0-2): an over-64KB extras value is no longer rejected
    at the signature layer (which silently dropped the whole observation). It
    is truncated to MAX_OBSERVE_EXTRA_VALUE_CHARS (largest-first) and marked
    ``extras_truncated`` — a strict superset of the old accept set (I1)."""
    e = ObserveEvent(action="x", junk="z" * 70_000)
    extra = e.__pydantic_extra__
    assert extra is not None
    assert extra["extras_truncated"] is True
    assert len(extra["junk"]) == schemas.MAX_OBSERVE_EXTRA_VALUE_CHARS
    assert extra["junk"] == "z" * schemas.MAX_OBSERVE_EXTRA_VALUE_CHARS


def test_observe_extras_deeply_nested_flattened_not_rejected() -> None:
    """CONTRACT CHANGE (P0-2): a >200-container extras structure is flattened
    to a text rendering rather than rejected. The observation persists."""
    nested: dict[str, object] = {"action": "x"}
    cursor: dict[str, object] = nested
    for _ in range(250):
        cursor["inner"] = {}
        cursor = cursor["inner"]  # type: ignore[assignment]
    e = ObserveEvent(**nested)
    extra = e.__pydantic_extra__
    assert extra is not None
    assert extra["extras_truncated"] is True
    assert "extras_text" in extra
    # The pathological nested structure is gone — flattened, not stored verbatim.
    assert "inner" not in extra


def test_observe_extreme_nesting_never_raises() -> None:
    """A nesting bomb deep enough to RecursionError json.dumps must NOT raise —
    container counting runs iteratively on the parsed structure BEFORE any
    serialize, so the flatten path is reached without hitting the stack limit."""
    nested: dict[str, object] = {"action": "x"}
    cursor: dict[str, object] = nested
    for _ in range(5000):
        cursor["inner"] = {}
        cursor = cursor["inner"]  # type: ignore[assignment]
    e = ObserveEvent(**nested)  # must not raise
    extra = e.__pydantic_extra__
    assert extra is not None
    assert extra["extras_truncated"] is True


def test_observe_code_diff_many_braces_accepted() -> None:
    """A flat extras value that happens to contain hundreds of literal braces
    (a code diff pasted as a single string) must be accepted verbatim — the old
    serialized-brace-count heuristic wrongly rejected it as a nesting bomb. It
    is one container (the extras dict); the braces live INSIDE a string."""
    diff = "\n".join("if (x) { doThing(); }" for _ in range(300))
    e = ObserveEvent(action="applied_patch", diff=diff)
    extra = e.__pydantic_extra__
    assert extra is not None
    assert extra["diff"] == diff
    assert "extras_truncated" not in extra


def test_observe_nonstring_subject_coerced_not_rejected() -> None:
    """A non-string ``subject``/``result`` (e.g. an int, or a dict a client
    packed into the field) is coerced to a string rather than rejecting the
    whole write at pydantic's ``str | None`` constraint."""
    e = ObserveEvent.model_validate({"action": "ok", "subject": 123, "result": {"code": 200}})
    assert e.subject == "123"
    assert e.result == '{"code":200}'


def test_ensure_observe_event_never_raises_on_hostile_inputs() -> None:
    """The write-first contract: ensure_observe_event NEVER raises on shape.
    Every hostile input returns a persistable ObserveEvent."""
    deep: dict[str, object] = {"action": "x"}
    cursor: dict[str, object] = deep
    for _ in range(3000):
        cursor["inner"] = {}
        cursor = cursor["inner"]  # type: ignore[assignment]

    hostile: list[object] = [
        {"action": "ok", "junk": "z" * 80_000},
        deep,
        {"action": 123, "subject": {"nested": ["a", "b"]}},
        [1, 2, 3],
        "just a bare string",
        123,
        {"action": "ok", "result": {"deeply": {"nested": {"object": True}}}},
    ]
    for item in hostile:
        e = schemas.ensure_observe_event(item)
        assert isinstance(e, ObserveEvent)
        assert isinstance(e.action, str) and e.action


def test_observe_extras_within_bounds_accepted() -> None:
    """Realistic extras pass: action + a few metadata fields."""
    e = ObserveEvent(
        action="edited_file",
        subject="afair/agents/extractor.py",
        result="ok",
        # A few extras — typical of what AI clients send.
        line_count=120,
        diff_kind="modified",
        tags=["python", "extractor"],
    )
    assert e.action == "edited_file"


def test_observe_action_long_coerced_and_preserved() -> None:
    """Write-first intake (v0.1.3): an over-long ``action`` is never rejected
    at the pydantic signature layer. It is truncated to the cap and the full
    original preserved under ``action_full`` so nothing the caller sent is lost.
    """
    original = "x" * (schemas.MAX_OBSERVE_ACTION_CHARS + 1)
    e = ObserveEvent(action=original)
    assert e.action == "x" * schemas.MAX_OBSERVE_ACTION_CHARS
    assert e.action_full == original  # type: ignore[attr-defined]


def test_observe_subject_and_result_long_coerced_and_preserved() -> None:
    """Same write-first coercion for the ``subject`` and ``result`` fields."""
    long_subject = "s" * (schemas.MAX_OBSERVE_SUBJECT_CHARS + 5)
    long_result = "r" * (schemas.MAX_OBSERVE_RESULT_CHARS + 5)
    e = ObserveEvent(action="ok", subject=long_subject, result=long_result)
    assert e.subject == "s" * schemas.MAX_OBSERVE_SUBJECT_CHARS
    assert e.result == "r" * schemas.MAX_OBSERVE_RESULT_CHARS
    assert e.subject_full == long_subject  # type: ignore[attr-defined]
    assert e.result_full == long_result  # type: ignore[attr-defined]


def test_observe_long_action_json_blob_persists_to_vault(ctx: ServerContext) -> None:
    """Regression for AFAIR-H / AFAIR-3: a live client stuffed a whole JSON blob
    (well over 200 chars) into ``action``. Pydantic's ``max_length`` used to
    reject it at the FastMCP signature layer BEFORE the tolerant intake
    validator ran, so the observation was silently dropped ("...NOT persisted to
    vault"). It must now persist: truncated ``action`` in the vault plus the
    full original under ``action_full``.
    """
    blob = json.dumps(
        {
            "tool": "edit_file",
            "path": "afair/agents/extractor.py",
            "note": "long structured payload the client packed into action " + "z" * 300,
        }
    )
    assert len(blob) > schemas.MAX_OBSERVE_ACTION_CHARS

    r = handlers.observe(event=ObserveEvent.model_validate({"action": blob}))
    assert r.ok is True

    stored = read_event_by_hash(ctx.db, r.content_hash)
    assert stored is not None
    payload = stored.payload
    assert payload["action"] == blob[: schemas.MAX_OBSERVE_ACTION_CHARS]
    assert payload["action_full"] == blob


def test_observe_overlong_result_persists_to_vault(ctx: ServerContext) -> None:
    """An 80KB ``result`` (over the 2000-char cap) must persist end-to-end:
    truncated ``result`` in the vault plus the full original under
    ``result_full``. The observation is never dropped."""
    long_result = "r" * 80_000
    r = handlers.observe(event=ObserveEvent(action="ran_query", result=long_result))
    assert r.ok is True

    stored = read_event_by_hash(ctx.db, r.content_hash)
    assert stored is not None
    payload = stored.payload
    assert payload["result"] == long_result[: schemas.MAX_OBSERVE_RESULT_CHARS]
    assert payload["result_full"] == long_result


# ── existing behavior preserved (no regression) ────────────────────────────


def test_remember_text_at_exact_cap_works(ctx: ServerContext) -> None:
    """Boundary test — exactly MAX_REMEMBER_BYTES still passes the handler."""
    # At the cap (handler raises STRICTLY above)
    text = "x" * schemas.MAX_REMEMBER_BYTES
    res = handlers.remember(content=TextContent(type="text", text=text))
    assert res.ok is True


def test_remember_text_above_cap_raises_typed_error(ctx: ServerContext) -> None:
    text = "x" * (schemas.MAX_REMEMBER_BYTES + 1)
    with pytest.raises(handlers.ContentTooLargeError):
        handlers.remember(content=TextContent(type="text", text=text))


def test_observe_caller_supplied_action_full_is_preserved() -> None:
    """When action is over-long AND the caller already sent action_full, keep
    theirs under action_full_client so nothing the caller sent is lost
    (Fable review nit #3)."""
    original = "x" * (schemas.MAX_OBSERVE_ACTION_CHARS + 50)
    e = ObserveEvent.model_validate({"action": original, "action_full": "caller-provided"})
    assert e.action_full == original  # type: ignore[attr-defined]
    assert e.action_full_client == "caller-provided"  # type: ignore[attr-defined]

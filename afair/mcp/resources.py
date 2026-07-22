"""MCP resources — auto-fetched session context for AI clients.

The three MCP tools (remember, recall, observe) are pull-based: the
client decides when to call them. Resources are different — many MCP
clients (Claude.ai, claude-code) call ``resources/list`` and
``resources/read`` automatically during the session-init handshake
and bake the content into the prompt before the user types anything.

This module exposes one such resource: ``afair://session-start``.
When the client connects, it pulls a compact summary of the current
vault state: the cognitive mode (CEN vs DMN), the most-salient
recent events, and any open threads carried forward from
consolidator runs.

Effect: every new conversation starts already aware of "what's been
on the user's mind." The AI doesn't have to recall before answering
the first question — the context is already loaded. Recall remains
the right tool for follow-up specifics, but the cold-start
"do you remember anything about me?" baseline is gone.

Caching
-------
Per-process LRU with a short TTL (60 s). Each session-start fetch
costs four substrate queries (mode, salience, consolidations, vault
stats); without caching, every MCP client connect would re-execute
all four. With caching, the same connect-then-connect-again pattern
(common in Claude Code which reconnects on autoreload) reads cached
bytes.

The cache is keyed on the latest event id — same trick as the
recent-canonical-context cache in handlers.py. Any new remember or
observe invalidates it implicitly because the latest event id has
moved.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import sqlite3


log = structlog.get_logger(__name__)


SESSION_START_URI = "afair://session-start"
SESSION_START_NAME = "Session start context"
SESSION_START_DESCRIPTION = (
    "Snapshot of the user's current vault state. Read at session "
    "start so the AI knows what's been on the user's mind without "
    "having to recall first."
)

# Cache TTL — 60 s is a comfortable balance between freshness (a
# remember that just happened shows up on next connect) and the cost
# of cold-computing the snapshot on every connect.
_CACHE_TTL_SECONDS = 60

# How many recent salient events to surface. 10 is enough to convey
# "what's the user been thinking about" without flooding the prompt.
_SALIENT_LIMIT = 10

# How many open threads to surface from the most-recent consolidation.
_OPEN_THREADS_LIMIT = 8

# How many open entity-audit proposals to surface. The graph changes
# slowly; a handful is plenty to prompt the user without flooding.
_PENDING_CORRECTIONS_LIMIT = 5

# How many upcoming dated/recurring memories to surface, and how far ahead
# to look. The relevance-decay layer's re-surfacing half (P3): a birthday
# next week, a deadline in ten days, a still-open promise.
_UPCOMING_LIMIT = 8
_UPCOMING_WINDOW_DAYS = 30.0

# ── the value-ranked, rate-limited pending nudge (Fix 3) ─────────────────────
# The old resource nagged whenever ANYTHING was pending, counting low-value
# edge_reviews into the sentence. Now the nudge sentence is value-ranked and
# rate-limited: conflicts (a memory conflict needs the operator's call) always
# earn a mention; retype/merge/ontology earn one only when the high-value queue
# has grown by NUDGE_MIN_NEW since it was last shown AND at least NUDGE_COOLDOWN_
# DAYS have passed. edge_reviews are NEVER in the nudge sentence — they expire on
# their own (Fix 2). Showing the nudge records the marker = the acknowledgment.
_PENDING_NUDGE_MARKER = "pending_nudge"
NUDGE_MIN_NEW = 3
"""High-value pending items must have grown by at least this many since the last
shown nudge before a non-conflict nudge fires again."""
NUDGE_COOLDOWN_DAYS = 7.0
"""And at least this many days must have passed since the last shown nudge (a
conflict bypasses BOTH gates)."""


class _Cache:
    """Tiny TTL+key cache for the session-start payload."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[str | None, int] | None = None  # (latest_event_id, ttl_bucket)
        self._payload: dict[str, Any] | None = None
        self._cached_at: float = 0.0

    def get(self, key: tuple[str | None, int]) -> dict[str, Any] | None:
        with self._lock:
            if (
                self._key == key
                and self._payload is not None
                and time.monotonic() - self._cached_at <= _CACHE_TTL_SECONDS
            ):
                return dict(self._payload)  # defensive copy
            return None

    def set(self, key: tuple[str | None, int], payload: dict[str, Any]) -> None:
        with self._lock:
            self._key = key
            self._payload = dict(payload)
            self._cached_at = time.monotonic()


_cache = _Cache()


def build_session_start_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compose the session-start payload from substrate state.

    One SQLite read, plus ONE mutable, non-substrate write when the value-ranked
    nudge is shown (it upserts the ``pending_nudge`` rate-limit marker in
    ``worker_watermarks`` — the acknowledgment, so the nudge self-quiets). Cached
    by the public ``read_session_start`` wrapper; this helper is exposed so tests
    can compute the payload directly without the cache layer. The marker write is
    idempotent per (queue-state, cooldown), so a re-compute on cache miss is safe.
    """
    # Imported lazily to avoid the agents → mcp circular at import time.
    from ..agents.mode_switcher import read_current_mode
    from ..agents.salience import SALIENCE_PRODUCED_BY
    from ..substrate import (
        count_pending_conflict_proposals,
        count_pending_corrections_by_kind,
        count_pending_ontology_proposals,
        live_kind_slugs,
        read_pending_conflict_proposals,
        read_pending_corrections,
        read_pending_ontology_proposals,
    )

    mode = read_current_mode(conn)
    salient = _read_top_salient(conn, limit=_SALIENT_LIMIT)
    open_threads = _read_open_threads(conn, limit=_OPEN_THREADS_LIMIT)
    vault_size = _read_vault_size(conn)
    cumulative_salience = sum(item["salience"] for item in salient)

    # Conflicts FIRST — the highest-value class (a memory conflict needs the
    # operator's call). Itemized so the AI can raise the top ones, not just a
    # count. Each gets the same directional prompt the decide surface serves.
    conflict_pending = [
        {
            "id": p.id,
            "kind": "conflict",
            "prompt": _conflict_prompt(p.reason),
            "confidence": p.confidence,
        }
        for p in read_pending_conflict_proposals(conn, limit=_PENDING_CORRECTIONS_LIMIT)
    ]
    # entity-audit corrections split by value: edge_reviews (low-value,
    # self-expiring) are separated from retype/merge/merge_review so they never
    # ride the nudge sentence. All still travel in the structured payload.
    audit_all = read_pending_corrections(conn, limit=_PENDING_CORRECTIONS_LIMIT * 2)
    high_value_audit = [
        {"id": p.id, "kind": p.kind, "prompt": p.prompt, "confidence": p.confidence}
        for p in audit_all
        if p.kind != "edge_review"
    ][:_PENDING_CORRECTIONS_LIMIT]
    edge_review_pending = [
        {"id": p.id, "kind": p.kind, "prompt": p.prompt, "confidence": p.confidence}
        for p in audit_all
        if p.kind == "edge_review"
    ][:_PENDING_CORRECTIONS_LIMIT]
    # Ontology proposals (ADR-0003 Phase 5): same surface, same decide loop.
    ontology_pending = [
        {
            "id": p.id,
            "kind": f"ontology_{p.action}",
            "prompt": p.prompt,
            "confidence": p.confidence,
        }
        for p in read_pending_ontology_proposals(conn, limit=_PENDING_CORRECTIONS_LIMIT)
    ]
    # Conflicts first, then the high-value corrections + ontology, then the
    # low-value edge_reviews — ranked order in the structured payload.
    pending = conflict_pending + high_value_audit + ontology_pending + edge_review_pending
    upcoming = _read_upcoming(conn)

    # Rate-limit + auto-acknowledge the nudge SENTENCE (Fix 3). The high-value
    # total EXCLUDES edge_reviews and uses the TRUE queue counts (not the capped
    # itemized lists above, which cap at _PENDING_CORRECTIONS_LIMIT), so the growth
    # gate keeps working past the display cap. A conflict always nudges; otherwise
    # the queue must have grown by NUDGE_MIN_NEW since the last nudge AND
    # NUDGE_COOLDOWN_DAYS must have passed. Showing it records the marker = the ack.
    conflicts_pending_total = count_pending_conflict_proposals(conn)
    _by_kind = count_pending_corrections_by_kind(conn)
    high_value_audit_total = (
        _by_kind.get("retype", 0) + _by_kind.get("merge", 0) + _by_kind.get("merge_review", 0)
    )
    ontology_total = count_pending_ontology_proposals(conn)
    # The growth baseline tracks ONLY the non-conflict high-value total (entity
    # retype/merge + ontology). Conflicts bypass the growth gate unconditionally,
    # so folding them into the stored baseline would let a resolved conflict
    # deflate the count and wrongly suppress a later legitimate non-conflict
    # nudge. Keep the two separate: conflicts gate on their own presence, the
    # rest gates on non-conflict growth.
    non_conflict_high_value_total = high_value_audit_total + ontology_total
    show_nudge = _nudge_should_show(
        conn,
        conflicts_pending=conflicts_pending_total,
        non_conflict_high_value_total=non_conflict_high_value_total,
    )

    instructions = (
        "These are the top recent salient events from the user's vault "
        "plus any unresolved threads. Treat them as already-known "
        "context for this session. For more specific questions, call "
        "afair.recall(query=...). After a recall, the next time you "
        'call recall or remember, include feedback={"useful_event_ids'
        '":[...], "not_useful_event_ids":[...], "missing_topic":'
        '"..."} referencing the hits from the prior recall. This '
        "signal trains the self-improvement tuner. Empty payload is a "
        "no-op."
    )
    # The nudge SENTENCE — only when a conflict is pending or the high-value queue
    # grew past the cooldown. Never mentions edge_reviews.
    if show_nudge and (conflict_pending or high_value_audit or ontology_pending):
        kinds_hint = "/".join(live_kind_slugs(conn))
        if conflict_pending:
            instructions += (
                " A memory conflict needs your call: two memories are in "
                "unresolved tension. The top ones are listed in "
                "pending_corrections (kind 'conflict') with a directional "
                "prompt. Raise one when it fits, then apply the answer with "
                'afair.recall(decide={"proposal_id":"<id>","verdict":'
                '"confirm"|"reject"|"retract"}).'
            )
        if high_value_audit or ontology_pending:
            instructions += (
                " There are also entity-graph corrections waiting (retype / "
                "merge review, and ontology revisions from the Schema-Evolver). "
                "The top ones are listed in pending_corrections, each with a "
                "ready-to-ask prompt. When one fits the conversation, ask the "
                "user, then apply their answer with "
                'afair.recall(decide={"proposal_id":"<id>","verdict":"confirm"|'
                '"reject"|"retract"}). If they say the kind is wrong, pass the '
                f"corrected one as to_kind ({kinds_hint}). Never apply without "
                "asking."
            )
    else:
        # Suppressed: the items still ride the structured payload, but do not
        # announce the count. Surface only if it genuinely fits the conversation.
        instructions += (
            " pending_corrections may carry items; surface one only if it fits "
            "the conversation naturally. Do not announce the count as a to-do "
            "list."
        )
    # edge_reviews are ALWAYS handled automatically and never part of the nudge.
    if edge_review_pending:
        instructions += (
            " Low-confidence relation reviews are handled automatically and "
            "expire on their own; mention them only if the user asks."
        )
    if upcoming:
        instructions += (
            " upcoming lists dated and recurring memories coming due soon "
            "(a birthday, a deadline, a still-open promise), each with the "
            "date it next matters. Bring one up when it fits the conversation."
        )

    # Record the acknowledgment when the nudge was shown (rate-limit auto-ack).
    # The marker stores the NON-CONFLICT high-value total so the next growth
    # comparison is against a conflict-free baseline (resolving a conflict must
    # not move the baseline).
    if show_nudge and (conflict_pending or high_value_audit or ontology_pending):
        _write_nudge_marker(conn, high_value_total=non_conflict_high_value_total)

    return {
        "mode": mode,
        "cumulative_salience": round(cumulative_salience, 3),
        "vault_size": vault_size,
        "recent_salient_events": salient,
        "open_threads": open_threads,
        "pending_corrections": pending,
        "upcoming": upcoming,
        "salience_producer": SALIENCE_PRODUCED_BY,
        "instructions": instructions,
    }


def _read_upcoming(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Dated/recurring memories coming due within the window, soonest first.

    The re-surfacing half of the relevance-decay layer (P3): the same temporal
    metadata that demotes a passed deadline lifts a birthday or a still-open
    promise back up as it approaches.
    """
    from datetime import UTC, datetime

    from ..substrate import next_relevant_moment, upcoming_temporal

    now = datetime.now(UTC)
    records = upcoming_temporal(conn, now, within_days=_UPCOMING_WINDOW_DAYS, limit=_UPCOMING_LIMIT)
    out: list[dict[str, Any]] = []
    for record in records:
        when = next_relevant_moment(record, now)
        out.append(
            {
                "event_id": record.event_id,
                "temporal_class": record.temporal_class,
                "when": when.isoformat() if when is not None else None,
            }
        )
    return out


def read_session_start(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the session-start payload, cached for ``_CACHE_TTL_SECONDS``.

    Cache key is the latest event id — any new remember/observe
    invalidates the cache implicitly. TTL bucketing prevents staleness
    when no new events have landed for a long time.
    """
    row = conn.execute("SELECT id FROM events ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_id = row["id"] if row else None
    ttl_bucket = int(time.monotonic() // _CACHE_TTL_SECONDS)
    key = (latest_id, ttl_bucket)

    cached = _cache.get(key)
    if cached is not None:
        return cached

    payload = build_session_start_payload(conn)
    _cache.set(key, payload)
    return payload


def clear_cache() -> None:
    """Reset the cache. Used by tests; rarely called in production."""
    global _cache
    _cache = _Cache()


def _conflict_prompt(reason: str) -> str:
    """A directional yes/no prompt for one unresolved conflict pair (ADR-0008),
    safe to show the operator verbatim. Kept in sync with the handlers copy that
    serves the same prompt on the recall/decide surface."""
    base = (
        "Two of your memories are in unresolved tension. Is the newer one "
        "current (supersedes the older), is the newer one wrong (keep the "
        "older), or is this not a real conflict?"
    )
    return f"{base} ({reason})" if reason else base


# ── the pending-nudge rate-limit marker (mutable, non-substrate) ─────────────


def _read_nudge_marker(conn: sqlite3.Connection) -> tuple[str | None, int]:
    """Return ``(last_shown_iso, last_shown_high_value_total)`` for the nudge, or
    ``(None, 0)`` if the nudge has never been shown.

    Stored in ``worker_watermarks`` (mutable derived state, same footing as the
    edge-scorer epochs): ``through_created_at`` carries the last-shown ISO
    timestamp, ``through_id`` carries the last-shown high-value total as text."""
    from ..substrate import watermarks

    wm = watermarks.read_watermark(conn, _PENDING_NUDGE_MARKER)
    if wm is None:
        return (None, 0)
    last_shown_iso, total_str = wm
    try:
        return (last_shown_iso, int(total_str))
    except (ValueError, TypeError):
        return (last_shown_iso, 0)


def _write_nudge_marker(conn: sqlite3.Connection, *, high_value_total: int) -> None:
    """Record that the nudge was just shown: store now + the high-value total it
    was shown for. A plain upsert (NOT ``write_watermark``, whose monotonic-ULID
    guard would reject this non-ULID, decreasable value)."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO worker_watermarks (worker, through_created_at, through_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(worker) DO UPDATE SET
                through_created_at = excluded.through_created_at,
                through_id = excluded.through_id,
                updated_at = excluded.updated_at
            """,
            (_PENDING_NUDGE_MARKER, now, str(high_value_total), now),
        )


def _nudge_should_show(
    conn: sqlite3.Connection, *, conflicts_pending: int, non_conflict_high_value_total: int
) -> bool:
    """Decide whether the nudge SENTENCE fires this session (Fix 3 rate-limit).

    Fires when EITHER a conflict is pending (always — a memory conflict needs the
    operator's call, and bypasses both gates) OR the non-conflict high-value queue
    (entity retype/merge + ontology) has grown by at least ``NUDGE_MIN_NEW`` since
    the last shown nudge AND at least ``NUDGE_COOLDOWN_DAYS`` have passed. The
    growth baseline deliberately EXCLUDES conflicts (they gate on their own
    presence, so folding them in would let a resolved conflict deflate the
    baseline and suppress a later legitimate nudge) and edge_reviews (those never
    nudge)."""
    if conflicts_pending > 0:
        return True
    last_shown_iso, last_total = _read_nudge_marker(conn)
    if non_conflict_high_value_total - last_total < NUDGE_MIN_NEW:
        return False
    if last_shown_iso is None:
        return True  # never shown, and the growth gate above already passed
    from datetime import UTC, datetime

    try:
        last_dt = datetime.fromisoformat(last_shown_iso)
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    elapsed_days = (datetime.now(UTC) - last_dt).total_seconds() / 86400.0
    return elapsed_days >= NUDGE_COOLDOWN_DAYS


# ── substrate readers ──────────────────────────────────────────────────────


def _read_top_salient(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    """Top-N salience-scored events by score, ordered most-salient first.

    Surfaces the summary + type_hint so the AI sees "what's hot" in the
    user's recent memory without needing the full payload.
    """
    from ..agents.salience import SALIENCE_PRODUCED_BY

    rows = conn.execute(
        """
        SELECT e.id, e.content_hash, e.created_at, e.kind, e.payload,
               i.extraction AS salience_extraction
        FROM interpretations i
        JOIN events e ON e.content_hash = i.event_hash
        WHERE i.produced_by = ?
        ORDER BY i.produced_at DESC
        LIMIT ?
        """,
        (SALIENCE_PRODUCED_BY, limit * 3),  # over-fetch so we can re-rank by score
    ).fetchall()

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        try:
            sx = json.loads(row["salience_extraction"])
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        score = float(sx.get("salience", 0.0))

        # Pull a short preview text from the payload — type-aware.
        preview = _preview_for_payload(payload)
        scored.append(
            (
                score,
                {
                    "event_id": row["id"],
                    "content_hash": row["content_hash"],
                    "created_at": row["created_at"],
                    "kind": row["kind"],
                    "salience": round(score, 3),
                    "preview": preview,
                    "type_hint": payload.get("type_hint") if isinstance(payload, dict) else None,
                },
            )
        )

    # Re-rank by salience score descending; cap at the requested limit.
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def _read_open_threads(conn: sqlite3.Connection, *, limit: int) -> list[str]:
    """Pull ``open_threads`` from the most-recent consolidation.

    The Consolidator writes its daily digest as a substrate EVENT
    (``kind='consolidation'``), NOT an interpretation row — its
    ``open_threads`` array lives in that event's payload (see
    ``consolidator._write_consolidation``). The old read looked for a
    ``consolidator:%`` interpretation row that production never produces, so
    this always returned ``[]`` since ship. Reading the event payload is the
    correct read-projection over the unchanged substrate (I3). Served by the
    existing ``events_kind_created_at_idx``; no new index.

    Surfacing the still-loose items at session start is the "what's
    unresolved" baseline.
    """
    from ..agents.consolidator import CONSOLIDATION_KIND  # lazy — avoid import cycle

    row = conn.execute(
        """
        SELECT payload FROM events
        WHERE kind = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (CONSOLIDATION_KIND,),
    ).fetchone()
    if row is None:
        return []
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return []
    threads = payload.get("open_threads", []) if isinstance(payload, dict) else []
    if not isinstance(threads, list):
        return []
    cleaned: list[str] = []
    for t in threads:
        if isinstance(t, str) and t.strip():
            cleaned.append(t.strip())
        elif isinstance(t, dict) and isinstance(t.get("text"), str):
            cleaned.append(t["text"].strip())
        if len(cleaned) >= limit:
            break
    return cleaned


def _read_vault_size(conn: sqlite3.Connection) -> dict[str, int]:
    """Cheap vault-overview counts for the AI's "size" intuition."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "  SUM(CASE WHEN kind = 'remember' THEN 1 ELSE 0 END) AS remembers, "
        "  SUM(CASE WHEN kind = 'observe' THEN 1 ELSE 0 END) AS observes "
        "FROM events"
    ).fetchone()
    if row is None:
        return {"total": 0, "remembers": 0, "observes": 0}
    return {
        "total": int(row["total"] or 0),
        "remembers": int(row["remembers"] or 0),
        "observes": int(row["observes"] or 0),
    }


def _preview_for_payload(payload: Any) -> str:
    """Short text preview for a payload. ~200 chars max."""
    if not isinstance(payload, dict):
        return ""
    content_type = payload.get("content_type")
    if content_type == "text":
        text = payload.get("text")
        if isinstance(text, str):
            return text[:200]
    elif content_type == "compound":
        parts = payload.get("parts", [])
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    pt = part.get("text")
                    if isinstance(pt, str):
                        return pt[:200]
    elif content_type == "binary" or content_type == "text-large":
        hint = payload.get("filename_hint") or payload.get("mime") or ""
        return f"[{content_type}] {hint}".strip()
    elif content_type == "event":
        # observe-event shape
        action = payload.get("action", "")
        subject = payload.get("subject", "")
        return f"{action}: {subject}".strip(": ").strip()
    return ""

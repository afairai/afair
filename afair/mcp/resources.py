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

    Pure function — no I/O outside the SQLite read. Cached by the
    public ``read_session_start`` wrapper; this helper is exposed so
    tests can compute the payload directly without the cache layer.
    """
    # Imported lazily to avoid the agents → mcp circular at import time.
    from ..agents.mode_switcher import read_current_mode
    from ..agents.salience import SALIENCE_PRODUCED_BY
    from ..substrate import (
        live_kind_slugs,
        read_pending_corrections,
        read_pending_ontology_proposals,
    )

    mode = read_current_mode(conn)
    salient = _read_top_salient(conn, limit=_SALIENT_LIMIT)
    open_threads = _read_open_threads(conn, limit=_OPEN_THREADS_LIMIT)
    vault_size = _read_vault_size(conn)
    cumulative_salience = sum(item["salience"] for item in salient)
    pending = [
        {"id": p.id, "kind": p.kind, "prompt": p.prompt, "confidence": p.confidence}
        for p in read_pending_corrections(conn, limit=_PENDING_CORRECTIONS_LIMIT)
    ]
    # Ontology proposals (ADR-0003 Phase 5) join the same pending list —
    # same surface, same decide loop, no new mechanism (I1).
    ontology_pending = [
        {
            "id": p.id,
            "kind": f"ontology_{p.action}",
            "prompt": p.prompt,
            "confidence": p.confidence,
        }
        for p in read_pending_ontology_proposals(conn, limit=_PENDING_CORRECTIONS_LIMIT)
    ]
    pending += ontology_pending
    upcoming = _read_upcoming(conn)

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
    if pending:
        kinds_hint = "/".join(live_kind_slugs(conn))
        instructions += (
            " pending_corrections lists entity-graph fixes the audit "
            "proposed — most are cross-kind auto-merges where the system "
            "picked a kind for an entity (e.g. 'Clario' filed as product when "
            "it's your project). Each has a ready-to-ask prompt. When it fits "
            "the conversation, ask the user, then apply their answer with "
            'afair.recall(decide={"proposal_id":"<id>","verdict":"confirm"|'
            '"reject"|"retract"}). If they say the kind is wrong, pass the '
            f"corrected one as to_kind ({kinds_hint}), "
            'e.g. verdict="reject", to_kind="project". '
            "If they say it isn't a real entity at all (a file path, a test "
            'artifact), use verdict="retract" to withdraw it. Never apply '
            "without asking."
        )
    if ontology_pending:
        instructions += (
            " Entries whose kind starts with 'ontology_' revise the vault's "
            "kind system itself (add/rename/merge/split/deprecate an entity "
            "kind, proposed by the Schema-Evolver from observed usage). "
            'Decide them the same way: verdict="confirm" applies the '
            'revision, verdict="reject" leaves the ontology unchanged, and '
            'verdict="revert" on a previously applied one undoes it with a '
            "compensating revision. Never apply without asking."
        )
    if upcoming:
        instructions += (
            " upcoming lists dated and recurring memories coming due soon "
            "(a birthday, a deadline, a still-open promise), each with the "
            "date it next matters. Bring one up when it fits the conversation."
        )

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

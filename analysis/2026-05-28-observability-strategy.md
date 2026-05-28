# Observability Strategy — Phase 0.5

**Date:** 2026-05-28
**Status:** Proposal, captured for next sprint. Not yet implemented.
**Trigger:** Investigation of heizzeit-event extraction stall (2026-05-28 08:28 UTC) and consolidator silence since 2026-05-26. Both invisible to current tooling.

---

## 0. The core insight

**Errors are easy. Silent no-shows are hard.**

The current system reliably catches and stores LLM errors as `status: failed` rows. It does NOT catch:

- Worker threads crashing on exceptions outside the `except LLMError` block.
- The in-memory ThreadPoolExecutor losing queued work on machine restart.
- Cold-path workers ticking but producing zero output for legitimate reasons (sparse day, all-judged pairs) vs. illegitimate ones (silent crash, schedule bug).
- Pipeline steps that should have happened by now but haven't.
- Workers that should be scheduled at all but aren't running.

What we need is not "more logs." It's a way to make the **designed flow** observable as data — so when reality diverges from the design, the system itself can tell us where and when.

---

## 1. Concrete failure modes we hit in the field

### 1.1 Heizzeit-event stall (2026-05-28 08:28 UTC)

Event written. Four hours later, `interpretation: null`. No `status: failed` row. No log line indicating failure. The fly machine has not restarted since 2026-05-27 15:09 UTC — so the ThreadPoolExecutor queue is technically alive, but the work for this event either:

- Never got enqueued (race in `schedule_extraction` somehow swallowed)
- Got enqueued but the worker crashed on a non-LLMError exception before reaching the `write_failed_interpretation()` call
- Got enqueued, started, and is stuck (deadlock on SQLite, infinite retry loop in litellm, etc.)

**We cannot distinguish these four states.** The only signal is the absence of an interpretation row, which could mean any of them.

### 1.2 Consolidator silence since 2026-05-26

Last consolidation events: both 2026-05-26 morning. The `interval_seconds = 6 * 3600` + `LOOKBACK_DAYS = 2` design says: every 6h, look at today + past 2 days, write a consolidation for any day with ≥3 events that doesn't already have one.

2026-05-26 had many events (>3, easily). The consolidator should have written a consolidation for 26th somewhere on the 27th. It didn't. Either:

- The cold-path scheduler isn't ticking the consolidator at all (subtle regression)
- The scheduler ticks but the consolidator's `_due_days_to_process()` returns empty for reasons we can't see
- The consolidator runs and fails silently before writing the consolidation event

**We have no way to query "when did the consolidator last tick, and what did it decide?"** The `cold_path.worker_done` log line exists, but it only emits on completion — if the worker never started, no log. If it started and crashed, no log.

### 1.3 ThreadPoolExecutor in-process queue

```python
# afair/agents/extractor.py:48
_EXECUTOR = ThreadPoolExecutor(max_workers=4)
```

The warm-path extraction queue lives entirely in Python process memory. On any restart — deploy, OOM, crash, fly-machine-stop — every queued task vanishes silently. Events sit with `interpretation: null` forever. No retry mechanism.

For Phase 0 single-user single-machine, this is acceptable for *one* day. For a user running the system for months across many deploys, the silent-loss probability becomes nontrivial.

---

## 2. Three-layer strategy

### Layer A — Pipeline trace events (substrate-level)

Every event has an implicit lifecycle:

```
T0  event.written           ← substrate INSERT (HAVE)
T1  extraction.enqueued     ← schedule_extraction submit (MISSING)
T2  extraction.started      ← worker thread picks up (MISSING)
T3  extraction.completed    ← {success | failed} (PARTIAL — failure recorded, success silent)
T4  canonicalization.started ← entity worker selects event (MISSING)
T5  canonicalization.completed (MISSING)
T6  conflict_resolution.attempted (MISSING)
T7  consolidation.included  ← day's consolidator ran and used this event (MISSING)
```

We currently have T0 (substrate row exists) and partial T3 (failure rows for LLMError specifically). The other six are invisible.

**Proposal: a new append-only `pipeline_events` table.**

```sql
CREATE TABLE pipeline_events (
  id              TEXT PRIMARY KEY,           -- ULID
  event_hash      TEXT NOT NULL,              -- the substrate event this trace is about
  step            TEXT NOT NULL,              -- "extraction.enqueued", etc.
  worker          TEXT,                       -- "extractor", "entity_canonicalizer", "consolidator"
  status          TEXT NOT NULL,              -- "started" | "completed" | "failed" | "skipped"
  payload         TEXT,                       -- JSON: error_type, duration_ms, skip_reason, etc.
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX pipeline_events_by_hash ON pipeline_events(event_hash, created_at DESC);
CREATE INDEX pipeline_events_by_step ON pipeline_events(step, created_at DESC);
```

Properties:

- **Append-only.** Same UPDATE/DELETE trigger pattern as substrate events. Spiritually I2-aligned.
- **Separate from substrate `events` table.** Pipeline traces are infrastructure noise, not user content. Keeping them separate means recall queries don't get polluted, and retention can be different (e.g., 90 days for traces vs. forever for substrate).
- **Indexed by event_hash** so "trace the lifecycle of this one event" is one query.
- **Indexed by step** so "show me all events that enqueued extraction but never started" is one query.

Writers: every cold-path worker + warm-path extractor + entity canonicalizer emits trace events at start, end, skip, and error. ~5 lines of code per emission point.

### Layer B — Expected-behavior watchdog

Declarative rules for "what should have happened by now."

```python
# afair/observability/expectations.py
EXPECTATIONS = [
    Expectation(
        name="extraction_completes_quickly",
        trigger="event.written",
        deadline_seconds=120,
        check_query="""
            SELECT count(*) FROM events e
            LEFT JOIN interpretations i ON i.event_hash = e.content_hash
            WHERE i.event_hash IS NULL
              AND e.created_at < datetime('now', '-120 seconds')
              AND e.kind IN ('remember', 'observe')
        """,
        severity="warning",
        suggested_action="Check pipeline_events for this event. Possibly rerun via extract_sync.",
    ),
    Expectation(
        name="consolidator_runs_daily",
        trigger="midnight UTC",
        deadline_seconds=6 * 3600,  # 6h after midnight = should have run by 06:00 UTC
        check_query="""
            SELECT count(*) FROM events
            WHERE kind = 'consolidation'
              AND json_extract(payload, '$.target_day') = date('now', '-1 day')
        """,
        invert=True,  # 0 rows = violation
        severity="error",
        suggested_action="Check pipeline_events for worker=consolidator. Investigate scheduler tick.",
    ),
    Expectation(
        name="canonicalizer_processes_new_extractions",
        trigger="extraction.completed",
        deadline_seconds=300,  # 5 min
        check_query="""
            SELECT count(*) FROM interpretations i
            WHERE json_extract(i.extraction, '$.status') != 'failed'
              AND i.created_at < datetime('now', '-300 seconds')
              AND NOT EXISTS (
                SELECT 1 FROM entity_mentions m WHERE m.event_hash = i.event_hash
              )
              AND NOT EXISTS (  -- skip events with no entities to mention
                SELECT 1 FROM pipeline_events pe
                WHERE pe.event_hash = i.event_hash
                  AND pe.worker = 'entity_canonicalizer'
                  AND pe.status IN ('completed', 'skipped')
              )
        """,
        severity="warning",
    ),
]
```

A meta-worker (`ExpectationChecker`, runs every 5 min) iterates the expectations, runs each check query, records violations as `pipeline_events` rows with `step="expectation.violated"`.

**The point:** these are not error logs. They are statements about the **designed flow** that the system itself verifies. When reality diverges from the design, the system tells us — by name — which expectation failed and what to look at next.

### Layer C — Health endpoint enrichment

Current `/health` returns `{"status":"ok"}`. Should return:

```json
{
  "status": "ok" | "degraded" | "down",
  "version": "<commit sha>",
  "uptime_seconds": 12345,
  "substrate": {
    "total_events": 63,
    "events_pending_extraction": 1,
    "oldest_pending_extraction_age_s": 14400
  },
  "workers": {
    "extractor": {
      "completed_24h": 1,
      "failed_24h": 0,
      "stalled_24h": 1,
      "queue_depth": 0
    },
    "consolidator": {
      "last_tick_at": "2026-05-27T18:16:00Z",
      "last_emit_at": "2026-05-26T08:16:00Z",
      "days_overdue": 2
    },
    "canonicalizer": { ... },
    "conflict_resolver": { ... },
    "pruner": { ... }
  },
  "expectations": {
    "violated_count": 2,
    "by_name": ["extraction_completes_quickly", "consolidator_runs_daily"]
  }
}
```

`status` derivation:

- `down` — DB unreachable (current behavior)
- `degraded` — ≥1 `severity: error` expectation violation
- `ok` — no error violations (warnings OK, but visible in body)

This makes the orchestrator (Fly, future load balancer) aware of degraded brain state without inventing a separate health surface.

---

## 3. Concrete code drops needed

In rough priority order:

| # | Drop | Effort | Unlocks |
|---|---|---|---|
| 1 | `pipeline_events` table + `record_pipeline_event()` helper | ~half day | substrate-level tracing |
| 2 | Wrap warm-path extractor in `try/except Exception` with `record_pipeline_event(step="extraction.crashed", ...)` so silent crashes become visible | ~1h | catches the heizzeit-class failure mode |
| 3 | Add trace emissions at every worker start/end/skip in `cold_path.py` | ~2h | makes cold path visible |
| 4 | Replace the in-process ThreadPoolExecutor with a SQLite-backed queue table (`extraction_queue` with `claimed_at` + heartbeat) | ~1 day | eliminates restart-loss class entirely |
| 5 | `ExpectationChecker` worker + `EXPECTATIONS` list with the 3 rules above | ~half day | declarative SLO-style monitoring |
| 6 | Health endpoint enrichment | ~2h | external visibility |
| 7 | `uv run python -m afair diagnose` CLI subcommand: walks all events with `interpretation: null` older than threshold, prints lifecycle tree per event with suggested remediation | ~half day | human debugging UX |

Drops 1+2 together fix the immediate heizzeit-class problem AND give us the data plane for everything else. Worth shipping as the first sprint.

Drop 4 (durable queue) is the bigger architectural change but pays for itself the first time a deploy eats an event in flight.

Drops 5+6 are the "the brain tells us when it's wrong" surface — exactly what the user asked for.

Drop 7 is the human debugging UX — useful from day one of layer A.

---

## 4. What this is NOT

- **Not** Sentry / Honeybadger / external error tracker. We can add one later but it solves a different problem (cross-service exception aggregation). What's missing here is **visibility into the designed flow**, not exception aggregation.
- **Not** Datadog / Grafana / metrics dashboard. Phase 0 has one machine, one user. Visualization is wasted work until there's something to visualize.
- **Not** OpenTelemetry traces. Single process, single user — the distributed-tracing model is overkill. The substrate-level pipeline_events table gives us the same shape (trace per event) with native query power.
- **Not** structured logs alone. Logs are ephemeral and machine-restart-vulnerable. Pipeline events are durable, queryable, and survive restarts — same Invariant I2 spirit.

---

## 5. Immediate manual action for heizzeit-event (today)

Before any of this ships, the heizzeit event can be unstuck by hand:

```bash
fly ssh console -a afair
cd /app
python -c "
from afair.agents.extractor import extract_sync
extract_sync('01KSPV6BEWWRYJ62FJ0Z2C0J7E')
"
```

This calls the synchronous extraction path, bypassing the threadpool. If it succeeds, interpretation lands and the canonicalizer picks it up on the next cycle. If it fails, we see the actual exception in our session.

---

## 6. Cross-references

- `VISION.md §6.5` — Agent Swarm hot/warm/cold paths (the flow that needs to become observable).
- `VISION.md §I7` — Recursive self-modification recorded. Pipeline traces are exactly that surface for system meta-actions.
- `VISION.md §I2` — Append-only substrate. The `pipeline_events` table follows the same discipline.
- `CLAUDE.md §0.1` — current Phase 0 status includes "failed extractions stored as `status: failed` rows for retry/diagnosis" — this proposal expands that surface from just failures to the full lifecycle.
- `~/.claude/rules/observability.md` — global rule. Most of it applies; the EU/PII parts especially. Phase 0 single-user simplifies the privacy story; the principle still holds.
- `docs/operations.md §11 Common failures` — adds new entries once layer A ships.

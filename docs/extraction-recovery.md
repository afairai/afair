# Extraction recovery and foreground recall

A committed intake event is the durable extraction queue. If the process dies
between the event write and executor submission, or a provider budget defers
work, `ExtractionRecoveryWorker` finds the gap on a later cycle or restart.
It runs every five minutes, scans at most 500 events using a lagged ULID keyset
cursor, and durably queues eligible gaps in the operational `extraction_jobs`
table before advancing that cursor. A crash before cursor advancement simply
repeats an idempotent batch. Existing queued gaps survive cursor advancement and
restart; telemetry retention cannot hide them. Recent events wait five minutes
for the warm path. At most five queued events are attempted per cycle. Intake
payload shapes are eligible; internal worker origins and generated outputs are
excluded. Empty steady-state discovery uses an indexed cursor lookup, rather
than a historical census. Deleting the discovery watermark safely rescans history.

Warm extraction, recovery, transient retry and optional vector repair share
atomic SQLite token leases across processes, plus bounded per-event process
locks. Recovery rechecks for an existing interpretation after acquiring the
lease, so a CLI batch and the live worker cannot extract the same event at once.
Leases expire after fifteen minutes if the owner dies; the expiry exceeds the
bounded normal provider timeout/retry envelope. Provider deferrals back off five
minutes and leave an honest pending job without appending a failed interpretation
or consuming a retry. Later candidates can proceed. Existing failed attempts
remain append-only and follow the bounded transient-retry policy. Warm executor
submission also caps outstanding work at 64 events; overload stays discoverable
from the committed raw event.

Read a counts-only historical census without provider calls. This explicitly
requested audit scans history, opens the existing vault in SQLite read-only
mode, and performs no directory creation, schema initialization or writes:

```sh
uv run python scripts/recover_extractions.py
```

After reviewing the census, repair one bounded batch (repeat explicitly as
needed; never start an unbounded global reprocess):

```sh
uv run python scripts/recover_extractions.py --apply --limit 5
uv run python scripts/recover_extractions.py --apply --limit 5 --repair-vectors
```

The optional vector stage uses existing successful interpretations, validates
the configured dimension, and inserts only missing vectors. It does not rebuild
or replace an index. Keep `EMBEDDING_MODEL` and `EMBEDDING_DIM` identical to the
model used to build the live index. This script loads the normal vault settings
and encryption key; use the intended environment. It writes no raw events.

Foreground recall marks itself active and cold-path work yields between workers
(and between recovery/retry candidates), with a two-second quiet period. A
provider call already in progress cannot be preempted. Semantic recall waits at
most two seconds from embedding submission (local keyword search consumes that
same deadline); if the provider is slow, unavailable,
or all eight embedding slots are occupied, keyword and entity results return
with an explicit fallback note. Timed-out running calls may finish and populate
the query cache, but cannot create an unbounded executor backlog.

Each recall emits one content-free `recall.timing` log with total milliseconds,
embedding wait milliseconds, local SQLite/hydration milliseconds, fallback and
success booleans. The local component includes routing, ranking and projection;
it is not engine-only SQL timing. Compare p50/p95 across these fields to separate
provider latency from local hydration/locking. No query, event identifier, source
payload or returned text is included.

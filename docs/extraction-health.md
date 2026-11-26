# Extraction health: work gaps versus missing traces

`/health` is liveness, not a provider-readiness verdict. Its pipeline snapshot
counts intake `event.written` records in the last seven days. An open intake
record is **pending** during its first thirty minutes and **stuck** afterward.
`expectation_violations` is `stuck_extractions + retry_exhausted`; deterministic
`permanent_failures` are reported separately and do not enter that sum. The
snapshot is refreshed by the expectation checker every fifteen minutes.

A terminal pipeline trace is best-effort operational telemetry. A provider result
is durably interpreted before later FTS enrichment and terminal trace recording.
A lost trace can therefore leave completed work looking open; a crash before
FTS enrichment can also leave a valid interpretation with incomplete indexes.
Those states must not be conflated. Re-extracting completed memories merely to create a trace wastes
provider budget and can change derived understanding unnecessarily.

The checker now also recognizes the latest corresponding durable extractor result
as completion when it satisfies the extractor's existing contract: matching event
id **and** content hash, `extractor:%` producer, string summary and kind, and an
explicit success status or a legacy absent status. It also requires a matching
`embedding.stored` or `embedding.failed` trace recorded after that interpretation,
which proves execution passed FTS enrichment and the completed-trace call, and
no failed or deferred operational job. A vector row alone is insufficient because
a separate backfill may have created it. With semantic recall disabled and no
post-FTS trace, the detector conservatively keeps the untraced event open. Explicit null/unknown status,
failed results, metadata-only PDFs, fallback/degraded markers, malformed results,
and non-extractor interpretations do not close an untraced intake. A newer degraded
result cannot be hidden by an older successful row. No event, interpretation or
pipeline trace is rewritten, deleted or fabricated by this reconciliation.

This change preserves the existing treatment of an actual terminal trace and the
existing failure aggregates. It does not reinterpret a low-confidence memory as a
provider failure or add new retrieval-quality thresholds. Embedding fallbacks are
still separately traced and remain outside silent-extraction expectations.

## Repairing real gaps

A successful interpretation with verified post-FTS progression and missing terminal
telemetry needs detector reconciliation, not another model call. A successful
interpretation without post-FTS proof requires an index-only audit/repair decision;
never replay the provider just to create telemetry. Metadata-only PDFs or truly
incomplete fallbacks remain degraded: the operator must provide usable source
text or explicitly resolve that limitation, rather than clear the gate by inventing
a success trace. There is no automatic repair of that class in this change. An event with no interpretation needs the
bounded durable recovery path, subject to its existing budget, lease, backoff and
terminal-failure controls. Inspect counts-only audit output first:

```sh
uv run python scripts/recover_extractions.py
```

Only after explicit authorization to mutate the intended vault, run a bounded
batch (`--apply --limit 5`) and inspect the next audit. Do not run the unbounded
legacy global `afair.admin reprocess` merely to pass a release gate. A terminal
operational job requires diagnosis and a scoped operator decision before it is
rescheduled. A deterministic `llm_error` is not automatically eligible for the
transient retry worker, which only retries timeout/rate-limit failures.

Passing a gate by waiting for a stuck event to age outside the seven-day telemetry
window does not repair it. Durable recovery and its historical audit are independent
of that lookback. Preserve explicit degradation until the underlying gap is fixed;
never add a fake terminal success or clear a snapshot by hand.

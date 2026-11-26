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

## Targeted index-only repair

`scripts/repair_extraction_indexes.py` defaults to a read-only, counts-only audit
of **explicit** event IDs (1–100 unique IDs, no discovery or bulk replay):

```sh
uv run python scripts/repair_extraction_indexes.py --event-id "$TARGET_EVENT_ID"
```

Keep identifiers in the vault operator session, not logs, chat or tickets. Default
mode uses SQLite `mode=ro` and `query_only`; it does not create schema, leases,
provider requests or completion traces. It checks exact FTS contents against the
same composition helper used by normal extraction and checks vector membership.

After separate authorization to modify that vault, append `--apply` for FTS-only
repair. Append `--apply --embeddings` only when missing vectors should also be
created with the configured embedding model through its normal usage ledger. The existing daily dollar cap currently applies
to completions, not embeddings; opting into embedding repair authorizes those
separately metered embedding calls.
The FTS-only operation is provider-free; neither mode invokes extraction,
transcription, vision or a generation model. An existing vector is preserved.

The tool admits only a latest corresponding clean successful interpretation for
an inline-text or observe-event source. Event ID **and** original content hash
must match. Missing, degraded, malformed, terminal and binary/large/compound
cases are skipped. Failed jobs from other purposes, terminal jobs, active leases
and unexpired backoffs remain blocked. A nonterminal job from this repair tool may
retry after its backoff; failures persist until verified success and three failed
attempts become terminal.
It uses the normal per-event lock and cross-process lease, rechecks the result
after acquiring the lease, rebuilds the original hash's FTS row, and verifies
exactly one row with the expected content. Optional vector creation validates the
configured dimension before insertion. A usage-ledger deferral or local/provider failure
retains operational backoff/failure visibility and does not emit completion.

Only after the requested postconditions succeed does it append one current-time
`extraction.completed` trace with producer `index_repair:v1`, identifying actual
index verification rather than pretending the original provider run finished at
some earlier time. The tool verifies that trace was stored. Repeating a successful
operation skips that terminal event, preserving a single repair completion trace.
Raw events, interpretation rows and original associations are never changed.
Embedding repair is optional because FTS completion is the extraction terminal
postcondition; a missing semantic vector remains visible in vector audits.

Choose `--embeddings` on the first apply if vectors are desired: a later run skips
the now-terminal extraction. Already-terminal missing vectors remain the concern
of the existing bounded `repair_missing_vectors` worker, which does not replay
extraction.

When `--embeddings` is explicitly requested, the repair completion trace waits for
that requested vector postcondition too. FTS may already have been verified when
the vector step fails; audit reports that progress independently. Three failures
still exhaust the operational repair job. This is an intentional bounded failure
policy: diagnose the vector/provider cause before the scoped operational-job
rescheduling procedure in [extraction recovery](extraction-recovery.md). Do not
replay extraction, erase failure history merely to pass a gate, or confuse partial
FTS progress with success of the requested vector repair.

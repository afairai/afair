# Background provider accounting and daily budget

`AFAIR_BACKGROUND_DAILY_BUDGET_USD=1.00` enables a vault-wide UTC-day
budget for background generative provider calls. Audio transcription has a separate
ledger operation and is excluded from this generative ceiling. `0` pauses background generation; separately accounted audio transcription remains enabled.
Unset preserves existing self-hosted and local-model behavior. Storing raw
memories and keyword search remain available; deferred extraction is left
pending for the recovery worker. Reset occurs at midnight UTC, without a
restart. In-flight reservations survive process restarts.

Every provider attempt inside a background worker scope gets its own row in
`<vault>/usage.db`: worker, effective model, UTC date/time, status, input/output
token counts, reserved USD, settled estimated USD and estimation source.
The owning scope follows retries, tuning judges and escalations. Provider SDK
retries are disabled in scoped calls, so retries cannot silently escape the
ledger. A paid response is accounted before JSON validation. A timeout or crash
retains its full reservation because the provider may have billed it.

The ledger is operational data, separate from the immutable memory substrate.
It uses the same SQLCipher key and cipher connection factory as the substrate;
this avoids deadlocking on an agent's open substrate write transaction during
network requests. Whole-volume backups include it. SQLite-only backup recipes
must also snapshot `usage.db` with SQLite's backup API; copying only
`substrate.db` does **not** preserve spending reservations. Memory JSONL export
is not a spending-ledger backup. Do not delete the ledger to reset a budget.

Reservations use the maximum output tokens and a conservative input byte bound
(including tools and chat overhead). Vision requests reserve the entire known
model input context because compressed image bytes cannot bound image tokens;
vision models with no known context limit defer under a cap. Haiku 5.5's
1,000,000-token context is built in, independently of LiteLLM's pricing map.
Requests without explicit cache controls reserve uncached input once; requests
with cache controls reserve input at 2× to cover one-hour cache creation.
Haiku 5.5 is the default image extraction model; `VISION_MODEL` overrides it.
Thus default vision can run under a $1 cap (a full-context input reservation
costs $0.50, plus bounded output), releasing unused input on settlement. Atomic `BEGIN IMMEDIATE` reservations
prevent concurrent extractors/workers from spending the same remaining budget.
A provider response releases the unused reservation after settlement. Dollar
values are estimates, not invoices: provider pricing changes and provider
billing adjustments remain external to the vault.

Prices normally come from the installed LiteLLM model map. Unknown model prices
fail closed with an enabled budget. Set `AFAIR_BACKGROUND_MODEL_PRICES` to a
JSON mapping of effective model IDs to `[input_USD_per_million,
output_USD_per_million]` to supply missing prices. Rates must be finite and
nonnegative; zero is appropriate only for genuinely free local inference.
An unpriced earlier attempt on the same day prevents later enabling a cap
from falsely claiming a fully measured day.

The built-in `anthropic/claude-haiku-5-5` reservation uses the conservative
high-context rates $0.50/$2.50 per million. Settlement uses $0.10/$0.50 up to
100,000 input tokens, $0.50/$2.50 above, cache reads at 0.1× and cache writes
at 1.25× for known five-minute writes and 2× for one-hour writes or unknown TTL. These are the
[Anthropic pricing rates](https://platform.claude.com/docs/en/about-claude/pricing)
verified November 26, 2026. Configured prices augment the built-in model mapping, and overrides apply at
reservation and settlement. LiteLLM's installed Anthropic adapter normalizes
`prompt_tokens` to include raw input plus cache reads plus cache writes; the
ledger subtracts cache counts before applying cache-specific multipliers. The
real adapter is covered by a test to guard against counting cached input twice.

The canonicalizer rechecks low-confidence matches from either supported Haiku
version with `anthropic/claude-sonnet-5-5`. This second pass shares the owning
worker's UTC-day budget and call allowance; a refusal defers the event rather
than bypassing the cap. Built-in Sonnet 5.5 prices are $2/$10 per million
input/output tokens, with cache reads at $0.10 per million and cache writes at
$2.50/$4 for five-minute/one-hour writes (unknown TTL uses $4). These prices do
not depend on the installed LiteLLM map; explicit model-price overrides still
apply. The published per-token input/output rates are one-third lower than
Sonnet 4.6's $3/$15; actual cost also depends on tokenization and usage.

Sonnet 5.5 rejects forced tool choice and sampling parameters. Its second-pass
request uses strict automatic tool choice with an explicit tool instruction,
`thinking: {"type": "between_tools"}` and low effort so upfront thinking does
not consume the bounded 300-token verdict. A missing or wrong tool response
still fails closed. Other models retain their existing tool-choice behavior.
See the [Anthropic migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).
This changes future canonicalization only: existing memories and interpretations
are not rewritten and no backfill is triggered.

Coverage is explicit: text, tool calls, judge calls, retries and inline image
requests pass through the ledger. Under an enabled cap, images with unknown model context limits defer because
no safe input-cost bound is available. Known models reserve their entire
input context, covering both inline images and remote URLs. Audio transcription continues under a generative cap and is reported
separately; unsupported transcription cost estimates remain explicitly unknown. Raw content remains stored. With no cap all vision requests continue and
are still logged, with unknown costs explicitly marked. Embedding-provider
charges are outside this background generation budget.

For daily per-worker/model totals call
`uv run python scripts/background_usage.py --day 2026-11-26`
(`--vault-dir` optionally overrides the configured directory). It loads the
configured vault key and opens the ledger using SQLite `mode=ro` plus query-only mode, creating no
missing database or vault directory. `accounted_usd` includes retained
reservations; `unsettled_attempts` and `unpriced_attempts` distinguish uncertain
outcomes from free work. The report separates completed and failed attempts, input/output/cache tokens,
settled estimates and retained reservations. This is a local operational
reconciler; private provider-admin billing reports are not available to the
vault. Compare the report with the provider console or an independently
retrieved admin usage report before treating estimates as invoiced costs.
No prompt text, memory contents or API keys are stored in the ledger.

## Explicit scope and failure policy

The scheduler, extraction, vector repair, remote recall embedding and the shipped
backfill, deduplication and rebuild scripts establish accounting scopes. Local
FastEmbed runs incur no provider fee. Cache hits avoid creating a paid-attempt
row. Embedding and transcription costs appear separately from the generation
cap. Historical embedding costs before instrumentation require a provider invoice.
Custom operator scripts that call providers must establish a named
`background_usage(settings, "operator.name")` scope.

The daily report groups estimated settled costs, retained reservations and
unknown attempts by operation. Pass the daily infrastructure invoice allocation
with `--infrastructure-usd` to include it in the report. An absent allocation
is JSON null. A zero completion budget permits separately accounted embedding
and transcription requests. Read-only reports support older ledgers; the next
accounted operation adds missing columns in a short transaction.

If the ledger cannot be opened or the preflight reservation cannot commit,
work defers before provider I/O, including a plaintext ledger encountered after
enabling vault encryption. Resolve the ledger encryption consistently with the
vault; no automatic deletion or cross-format replacement occurs. A settlement
write failure returns the paid result and retains its committed reservation.
Definitive rejected 400/401/403/404/429 requests release reservations; ambiguous
server errors and timeouts retain them. Failed settlement logging never masks
the original provider exception.

Successful audio transcription is committed to the encrypted substrate's
operational `extraction_transcripts` cache, keyed by event and transcription
model, before downstream generative extraction. A budget refusal, including
one caused by another concurrent worker, leaves this paid transcript reusable
across retries and process restarts. The existing per-event cross-process lease
also prevents concurrent transcriptions of the same event. This cache is covered
by ordinary substrate backups and contains no API credentials. Changing the
transcription model deliberately requests a new transcript.

## Synthetic mixed-load baseline

Run `PYTHONPATH=. uv run python scripts/bench_vault_load.py` from the checkout.
The runner creates a temporary vault with 20,000 historical synthetic events,
issues 100 observe and 100 recall calls alongside a snapshot export and one
recovery-discovery batch, and simulates an unavailable embedding provider.
The extractor dispatch is disabled to make this an offline outage measurement.

On the development Mac on 2026-11-26, recall took 327 ms at p50 and 382 ms at
p95. Writes took 1.65 ms at p50 and 5.46 ms at p95. All 100 recalls fell back
to keyword retrieval. Recovery queued 500 events; the oldest pending age was
3,608 seconds (seed age was one hour). Peak process RSS was 180.1 MiB, sampled
disk use 25.3 MiB and WAL size 11.5 MiB. Production latency, provider timing
and network transport are outside this local process benchmark.

Maintenance embedding backfills, model switches and server warmup also record
remote calls. On a ledger outage, self-hosted installations with no configured
spending guard can still embed. The log records `usage.embedding_unaccounted`,
and those outage calls require provider-side reconciliation. A configured
spending guard defers the call before provider I/O.


### Scheduler lifetime in embedded callers and tests

`ColdPathScheduler.start()` is idempotent while its thread is alive. Call
`stop(timeout=5)` in a `finally` block when the owning test or embedded caller
finishes. It wakes the poll immediately, lets the current worker finish its
transaction cleanup, skips remaining workers, and closes the scheduler's SQLite
connection on its own thread. Check the returned boolean: `False` means an
in-flight worker is still running; join again before releasing its resources.
Stopping cannot forcibly interrupt a provider call. A fully stopped scheduler
can be restarted. The scheduler tests register every started instance for
teardown and assert that no owned thread survives, even after a failed assertion.

### Bounded synthesis cycles

Living syntheses allow at most six provider attempts per scheduled cycle,
including timeouts and errors. Unchanged clusters do not consume an attempt.
The cycle receipt reports `attempted`, `written`, `errors`, and `capped`; capped
means further changed candidates remain for a later cycle. A failed attempt does
not become a successful synthesis or a worker budget deferral. The provider
wrapper disables SDK retries for scheduled work and keeps its existing per-call
timeout; the attempt cap is not a promise of an exact wall-clock deadline.

Discovery also uses point lookups in the existing partial invalidation-target
index for every correlated source/prior check. SQLite requires removal of the
outer column's type affinity (`+e.content_hash`) to seek this JSON expression
index. `INDEXED BY` alone can still scan its entire history. Canonical hashes
remain textual `sha256:` values, and null/missing targets do not invalidate them.
Opening a supported older vault installs the existing index through normal
additive schema initialization. The query change does not rewrite memory.

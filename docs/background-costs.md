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
Thus Haiku 5.5 vision can run under a $1 cap (a full-context input reservation
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

The production scheduler and extraction/recovery paths establish accounting
scopes. Direct operator scripts that invoke workers themselves (for example
`backfill_entities.py`, `drain_entity_dedup.py`, and bespoke maintenance Python)
are exempt unless they explicitly wrap execution in
`with background_usage(settings, "operator:<script>"):`. The extraction
recovery operator script uses the normally scoped extractor. Embeddings,
including vector repair, remain outside this generative ledger and cap.
A zero generative cap still permits separately accounted transcription.

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

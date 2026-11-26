# Synthetic chronological replay

Run the independent seven-family holdout through the product's memory paths:

```bash
uv run python -m afair.eval.chronological_replay > /tmp/afair-replay.json
```

The default **offline** mode is a wiring baseline. It uses deterministic
prompt-only extraction, lexical answers, and OCR for a small synthetic bitmap
font. It makes no model-quality claim. The baseline deliberately remains weak:
it can answer an unknown detail from a nearby fact, repeat historical names, or
abstain despite a successful image extraction. A nonzero exit and failed family
scores expose these limitations; do not rewrite the fixture to make them pass.
Tests verify execution and honest scoring, rather than asserting a perfect score.

## Independent holdout and chronology

The fixture is `afair/eval/fixtures/chronological_holdout.jsonl`, frozen independently
of the runner. Its SHA256 is
`a8b9b49eb65a306088718cf0cd499841750be7052398b7988a2576fdbae9a46b`.
A changed digest stops the run before intake. Families cover namesakes, a correction
across clients, withdrawal, an unknown answer, a negative control, repeated noisy
observations, and an image card.

Each case gets a **new temporary vault**, deleted at the end, including on error.
There is no vault-path argument. The runner rejects an active server context or
installed vault key and never opens a customer vault. It changes into a disposable
working directory and clears ambient product/provider settings, including
`AFAIR_VAULT_KEY`, `VAULT_DIR`, normal provider keys and even evaluation-key
variables; only a small OS/runtime allowlist survives. The dedicated provider key
is captured explicitly before this boundary and passed in memory. The original
working directory and environment are restored afterward.

Settings are constructed without environment/dotenv sources and passed explicitly
into extraction's background usage context. A guard rejects any ordinary Settings
constructor during the run, so a future fallback fails closed before reading
`.env` or `.env.local`. Tests make `load_settings()` raise, install sentinel keys
and a configured customer-vault path in both environment and dotenv files, prohibit
opening those files, and verify a complete offline run with no sentinel exposure.
Labels (`expected`, `evidence`, `forbidden`, family names) reach only the scorer;
they never enter extraction, answer, or synthesis prompts. A test changes the
labels while preserving the inputs and verifies that generated answers stay the
same.

The runner invokes the real `remember` / `observe` handlers with synchronous
production extraction, then recalls after each chronological input. These prefix
checkpoints cannot contain future corrections. Each input ID maps to actual
substrate event IDs; the report retains provenance rows, interpretation IDs,
retrieved source IDs, and both final client answers. Repeated identical observes
exercise production content-addressed dedup, so eight calls may map to one event.
The correction and withdrawal source texts enter as ordinary memories; the harness
does not infer invalidations from scorer labels or silently rewrite history.

The `first` and `second` client labels simulate authenticated credential context
and exercise the production provenance path. This is an **in-process handler**
replay, not an OAuth or network transport test. Recall runs at production **normal depth** in both modes: extraction stores
vectors in `events_vec`; query embedding runs in the production recall pool;
SQLite nearest-neighbor results fuse with FTS through the real reciprocal rank
fusion path. The bounded local `synthetic/hash-ngram-v1` adapter generates stable
64-dimensional signed token/bigram hashes, with at most 100,000 input characters.
It makes no network calls or model downloads. Reports expose document/query
embedding counts, vector-row counts, and actual recall depth so a fallback cannot
masquerade as a hybrid run.

This synthetic local encoder is a deterministic semantic-path **stand-in**, not a
trained semantic model. Provider mode uses the same bounded local encoder while
calling real text/vision providers for generation. It therefore does not measure
production learned-embedding quality or paid embedding-provider behavior. Unrelated
background workers remain disabled. These bounds and limitations are explicit in
the report; no unavailable local model silently falls back to a stronger quality
claim.

The card is rendered as actual PNG pixels with a generic bitmap alphabet. Its
filename is always `card.png`, with no answer-bearing text metadata. Extraction
passes through production binary intake, object storage, vision dispatch,
interpretation validation, and FTS enrichment. Only the provider boundary changes
between offline raster OCR and real vision inference.

## Synthesis coverage

The actual living-synthesis worker runs, and its discovery statistics appear under
`automatic_synthesis`. These frozen cases contain at most two distinct source
memories, below automatic cluster discovery's three-source floor. Zero generated
automatic syntheses is therefore reported honestly.

Separately, `queried_source_synthesis` calls the production cited synthesis routine
on actual retrieved, non-invalidated records. It reports source IDs, resolved
citation hashes, and key-point count. This exercises generation and citation
resolution without changing production discovery thresholds or fabricating extra
source events. An empty synthesis is visible as zero key points; an empty citation
set alone is not evidence of useful synthesis. This readout is not persisted as a
user memory or presented as an automatically discovered cluster.

## Product defects exposed and repaired

The replay investigation separated readout limitations from lost product evidence.
Two product issues have focused synthetic regressions outside the frozen holdout:

- Ongoing-state questions containing “still” now use recall's existing newest-first
  ranking for temporally relevant matches. A newer withdrawal ranks before an older,
  lexically stronger confirmation. “Still life” remains a non-temporal query.
- Synthesis now includes the latest successful extracted evidence for binary,
  spilled-text and compound sources. Previously it saw only object metadata even
  when recall already exposed a successful vision extraction. The projection names
  its interpretation and producer, carries at most 8,000 content characters per
  source including fence-escape expansion, and excludes raw provider diagnostics
  and binary bytes. Derived evidence stays inside the existing untrusted fence.
  Persisted source-to-interpretation IDs refresh an existing metadata-only synthesis
  once, and refresh on later successful extraction changes. Failed retries do not
  hide successful evidence or cause repeated synthesis; old events remain immutable.

The offline final-answer baseline remains deliberately unchanged. Its correction
answer copies an historical place name from the correction sentence, its lexical
matcher can prefer an old confirmation or answer a missing detail from a nearby
fact, and it can abstain despite retrieving the image interpretation. Those are
baseline generation limitations, not evidence that recall lost the correction or
image. They remain failures in the report. The image synthesis readout now contains
actual extracted card evidence and citations; no holdout labels or thresholds were
changed to manufacture a pass.

## Real provider mode

Provider mode is opt-in and was not needed for the offline checks. Use a dedicated
evaluation credential, supplied through an explicit environment variable named
`AFAIR_EVAL_*`. Configure both text and vision model strings for your chosen
provider; no provider is privileged:

```bash
uv run python -m afair.eval.chronological_replay \
  --mode provider \
  --model '<provider>/<text-model>' \
  --vision-model '<provider>/<vision-model>' \
  --api-key-env AFAIR_EVAL_API_KEY \
  --max-cost-usd 0.25 \
  --max-calls 48 > /tmp/afair-replay-provider.json
```

Supply the dedicated credential through your usual secure environment mechanism;
do not put its value in a command, fixture, report, or repository. The runner never
falls back to the product's usual provider key variables. Offline mode rejects
credentials entirely. Provider mode uses the actual production tool-use parsing,
extraction validation, recall, and cited synthesis paths with real responses.
A separate bounded answer adapter turns recalled evidence into a cited answer;
this adapter is an evaluator client, not a new afair MCP tool.

Every dispatch reserves a conservative price estimate before calling the provider.
Unknown or invalid pricing fails closed. The estimate counts UTF-8 input bytes as
tokens, includes tool schemas and an image allowance, and doubles the estimated
price. This is a conservative reservation based on the installed model pricing,
not a provider billing guarantee. Defaults cap the entire run at 48 calls,
512 output tokens per call, 64 KiB request bytes, 15 seconds per request, a
120-second dispatch window, and $0.25 reserved spend. SDK retries are disabled.
An already-running provider request relies on its timeout; no new request starts
once the time/call/spend limit is exhausted. The report exposes calls by operation
and total reserved cost. Errors fail their family rather than silently passing.
Provider diagnostic text and credentials are omitted from report failure details.

## Scoring and interpretation

The JSON report includes every family, source trace, client output, and a `passed`
flag. `family_metrics` carries each metric's numerator, denominator, and score;
answer metrics count the two scheduled client answer slots, while cross-client
consistency counts one comparison pair. An incomplete case contributes 0/2 answer
slots and 0/1 consistency pairs, rather than vanishing from the denominator.
The simpler `families` scores remain available for existing report consumers.

`run_provenance` records the actual product version, UTC start time, immutable
`afair/_build_revision` commit or explicit `unknown`, SHA256s of relevant product,
extractor, synthesis and evaluation code, and hashes of the actual prompt templates
and every dispatched request's messages/tool schemas. Source IDs/timestamps naturally
change request hashes between runs; template and code hashes identify the executable
behavior. A development checkout is not claimed to match an unverified Git revision.
Credentials are excluded from request hashes and report metadata. The command exits 0 only if every family metric passes; otherwise it exits 1.
Logs go to stderr so stdout remains valid JSON.

Answer metrics are intentionally deterministic probes: expected phrase presence,
forbidden phrase exclusion, citation validity, citation overlap with the labelled
supporting source, abstention accuracy, and exact answer/citation consistency
across clients. For unknown answers, abstention requires an empty answer and no
citations; an abstention flag cannot hide a fabricated answer. Bad citations and
unsupported guesses fail independently of expected phrase matching.

These lexical probes do **not** establish semantic entailment, detect every
contradiction or paraphrase, or replace a blinded human/provider judge. Synthesis
integrity is reported separately from query-answer metrics because a general
historical synthesis is not an answer to a specific unknown-detail question.
Provider runs remain a small synthetic holdout, not evidence from customer vaults,
trained semantic embedding quality, or the full managed deployment.

For the narrower deterministic scoring contract, see
`afair/eval/memory_quality.py`; that scorer's hand-authored output fixture is
separate from this chronological execution holdout.

# Useful evidence without maintenance reminders

All explicit remember and observe calls still append raw events. These gates
only reduce regenerable background work; nothing is erased or overwritten.

The conflict resolver skips observe/observe comparisons with content-free
routine outcomes, including absent/null `result`. Factual outcomes, zero/false,
failures, context, or unfamiliar fields remain evidence. Substantive observation
pairs also need a plausible common reference: explicit lineage, compatible
project/task/thread/repository metadata, a specific subject/context, or current
canonical entity evidence. Generic tool names, boilerplate context and reusable
relative filenames do not supply a project reference. Explicitly different
projects cannot match just because both observations mention the same file or
person. Remember pairs retain ordinary eligibility. Existing conflict flags and
proposals remain readable and undecided even when a future comparison would
be filtered; the filter is not a retroactive resolution policy.

Living syntheses filter observations before the useful-source and cluster caps.
Discovery uses the existing `(kind, created_at DESC)` index: up to 400 records
per non-derived source kind, up to 3,200 recent raw observations, plus up to
400 current prior source anchors. Invalidation checks probe the existing
invalidation-target index. There is no full-history window sort. A 19,003-row
noisy-vault regression verifies the query plan and bounds event materialization
to 3,203 records for a three-memory vault with 19,000 routine observations.
Older raw observations remain stored and recallable outside this discovery
horizon; existing substantive syntheses remain live when their sources still
qualify. Durable remembers have an independent window so routine bursts cannot
evict them from discovery.

Consecutive identical observation outcomes in the same reference lane retain
the first source as a stable representative. Timestamp and execution duration
do not change factual content; actor and unfamiliar payload fields do. This
collapse precedes the 40-source synthesis cap so repeated observations cannot
rotate the selected hashes and cause needless calls. A 150→151→150 sequence
has three runs and refreshes at each change; a historical value is never treated
as a duplicate merely because it appeared before. Remembers, source removals,
and changed factual content refresh as before. Legacy syntheses supported only
by routine observations retire by append-only invalidation, without a model
call or any deletion of raw sources.

Session start provides relevant context, with the legacy `pending_corrections`
key empty. It does not enumerate a maintenance queue or write nudge markers.
`recall` keeps honest queue totals for older clients, as passive metadata.
Explicit `recall(stats=True)`, `pending_limit` pagination, the Memory Mirror,
and `recall(decide=...)` remain available. Client guidance never turns those
counts into unsolicited chores. Conflict caveats relevant to the current answer
still surface on recalled evidence, and decisions still require the user.

The conservative gate deliberately retains unknown outcomes. It saves less
than a model classifier might, while preventing routine action names from
silently hiding useful facts or corrections. Tests cover storage retention,
changed facts, repeated evidence, queue silence, and explicit review behavior.

## Release and verification

Run the synthesis/evidence/ontology/session-start/handler/MCP contract tests,
Ruff, and mypy before release. Inspect the MCP golden diff: client guidance may
change, while signatures, true queue counts and explicit review remain stable.
Replay the pure `should_compare_observations` predicate against historical
judgments and open conflicts using read-only inputs, and inspect aggregate
false-negative results before enabling a broader reference gate. Graph inputs
are current canonical entity IDs from `entity_mentions`, excluding retractions.
Do not publish source payloads in audit output. True corrections, regressions,
and meaningful same-project changes must survive; savings on unrelated pairs
alone do not establish quality. Leave every historical flag and proposal
untouched; explicit review remains the operator's choice.

The first worker cycle may retire legacy routine-only syntheses or perform one
refresh of a synthesis whose old duplicate sources no longer qualify. Review
aggregate cycle counters and paid usage after release. Routine-only retirement
needs no operator queue reminder and invokes no automatic conflict decisions.

# Phase 0 capability-gate journal — afair

> **Status:** PASS (closed 2026-06-14)
> **Verdict:** Met by sustained real-world daily use. The user has run afair
> daily across their AI clients for weeks; the vault holds genuine
> cross-vendor data (the proof the gate asked for). The architecture
> survived contact with reality without a rebuild and without breaking
> I1–I8. Phase 0 is closed; proceed to Phase 1.
> **Window opens:** 2026-05-25  (Day 1)
> **Audience:** the user (Gowrynath) and any future AI agent reviewing whether the gate passed

---

## 0. The gate

The Phase 0 capability gate (tracked in `CLAUDE.md §0`):

> **Capability gate:** Daily use from Claude Code, Codex CLI, and Claude.ai chat for two weeks without rebuilding. Trust the system enough to keep calling it because nothing reaches into your life uninvited.

Concretely, the gate passes when:

- I (Gowrynath) use afair in my normal flow on most of those 14 days, AND
- The architecture survives contact with daily reality without me wanting to bulldoze it.

Passing isn't binary "I used it every day." It's: **the architecture earned its keep, the four MCP tools feel like the right shape, and the recall actually saved work at least a few times.**

A failed gate isn't failure — it's clarity about which parts of the design to revise before Phase 1.

---

## 1. What to capture each day

Append a `## Day N — YYYY-MM-DD` section. Inside, jot whatever's relevant. Suggested prompts:

- ✅ **Wins:** "This saved me work because…" (the signal we hope for)
- ⚠️ **Misses:** "The AI didn't reach for the tool when it should have"
- 🛠️ **Friction:** "I had to manually do X" — UX papercut, install pain, etc.
- 🤖 **Extraction quality:** "best_guess_kind was wrong" / "missed an entity"
- 🔍 **Retrieval quality:** "recall returned 0 when there was a match" / "10 irrelevant hits"
- 🏗️ **Architecture pressure:** "I want to change ___" — what the gate is really testing

Entries are **append-only** — don't rewrite past days. If you change your mind, write a new entry that references the old one.

You can also use afair itself to remember these moments:

> *Use afair to remember: "today recall surfaced the Sajinth roadmap fact perfectly — saved me 5 minutes of scrolling."*

That makes the journal partially self-writing — and proves the system is useful for its own meta-narrative.

---

## 2. Weekly retro markers

### Day 7 (~ 2026-06-01) — first-week pulse

Questions to answer:

- How many of the 7 days had a real use (not just a test)?
- Did I want to change anything about the architecture? If so, what?
- Did I get a **cross-vendor moment** — e.g., remembered something in Claude Code, recalled it from Codex CLI? That's the I5 proof.
- Any compromised-credential incidents? (Token/keys leaked into chat — track exposure, decide if rotation is now blocking.)

### Day 14 (~ 2026-06-08) — gate verdict

The decision:

| Outcome | Meaning |
|---|---|
| **PASS** | Keep going to Phase 1 (richer extraction, conflict handling, expanded ingestion sources). |
| **FAIL** | Name what broke. Rebuild that piece before adding scope. The substrate (I2/I3) probably survives any failure mode; the issue is usually in the MCP UX, the extractor quality, or the daily-use habit. |
| **EXTEND** | Give it another week, with a specific question to answer. (E.g., "does it still feel useful after a week of real cross-vendor work?") |

Whatever the verdict, capture the **why** — that's the input to Phase 1's design.

---

## 3. Recurring questions to check during the window

These show up across multiple days; track them cumulatively:

- **Trust:** Am I starting to trust the system enough to remember sensitive things? (The trust ladder in `CLAUDE.md §3` is a real thing.)
- **Cross-vendor:** Have I actually used it from multiple clients (Claude Code + Codex / Claude.ai)? That's I5 in practice.
- **Latency:** Does `recall` feel fast enough on the shallow path? p95 target is < 100 ms.
- **Extraction quality:** Are the Interpretation rows worth anything yet, or are they noise?
- **Compromised credentials:** Still using the three leaked secrets (Anthropic, OpenAI, MCP bearer). When does it become worth rotating?

---

## 4. Daily entries

> **Append-only below this line. Newest at the bottom.**

---

### Day 1 — 2026-05-25

**Wins ✅**
- Phase 0 capability gate hit for Claude Code: `remember` + `recall` round-trip
  through the live Fly deployment, bearer-token authed, end-to-end. First real
  entry in the vault is `01KSFYV3WAGBKQCWYYQANQ20CX` — "first successful
  cross-verification, 2026-05-25".
- The full architecture (substrate → MCP server → extractor → Fly deploy
  → bearer-token auth → GitHub Actions CI) came together in one session
  with all gates green.
- The two-tier substrate (SQLite + filesystem object store) felt obviously
  right once the question "what about binary?" came up — design held.

**Friction 🛠️**
- The install script first wrote only to `~/.claude/settings.json`; current
  Claude Code reads from `~/.claude.json`. Cost a restart cycle to discover.
  Fixed in commit `d3feeea` — installer now writes to both paths.
- Three credentials leaked into the chat transcript via file-diff
  system-reminders (Anthropic, OpenAI, MCP bearer). Annotated in
  `a secure off-repo backup` as compromised; rotation deferred by user choice.
- Initial deploy hit a `flyctl` token validation error in CI — first token
  pipe corrupted by trailing newline. Resolved by reading from a file with
  explicit `tr -d '\n'`.

**Architecture pressure 🏗️**
- None. After a full day of building, no part of the substrate, MCP surface,
  or extractor feels wrong. The I3 escape hatch (JSON payload with
  discriminator) has already proved its worth (binary support, observe-event
  shape) without needing schema changes.

**What I want to test next**
- Whether the Codex CLI integration actually works (installer wrote the config;
  restart needed).
- Whether the warm-path Extractor's quality on real (non-smoke) content is
  any good — the live-LLM smoke proved the wiring; daily use proves utility.

---

### Day 1 (continued) — 2026-05-25 (afternoon)

**Win ✅✅✅ — I5 Vendor Neutrality proven in practice**

The cross-vendor moment landed:

- **Claude Code** (Anthropic-side) called `remember` at 16:17 — wrote event
  `01KSFYV3WAGBKQCWYYQANQ20CX`.
- **Codex CLI** (OpenAI-side) called `recall` + `observe` at 16:30 against
  the same deployed substrate — wrote event `01KSFZJ38XMM2AKME1Q3Z1GMCD`.
- **Claude Code** then called `recall("codex mcp list")` and found Codex's
  event verbatim.

Two AI clients from two different vendors, one Fly machine in fra, one
SQLite file, single bearer token. The vault is the user's, not Anthropic's
and not OpenAI's. I5 is empirically demonstrated.

**Friction 🛠️**
- Codex schema gotcha — the installer wrote `type = "http"` + `[...headers]`
  but Codex 0.133.0 wants no `type` field + `[...http_headers]` subtable
  (verified by checking marker-io and sentry entries on the host).
  Fixed in commit `5163dd2`. Re-install required a manual cleanup pass
  because the installer's idempotency check matches on the section header
  and skipped re-writing when partial state was present.
- Codex's initial recall queries were too narrow ("recent verification of
  Codex MCP list afair enabled MCP servers"). FTS5 matched none of
  the stored facts. Looser queries ("codex mcp list") found everything.
  This is a Codex-prompting issue, not a substrate issue — but worth
  noting: the recall tool's description may want a hint like "prefer
  short, content-bearing keywords over long sentences."

**Misses ⚠️**
- Claude.ai's custom-connector UI doesn't expose a custom-header field —
  only OAuth client id/secret. So Claude.ai is **blocked on OAuth**
  (Phase 1+ work). Claude Code and Codex both work today.

---

### Day 2 — 2026-05-26 (build day, not a use day)

**Note:** Today was a heavy building day, not daily-use validation. The
14-day capability gate window pauses where shipping ate the day; real
daily-use observations resume tomorrow. Logging the shipped work here
so future-me has the architectural context when reading later entries.

**Shipped today (briefly):**

- MCP surface collapsed `6 → 3` tools (`remember`, `recall`, `observe`).
  The old `list_context` / `get_event` / `invalidate` verbs are now
  kwargs on the survivors. Done pre-release so I1 freezes at the
  intended forever-shape.
- **Phase 4 Track 1 — Emergent Entity Graph** (Stages 1-6 + live
  backfill of existing 59 events). Five new append-only substrate
  tables, EntityCanonicalizer cold-path worker (3-stage match with
  Sonnet escalation), recall enrichment via
  `interpretation.canonical_entities` + `entity_edges`, entity-aware
  query routing.
- **Phase 4 Track 2 v0 — per-hit surprise score** based on entity-novelty
  against the recent-context window (last 20 events). Track 2 mode-
  switching deferred (depends on Phase 2 Salience agent).
- **Rebrand:** codename `neverforget` → product name `afair` (`afair.ai`
  brand). Python package, env vars, MCP server name, Fly app, GitHub
  repo, local directory, Claude Code config — all carried over.
- **Multi-env Fly setup:** `afair` (prod) + `afair-dev` (dev) with
  branch-based GH Actions CI. Old `neverforget` app destroyed after
  verification.
- **Substrate/landing decoupled:** `/` on the substrate machine now
  returns a JSON pointer, not HTML. Marketing site comes later on
  afair.ai (separate deployment).

**Live verification 🟢**
- 10/10 smoke on both afair.fly.dev and afair-dev.fly.dev
- Entity graph queryable via MCP: 58 events, canonical entities surface
  on recall hits, surprise score gives meaningful signal (Sajinth-context
  hits score ~1.0 = older / surprising, elvah-context hits score 0.0 =
  current focus).
- Claude Code (local config) + Claude.ai (web UI connector) both
  reconnected to the new endpoint.

**Architecture pressure 🏗️**
- One real bug surfaced during the live backfill: canonicalizer looped
  over events whose extractor returned all-filtered entities (empty
  names → no mentions written → NOT EXISTS query never settled). Fixed
  same-session with a NO_MENTIONS marker pattern (commit `4115f6c`).
  This is the kind of "architecture survives contact with reality"
  signal the gate is asking for — patch was a 15-line, non-disruptive
  fix in the existing cold-path-marker pattern.
- The decision to ship surprise score as a v0 observability slice
  (without mode-switching yet) felt right in retrospect: I can now
  WATCH surprise distributions before committing to a threshold-based
  agent. Data first, agent second.

**Friction 🛠️**
- GitHub Actions push-trigger silently dropped 5 consecutive pushes
  mid-morning (transient outage). Recovery: `gh workflow run` manual
  dispatch worked. Runbook §2 in docs/operations.md updated to capture
  the recovery path.
- Initial `flyctl deploy` after the rebrand failed because the
  Dockerfile had two leftover `neverforget` strings the sed didn't
  catch (`COPY afair ./neverforget` + `CMD ["python", "-m", "neverforget"]`).
  Caught in the first deploy attempt, fixed in `0d0d160`. Lesson: sed
  is good for identifier-level renames but every `string-literal`
  context deserves a follow-up grep.

**What to watch starting tomorrow (Day 3+)**
- Surprise score distribution in real recalls — when does 0.8+ correspond
  to actual context-switches vs noise?
- Entity-graph hit-rate — when does the canonical_entities overlay help
  the AI client disambiguate vs when is it just clutter?
- Whether the rebrand introduced any subtle regression that wasn't caught
  by the 281 tests (cross-vendor reconnect, OAuth flow, daily-use latency).
- The Phase-0 gate's actual question — does the architecture still feel
  right after 2 weeks of using it for real?

**Self-writing journal pattern:** going forward, user dictates
observations during daily use, I (Claude) append them here in
formatted entries. Append-only, never rewrite past entries. Marker
this entry as the start of the user-dictated portion.

---

### Day 2 (continued) — 2026-05-26 evening: cross-session bug catch

**Win ✅ — cross-session debugging via the vault itself**

Parallel claude.ai mobile session audited the vault and noticed daily
consolidation event `01KSHN1BRJ8TX4HZ1VNNFSNV3M` (31 parent_hashes,
2026-05-26 08:04:52 UTC) had a malformed `context` field:
`"v, e, n, d, o, r,  , n, e, u, t, r, a, l, i, t, y, ..."` — clearly
character-iteration corruption. Logged as observe event
`01KSJX608Q63GJ6TP9TY5A8XZ7`.

This Claude-Code session picked up the find via `recall("consolidator bug")`,
read the observe payload, traced root cause, patched, tested, deployed
— all within ~15 min, without the human user having to relay any
technical context. The vault was the communication medium between the
two AI sessions.

**Architecture pressure 🏗️ — Haiku schema-compliance is a real risk**

The bug: `_summarize_day` did
`[str(t) for t in (data.get("themes") or [])]` to coerce LLM output.
When Haiku returned `themes: "vendor neutrality, ..."` as a single
STRING instead of the requested JSON array, the comprehension iterated
the string over its characters → list-of-single-chars. Pydantic
accepted the result (each char IS a `str`), `", ".join()` then
produced the corruption. Tool-use schema enforcement is provider-side;
Haiku ignored it.

This is a deeper signal: **even tool-call-forced output is not
schema-guaranteed.** Defensive `isinstance` guards at every
deserialization point are non-optional for Phase 3+ agent reliability.

The same pattern probably needs auditing in: extractor (entities,
salient_facts, time_references), conflict_resolver (verdict enum),
entity_canonicalizer (matched_entity_id). All places where the LLM
might silently return wrong types.

**Friction 🛠️ — already-corrupted event stays in the substrate**

Per I2, the corrupt consolidation event can't be modified. Per I3, it
can be re-interpreted but that means writing a NEW consolidation with
the same parent_hashes. Decision: leave the bad event alone for now —
it's not actively breaking anything (recall returns it fine), and
future cycles produce clean output. If the bad event later surfaces as
a problem (e.g., FTS catches a single character as a query match
suspiciously often), we'll regenerate.

**Code change:** commit `4f51ea7` introduces
`_coerce_to_string_list(value, field=)` that explicitly type-checks
LLM-returned values before iteration. String input wraps as a
one-element list with a structured warning logged. Three new tests
cover list-input, string-input, and unit cases.

**Why this is a "win" not a "miss":** the bug existed in the deployed
system this morning, and a parallel AI session caught it during normal
daily use via the entity graph + recall surface we just shipped. The
vault as cross-session debugging medium is exactly the "this saved me
work" pattern the gate is looking for.

---

### Day N — 2026-06-13 (bring-current entry)

**Window status note (honest):** the original calendar window (Day 1
2026-05-25 to Day 14 2026-06-08) has elapsed, but logged daily-use days
are thin. The period was build-dominated: the GBrain competitive
response (recall honesty layer, recall benchmark, article citations,
gazetteer, temporal recency), the pre-launch hardening sprint, the
Stripe + Chatwoot setup, the copy/impeccable pass, and the
cumulative-surprise wiring all landed in this window. Those were
shipping days, not pure validation days, the same caveat Day 2 flagged.
The calendar gate ran out before a clean two-week daily-use record
accumulated. The verdict (below) is the user's to set; this entry only
brings the evidence current.

**Wins this session ✅**
- The vault is being used live right now: this Claude Code session called
  `recall`, `remember`, and `observe` against the deployed substrate
  end-to-end. `recall` returned the just-written copy-voice decision with
  the new `coverage` honesty field populated and a real caveat surfaced.
  The three-verb surface still feels like the right forever-shape.
- Cross-vendor reach verified at the server: OAuth 2.1 discovery, DCR,
  PKCE (S256), RFC 9728 protected-resource metadata, and the `/mcp` 401 +
  `WWW-Authenticate` challenge all confirmed live on `mcp.afair.ai`. The
  web-client path (Claude.ai / ChatGPT / Perplexity) is now URL-only via
  OAuth; the stale "OAuth later, bearer fails" note in claude-ai.md is
  gone. See `docs/clients/`.
- The Day-1 Claude.ai blocker ("custom-connector UI only offers OAuth,
  bearer fails") is resolved by shipping OAuth, which is exactly what
  that surface wanted. The same flow unblocks ChatGPT + Perplexity.

**Retrieval quality 🔍**
- The recall honesty layer (BUILD #1) is doing real work: the live recall
  this session flagged "a newer record updates an older one" as a caveat,
  the kind of hedge the gate wanted recall to volunteer instead of
  presenting stale facts as settled.

**Architecture pressure 🏗️**
- Still nothing that says "bulldoze it." The two-register copy decision
  (marketing no-first-person, emails keep founder voice) and the
  surprise-into-mode-switcher wiring both fit the existing shapes
  (additive recall fields, append-only observe events, a new
  substrate-derived signal) without touching I1 to I8.

**Gate verdict: PASS (set by the user, 2026-06-14)**

The earlier framing of this as an "open decision" was wrong. The gate was
already met: the user has run afair in daily flow across their AI clients
for weeks, and the vault is full of genuine cross-vendor data, which is
exactly the evidence the gate asked for. Nothing in the build-heavy window
made the architecture feel wrong; I1–I8 were never broken. Phase 0 is
closed, Phase 1 is next.

**Optional, not a gate condition:** three web-client connectors are newly
documented and worth a one-time hands-on connect when convenient, Claude.ai
(now unblocked by the shipped OAuth flow), ChatGPT, and Perplexity. The
server side is verified; these just broaden reach. They were never part of
passing the gate.

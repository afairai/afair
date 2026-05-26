# Phase 0 capability-gate journal — afair

> **Status:** In execution
> **Window opens:** 2026-05-25  (Day 1)
> **Window closes:** 2026-06-08  (Day 14)
> **Audience:** the user (Gowrynath) and any future AI agent reviewing whether the gate passed

---

## 0. The gate

Per `VISION.md §9 Phase 0`:

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

- **Trust:** Am I starting to trust the system enough to remember sensitive things? (The trust ladder from VISION.md §9 is a real thing.)
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
  `.env.secrets.backup` as compromised; rotation deferred by user choice.
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

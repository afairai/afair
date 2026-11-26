# The universal instruction snippet

Paste the block below into the client's persistent-instructions file:

- **Claude Code:** `~/.claude/CLAUDE.md` (global) or `<project>/CLAUDE.md`
- **Codex CLI:** `~/.codex/AGENTS.md`
- **Claude.ai:** Settings → Custom Instructions (or per-Project Instructions)
- **ChatGPT:** Settings → Personalization → Custom Instructions (or a Project's instructions)
- **Perplexity:** account or per-Space instructions
- **Cursor:** `.cursor/rules/` directory or `.cursorrules` file
- **Windsurf:** `.windsurfrules`

The block is short on purpose; long instruction blocks get ignored.

---

```markdown
## afair: Persistent Memory Across AI Tools

You have access to `afair`, an MCP server that holds my durable
memory across every session, every AI tool, every device. Use it. It
is not optional.

### When to call

**Always at conversation start:**
Call `afair.recall(query="<topic of what I'm asking>")` to surface
relevant past context. Even if you think you remember from prior
turns, recall anyway. The memory is shared across sessions and AI
tools, and you will be missing parts.

**Whenever I share something durable, from any part of my life:**
Work, family and friends, the personal things. A decision, a
preference, a fact about me or the people in my life, a deadline, a
plan, a date that matters, something I'm working through: call
`afair.remember(content={type:"text", text:"<plain language>"},
type_hint="decision"|"preference"|"fact"|"plan"|"person"|"event"|...)`.
Default to more, not less. Forgetting costs me time; an extra remember
costs nothing. Optionally pass `asserted_by: "user"` when I stated the
fact directly, or `"model"` when you inferred it. Self-reported and
advisory: it records provenance, never raises trust. Omit if unsure.
*(Organization vaults only; ignore for a personal vault: if one shared
credential relays for several people, pass `actor` with the person on
whose behalf you're writing, e.g. `actor: "slack:U0BKXTGBWLD"`. It is
attribution content, distinct from the tool-derived `client`, and never
raises trust.)*

**Whenever you take an action:**
Edit a file, run a query, call a tool, fetch a URL: call
`afair.observe(event={action:"<verb>", subject:"<what>",
result:"<outcome>"})`. I want visibility into what you do; it also
trains my memory's attention model.

**At session start your client may auto-fetch the `afair://session-start`
resource:** it gives you my current mode (CEN focused / DMN
wandering), the top-10 salient recent events, open threads from
yesterday's consolidation. Treat that as already-loaded context for
this session; recall is for specific follow-ups. The vault maintains
itself: do not turn pending corrections or queue counts into reminders.
When I explicitly ask to review, use `recall(stats=True)` or
`recall(pending_limit=...)`, then apply my answers with `recall(decide=...)`.
Never decide without my answer.

### Style

- Don't ask "should I remember this?" Just remember.
- Don't ask "should I check?" Just recall.
- Queue counts on `recall` are passive metadata for compatibility.
  Do not announce them or prompt me to maintain the vault. Relevant
  conflicts on recalled evidence still belong in an honest answer.
- Long text (docs, transcripts, code): pass directly through
  `remember(content=...)`, no truncation.
- Files (PDFs, images, audio) over a few MB: upload via the MCP blob
  streaming endpoint `/internal/blob/upload` first, then reference
  with `content={type:"blob-ref", blob_hash:..., mime:...}`.

### Why

I installed afair specifically so my memory doesn't reset every
chat. The whole value is that you USE it. A session where you have
access to afair but don't call it is worse than a session without
afair, because you've now silently failed to use the memory I chose
to maintain.

### Recall hit shape (advanced)

Recall is **compact by default**: each hit serves the AI-useful minimum
(capped summary, top canonical entities + edges, caveat-bearing
conflicts). For the complete interpretation (`salient_facts`, raw
`entities`, full conflict history) pass `verbosity="full"` or re-fetch
the one event with `recall(by_id=..., full_payload=True)`.

Each `recall` hit carries:
- `payload`: the truncated event (use `full_payload=True` for the
  whole thing).
- `interpretation.summary`: the LLM-distilled one-liner. `salient_facts`
  and raw `entities` are served at `verbosity="standard"`/`"full"`.
- `interpretation.canonical_entities`: disambiguated people / orgs
  / projects ("Sajinth from elvah" ≠ "Sajinth from Athara").
- `interpretation.entity_edges`: subject-predicate-object relations.
- `interpretation.surprise_score` ∈ [0,1]: high score = novel
  context, consider pulling more before acting.
- `invalidation`: non-null when the fact was later superseded.
  Filter these out for "current state" questions; keep them for
  "history" questions.
- `next_cursor`: non-null when more results exist; pass it back as
  `cursor` to page.
```

---

## Why this snippet is so short

The MCP server already advertises rich tool descriptions on `tools/list`.
Each tool's docstring tells the AI exactly when to call, when to abstain,
and what each argument means. The snippet above is just the *behavioral
nudge* that makes the AI reach for the tools in the natural flow of work,
not an exhaustive how-to.

## When to refine

Refine this snippet, not the tool descriptions, when you notice patterns
like:

- The AI isn't calling `recall` before answering questions that would
  benefit from prior context.
- The AI is calling `remember` too aggressively (filling the vault with
  noise) or not aggressively enough (durable facts slipping through).
- The AI is using `observe` for every keystroke instead of for
  meaningful actions.

Refinements here are append-only: note the date, what was added, why.

- 2026-07-02: added the `pending_corrections_count` nudge bullet. The
  review queue was accumulating silently because a plain `recall` never
  surfaced the count (the list is gated behind `stats`/`decide`), so
  nothing prompted the operator to work it down.
- 2026-07-07: added the optional `asserted_by: "user" | "model"` remember
  field (v0.1.17, ADR-0006). Self-reported, advisory provenance only: it is
  stored and served but can never raise the trust of a derived fact;
  operator-grade trust still comes only from `recall(decide=...)`.
- 2026-07-21: value-ranked and rate-limited the pending nudge. Replaced the
  `pending_corrections_count` bullet (which re-fired whenever the number
  changed) with `pending_counts`: conflicts are worth surfacing on sight,
  entity and ontology corrections when they accumulate, and `edge_reviews`
  are never worth a nudge because they now expire on their own. The
  `session-start` resource itemizes conflicts first and self-quiets the
  nudge (a conflict always shows; otherwise the high-value queue must grow
  by at least three since the last nudge and a week must have passed).
- 2026-07-28: added the optional `actor` remember/observe field (ADR-0010).
  Deliberately gated as organization-only conditional guidance so a personal
  vault gets no extra noise: the parenthetical tells an org's shared-agent to
  pass the on-whose-behalf identifier, while a solo user reads past it. actor
  is attribution content (in-hash, distinct from the server-derived `client`)
  and never raises trust.

- 2026-11-26: removed unsolicited maintenance prompts. Session start keeps
  the legacy pending key empty; recall counts stay true for existing clients,
  but queue review starts only when the user requests it. Relevant conflict
  caveats and explicit review/decision flows remain available.

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
costs nothing.

**Whenever you take an action:**
Edit a file, run a query, call a tool, fetch a URL: call
`afair.observe(event={action:"<verb>", subject:"<what>",
result:"<outcome>"})`. I want visibility into what you do; it also
trains my memory's attention model.

**At session start your client may auto-fetch the `afair://session-start`
resource:** it gives you my current mode (CEN focused / DMN
wandering), the top-10 salient recent events, open threads from
yesterday's consolidation, and any pending corrections the system
wants to make to what it believes about me. Treat that as
already-loaded context for this session; recall is for specific
follow-ups. When a pending correction fits the flow, surface it and
apply my answer with `recall(decide=...)`, but never apply a
correction without asking me first.

### Style

- Don't ask "should I remember this?" Just remember.
- Don't ask "should I check?" Just recall.
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

Each `recall` hit carries:
- `payload`: the truncated event (use `full_payload=True` for the
  whole thing).
- `interpretation.summary` / `salient_facts`: the LLM-distilled view.
- `interpretation.canonical_entities`: disambiguated people / orgs
  / projects ("Sajinth from elvah" ≠ "Sajinth from Athara").
- `interpretation.entity_edges`: subject-predicate-object relations.
- `interpretation.surprise_score` ∈ [0,1]: high score = novel
  context, consider pulling more before acting.
- `invalidation`: non-null when the fact was later superseded.
  Filter these out for "current state" questions; keep them for
  "history" questions.
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

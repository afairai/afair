# The universal instruction snippet

Paste the block below into the client's persistent-instructions file:

- **Claude Code:** `~/.claude/CLAUDE.md` (global) or `<project>/CLAUDE.md`
- **Codex CLI:** `~/.codex/AGENTS.md`
- **Claude.ai:** Settings → Custom Instructions (or per-Project Instructions)
- **Cursor:** `.cursor/rules/` directory or `.cursorrules` file
- **Windsurf:** `.windsurfrules`

The block is short on purpose — long instruction blocks get ignored.

---

```markdown
## neverforget MCP

You have access to a `neverforget` MCP server providing persistent memory
across sessions, AI clients, and devices. Use it daily:

1. **Before answering** questions that benefit from history (preferences,
   past decisions, names, ongoing projects, recurring themes), call
   `recall(query)` first.
2. **When the user signals** save/remember/note/keep, call `remember()`.
   Also call it proactively for durable facts the user has just shared
   that should outlive this conversation.
3. **After completing significant work** (a fix, a feature, a decision,
   a deployment), call `observe()` to log what happened so future
   sessions know.
4. **At session start** for unfamiliar contexts, call `list_context(about)`
   to see what is already known about a subject.

Be a thoughtful librarian. Save signal, not noise. The substrate is the
user's vault, not yours.
```

---

## Why this snippet is so short

The MCP server already advertises rich tool descriptions on `tools/list` —
each tool's docstring tells the AI exactly when to call, when to abstain,
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

Refinements here are append-only — note the date, what was added, why.

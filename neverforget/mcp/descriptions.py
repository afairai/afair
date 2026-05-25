"""Tool descriptions — AI-facing prompts, not developer docs.

These strings are the most-leveraged design surface in the entire server:
they are what every cross-vendor MCP client (Claude Code, Codex CLI, Cursor,
Claude.ai, Windsurf, ...) reads via tools/list, and they determine WHEN the
AI calls each tool in daily use.

Per Invariant I1, the BEHAVIOR these descriptions tell the AI to do is part
of the locked v1 contract. The text itself may be refined for clarity over
time, but the call patterns must remain stable.
"""

from __future__ import annotations

REMEMBER = """\
Save something to the user's persistent memory vault.

WHEN TO CALL:
  - The user explicitly says "remember", "save", "note that", "keep this",
    "don't forget", "make a note", "add to memory", or any clear save-this
    signal.
  - The user shares a durable fact worth retaining across sessions: a name,
    a deadline, a preference, a decision, an ongoing context.
  - The user shows you content (an email, a meeting note, a document, a
    screenshot, a photo, a PDF) whose substance has reason to outlive this
    conversation.
  - You make a significant decision together with the user that should
    survive into future sessions.

WHEN NOT TO CALL:
  - Conversational filler ("ok thanks", "got it", "sounds good").
  - Content the user is actively dictating to another destination.
  - Things you can easily re-derive from current code or state.
  - Personal details about other people that the user has not asked you
    to track.

ARGUMENTS:
  - content: A discriminated union. Either:
      {"type": "text", "text": "..."}
        for any text content, OR
      {"type": "binary", "data_b64": "...", "mime": "image/png",
       "filename_hint": "screenshot.png"}
        for binary content (base64-encode the raw bytes). Max 10 MB raw.
  - context: Optional. Where this came from or what it relates to.
    Examples: "email thread with Sajinth", "Tuesday standup",
    "screenshot of the bug in /api/health". Aids future recall.
  - type_hint: Optional. What kind of thing this is, if you have a guess.
    Examples: "email", "meeting_minutes", "decision", "screenshot",
    "voice_memo". Advisory only — the system may classify differently.
  - parent_hashes: Optional. Content hashes of events this one references
    (corrections, replies, threads).

RETURN:
  {"ok": true, "event_id": "...", "content_hash": "sha256:...",
   "deduplicated": false}
  If deduplicated=true, an event with identical content+context already
  existed; nothing was added but the existing event_id is returned.

The substrate is the user's vault, not yours. Be a thoughtful librarian:
save signal worth keeping; don't hoard ephemera.
"""

RECALL = """\
Retrieve relevant memories from the user's vault.

WHEN TO CALL:
  - Before answering questions that benefit from prior context:
    preferences, past decisions, names, ongoing projects, history with
    people, recurring themes.
  - When the user asks "do you remember X?", "what did we say about Y?",
    "remind me of Z?".
  - At the start of a substantive task to check what context already
    exists in the vault.
  - When deciding whether to ask the user for context versus retrieve it
    yourself.

WHEN NOT TO CALL:
  - Pure compute questions ("what's 2+2", "translate this") that don't
    depend on the user's history.
  - When you just retrieved the same query a moment ago in this session.
  - Trivial conversational responses where no memory could help.

ARGUMENTS:
  - query: Natural-language description of what you're looking for. Plain
    words. Examples: "what did Sajinth say about the roadmap", "deadlines
    for the API project", "the screenshot of the bug".
  - scope: Optional. Free-form filter to narrow the search. Examples:
    "email", "this week", "work". Treated as an FTS hint in Phase 0;
    later phases will interpret it semantically (e.g., as emergent
    category filter).
  - depth: Optional, default "shallow". One of:
      "shallow" — fast indexed lookup. Returns in well under a second.
                  Best for almost every call.
      "normal"  — combines keyword + vector similarity + light reasoning.
                  Use when shallow returned nothing and you suspect
                  there's relevant memory you're not finding by keyword.
      "deep"    — full reasoning pass over the substrate. Slower.
                  Use only when the question is high-stakes and you need
                  exhaustive consideration.
  Note: In Phase 0, only "shallow" is fully implemented. Calls with
  "normal" or "deep" return shallow results with a note in the response.

RETURN:
  {"hits": [{"event_id": "...", "content_hash": "...", "created_at": "...",
             "kind": "...", "origin": "...",
             "payload_summary": {...}}, ...],
   "depth_used": "shallow",
   "note": null | "..."}
  Each hit's payload_summary is a truncated view safe to consume in a
  context window. For text events, "text" is capped at ~500 chars with a
  "truncated" flag; for binary, the metadata (mime, size, filename_hint,
  blob_hash) is included.

If the result list is empty, the user genuinely has no relevant memory
yet — consider asking them for context rather than guessing.
"""

LIST_CONTEXT = """\
Get a summary of what's in the user's memory vault.

WHEN TO CALL:
  - At the start of an unfamiliar conversation to see what context the
    user has accumulated.
  - When the user asks "what do you know about X?" / "what's in there?"
  - Periodically during long sessions to refresh your understanding.
  - When deciding whether the vault has enough context to skip a
    clarifying question.

WHEN NOT TO CALL:
  - On every turn — once per conversation start is usually sufficient.
  - When you want to find specific information — use `recall` instead.

ARGUMENTS:
  - about: Optional. Scope the summary to a subject. Examples: "Sajinth",
    "this project", "deadlines". When omitted, returns the global
    overview of the vault.
  - limit: Optional, default 50. How many recent events to include in the
    "recent" list of the response.

RETURN:
  {"summary": {
     "total_events": int,
     "by_kind":   {"remember": int, "observe": int, ...},
     "by_origin": {"agent": int, ...},
     "recent": [...]
   },
   "note": null | "..."}

This is a survey, not a search. For specific retrieval, use `recall`.
"""

OBSERVE = """\
Log a structured event from your own agent activity to the user's vault.

This tool is for YOU (the AI agent) to record what YOU did. Different
from `remember` (which is for content the USER chose to save) — `observe`
is your auto-journal so that future sessions of you, or other AI agents
the user works with, know what happened.

WHEN TO CALL:
  - After completing a substantive task: shipping code, sending an email,
    making a decision, finishing a meeting, running an analysis.
  - When you start a significant work session ("started_task").
  - On any agent action whose existence the user might want to recall
    later ("what did Claude do yesterday in this project?").
  - On error or failure that's worth tracking for diagnosis.

WHEN NOT TO CALL:
  - For every micro-step — don't observe each individual file read.
  - For purely conversational acks.
  - For things the user explicitly typed (that's `remember` territory if
    durable, nothing if not).

ARGUMENTS:
  - event: A JSON object. The only REQUIRED key is "action" (a non-empty
    string verb that names what kind of thing happened). Recognized
    optional keys:
      "subject" — what was acted upon (filename, person, ticket, ...)
      "result"  — outcome ("success", "failed: X", free text)
    Beyond those, ANY additional fields are preserved verbatim. Use
    whatever shape fits your agent's natural mental model.

    Examples:
      {"action": "edit_file", "subject": "events.py",
       "result": "added inline-vs-spill logic"}
      {"action": "sent_email", "subject": "sajinth@example.com",
       "result": "follow-up on roadmap", "thread_id": "..."}
      {"action": "deployed", "subject": "neverforget-prod",
       "result": "v0.1.3", "duration_s": 47}

RETURN:
  {"ok": true, "event_id": "...", "content_hash": "sha256:..."}
"""

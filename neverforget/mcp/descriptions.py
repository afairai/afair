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
  - depth: Optional, default "auto" (Phase 2+). One of:
      "auto"    — system picks based on query shape. Exact identifiers
                  (sha256:..., URLs, ULIDs) and single-token queries go
                  to shallow; multi-token natural language goes to
                  normal hybrid. Recommended default — you almost never
                  need to override.
      "shallow" — FTS5 keyword search only. No embedding inference.
                  Cheapest (~10ms). Use for exact-match scenarios that
                  auto might miss.
      "normal"  — Hybrid FTS5 + vector recall via Reciprocal Rank
                  Fusion. Catches semantic matches that share no
                  tokens with the query. ~120ms latency since the
                  embedding inference is local (FastEmbed in-process).
      "deep"    — Same as normal today; reserved for the Phase 3+
                  reasoning agent. Returns a note if you request it.

  The returned ``depth_used`` field tells you which path actually ran
  — useful when ``auto`` resolves to one direction or the other.

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

NEED THE FULL CONTENT of one specific hit (e.g., the user said "show me
the whole document" or "read me section 3")? Call ``get_event`` with the
``event_id`` from the recall hit — it returns the untruncated payload.
"""


INVALIDATE = """\
Mark a previously-recorded fact as superseded by later evidence.

WHEN TO CALL:
  - The user explicitly corrects an earlier statement ("actually Sajinth
    is now CTO, not CEO" / "ignore what I said about X yesterday").
  - You discover a contradiction between an old memory and a new one
    the user is sharing now, AND the new one is meant to replace the
    old one rather than coexist with it.
  - A meeting outcome supersedes an earlier plan; a launched product
    name replaces a working title; a deal closed at terms different
    from the initial discussion.

WHEN NOT TO CALL:
  - The user adds NEW information without contradicting old (use
    ``remember`` — facts accumulate by default).
  - You're uncertain whether the old fact is genuinely wrong; ask the
    user first. Invalidation is for explicit supersessions.
  - The target event doesn't exist or is itself an invalidation event
    (the call will be rejected).

ARGUMENTS:
  - target_hash: ``sha256:...`` content_hash of the event being
    superseded. Find it via the ``content_hash`` field returned from
    ``recall``, ``observe``, or ``remember``.
  - reason: Optional free-text explanation. Helpful for audit and for
    future recall ("we ignored this fact because…").

RETURN:
  {"ok": true,
   "event_id": "01K...",          // ULID of the invalidation event itself
   "content_hash": "sha256:...",  // the invalidation event's own hash
   "target_hash": "sha256:...",   // what was invalidated
   "target_already_invalidated": false}  // true if prior invalidation existed

WHAT THIS DOES:
  Appends a new event to the substrate (kind=invalidate) referencing
  the target. The target event is NEVER touched — substrate is
  append-only by design (Invariant I2). Subsequent ``recall`` calls
  still return the target as a hit, but each hit now carries an
  ``invalidation`` field with the timestamp + reason. AI clients
  decide: for current-state questions prefer hits where
  ``invalidation is null``; for historical/audit questions, all
  hits remain relevant context.

  Bi-temporal queries ("what was true at point T") work for free —
  events with ``created_at <= T`` and either no invalidation or
  ``invalidation.at > T`` were valid at T.
"""


GET_EVENT = """\
Return the FULL untruncated payload for one specific event.

WHEN TO CALL:
  - After a ``recall`` hit when the user asked for the whole document
    or a specific portion that the 500-char preview cut off ("show me
    the whole VISION.md", "what does section 6 say", "paste the full
    email").
  - When the AI needs verbatim text — quoting, summarizing a specific
    document, comparing two events word-for-word.
  - Programmatically, when a follow-up tool call needs the complete
    payload (e.g., re-encoding for another vendor).

WHEN NOT TO CALL:
  - Skim-style "what's in the vault?" queries — that's ``recall`` or
    ``list_context`` with their built-in truncation.
  - The user just wants a one-line summary — the recall hit's
    ``interpretation.summary`` already has that.

ARGUMENTS (provide exactly one):
  - event_id: ULID returned by ``recall``/``observe``/``remember``.
  - content_hash: sha256-prefixed hash returned alongside event_id.

RETURN:
  {"event_id": "...",
   "content_hash": "sha256:...",
   "created_at": "...",
   "kind": "remember" | "observe",
   "origin": "...",
   "payload": {<FULL payload — text-large blobs are inlined as "text">},
   "interpretation": {<latest extractor output>} | null,
   "linked_event_ids": [...],
   "parent_hashes": [...]}

  For "text" and "text-large" events, payload.text contains the entire
  content (could be many KB). For "binary" events, the bytes themselves
  remain in the object store; the payload metadata (mime, size,
  filename_hint, blob_hash) is returned. Use a future ``read_blob`` tool
  to fetch raw bytes.

If neither selector matches, ``get_event`` raises an error — same shape
as any other tool failure in MCP. Provide exactly one selector; passing
both or neither is rejected.
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

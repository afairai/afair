"""Tool descriptions — AI-facing prompts, not developer docs.

These strings are the most-leveraged design surface in the entire server:
they are what every cross-vendor MCP client (Claude Code, Codex CLI, Cursor,
Claude.ai, Windsurf, ...) reads via tools/list, and they determine WHEN the
AI calls each tool in daily use.

Per Invariant I1, the BEHAVIOR these descriptions tell the AI to do is part
of the locked v1 contract. The text itself may be refined for clarity over
time, but the call patterns must remain stable.

Three tools, three verbs, three forever signatures:

  remember  →  write  (with optional supersession via ``invalidates``)
  recall    →  read   (search / by-id / stats — one verb, many modes)
  observe   →  log    (agent self-journal)
"""

from __future__ import annotations

REMEMBER = """\
Save something to the user's persistent memory vault. Optionally
supersede prior facts that are no longer true.

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
  - The user corrects an earlier fact ("actually Sajinth is at Athara,
    not elvah"). Write the new fact AND pass the old event's content_hash
    in ``invalidates`` to mark it superseded.

WHEN NOT TO CALL:
  - Conversational filler ("ok thanks", "got it", "sounds good").
  - Content the user is actively dictating to another destination.
  - Things you can easily re-derive from current code or state.
  - Personal details about other people that the user has not asked you
    to track.

ARGUMENTS:
  - content: A discriminated union. Either:
      {"type": "text", "text": "..."}                          for any text, OR
      {"type": "binary", "data_b64": "...", "mime": "image/png",
       "filename_hint": "screenshot.png"}                       for binary.
    Max 10 MB raw bytes.
  - context: Optional. Where this came from or what it relates to.
    Examples: "email thread with Sajinth", "Tuesday standup",
    "screenshot of the bug in /api/health". Aids future recall.
  - type_hint: Optional. What kind of thing this is, if you have a guess.
    Examples: "email", "meeting_minutes", "decision", "screenshot".
    Advisory only — the system may classify differently.
  - parent_hashes: Optional. Content hashes of events this one references
    (corrections, replies, threads).
  - invalidates: Optional. List of content_hashes that this new fact
    supersedes. Each target gets its own append-only invalidation event
    referencing it. Use when the user corrects a prior fact or a meeting
    outcome supersedes an earlier plan.

RETURN:
  {"ok": true, "event_id": "...", "content_hash": "sha256:...",
   "deduplicated": false, "invalidated": ["sha256:...", ...]}

  - deduplicated=true means an event with identical content+context already
    existed; nothing was added but the existing event_id is returned.
  - invalidated lists the content_hashes that were marked superseded in
    this call.

The substrate is the user's vault, not yours. Be a thoughtful librarian:
save signal worth keeping; don't hoard ephemera.
"""


RECALL = """\
Read the user's memory vault. One verb, multiple call modes.

WHEN TO CALL:
  - Before answering questions that benefit from prior context: preferences,
    past decisions, names, ongoing projects, history with people, recurring
    themes.
  - When the user asks "do you remember X?", "what did we say about Y?",
    "remind me of Z?".
  - When the user wants the FULL content of a specific event ("show me the
    whole document"): use ``by_id`` or ``by_content_hash`` with
    ``full_payload=True``.
  - When you want a snapshot of the vault's contents ("what's in there?"):
    use ``stats=True``.
  - At the start of a substantive task to check what context already
    exists.

WHEN NOT TO CALL:
  - Pure compute questions ("what's 2+2", "translate this") that don't
    depend on the user's history.
  - When you just retrieved the same query a moment ago in this session.
  - Trivial conversational responses where no memory could help.

ARGUMENTS (all optional; combine as needed):
  - query: Natural-language search. Examples: "what did Sajinth say
    about the roadmap", "deadlines for the API project".
  - by_id: ULID of one specific event. Returns that event in full.
    Use after a prior recall hit when you need the whole content.
  - by_content_hash: sha256-prefixed hash of one specific event.
    Same lookup semantics as by_id.
  - scope: Optional substring filter. Reserved — currently no-op until
    Phase 3.5 emergent context detection lands.
  - depth: One of "auto" (default), "shallow", "normal", "deep".
      "auto"    → system picks based on query shape (identifiers and
                  single tokens → shallow; multi-token natural language
                  → normal hybrid). Recommended default.
      "shallow" → FTS5 keyword only. Cheapest.
      "normal"  → Hybrid FTS5 + vector. Local embedding inference, ~120ms.
      "deep"    → Same as normal today; reserved for Phase 3+ reasoning.
  - limit: Default 20. Max hits to return.
  - full_payload: Default false. When true, each hit's payload is the full
    untruncated content (for text-large events, the blob is read back into
    text). When false, text payloads are clipped at ~500 chars.
    Lookup modes (by_id, by_content_hash) imply full_payload=true.
  - stats: Default false. When true, the response includes a
    ``summary`` field with total_events, by_kind, by_origin counts —
    useful for "what's the lay of the land" queries. Combines with any
    other mode.

RETURN:
  {"hits": [{"event_id": "...", "content_hash": "...", "created_at": "...",
             "kind": "...", "origin": "...",
             "payload": {...}, "truncated": bool,
             "interpretation": {...} | null,
             "linked_event_ids": [...],
             "parent_hashes": [...],
             "invalidation": {...} | null,
             "conflicts": [...]}],
   "depth_used": "shallow" | "normal" | "deep",
   "note": null | "...",
   "summary": null | {total_events, by_kind, by_origin}}

Each hit's payload is either the truncated summary or the full content,
depending on the full_payload flag (and lookup mode). ``truncated`` tells
you which form you got.

If hits is empty for a query, the user genuinely has no relevant memory
yet — consider asking them for context rather than guessing.

If ``invalidation`` is non-null on a hit, the fact was marked superseded
by a later event. For current-state questions, prefer hits where
invalidation is null. For historical questions, treat all hits as
relevant context.
"""


OBSERVE = """\
Log a structured event from your own agent activity to the user's vault.

This tool is for YOU (the AI agent) to record what YOU did. Different
from ``remember`` (which is for content the USER chose to save) — ``observe``
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
  - For things the user explicitly typed (that's ``remember`` territory if
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
      {"action": "deployed", "subject": "afair-prod",
       "result": "v0.1.3", "duration_s": 47}

RETURN:
  {"ok": true, "event_id": "...", "content_hash": "sha256:..."}
"""

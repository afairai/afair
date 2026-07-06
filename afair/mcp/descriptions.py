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

SERVER_INSTRUCTIONS = """\
afair is the user's persistent memory layer, a chosen replacement for
the short, vendor-locked, vanishing memory every chat tool ships with
by default. The user installed this so their context can travel across
sessions, across AI tools, across years.

YOUR PROTOCOL:

1. At the START of every conversation, call ``afair.recall(query=...)``
   with the gist of what the user is asking, or with the topic shape
   if they haven't asked anything concrete yet. Treat this as your
   context refresh; the user does not want to repeat themselves.

2. When the user shares anything durable, from any part of their life:
   work, family and friends, the personal things, call
   ``afair.remember(content={type:'text', text:...},
   type_hint='decision'|'preference'|'fact'|'plan'|...)``. A work
   decision or deadline counts; so does a friend's kid's name, a food
   preference, an anniversary, a worry they keep returning to. Default
   to remembering more, not less. The cost of forgetting a memory is
   much higher than the cost of an extra append-only row.

3. When YOU take an agent action (edit a file, run a query, fetch a URL,
   call another tool) call ``afair.observe(event={action, subject,
   result, ...extras})``. The user wants visibility into what their AI
   does, and the salience-and-mode-switching agents read observe events
   to keep attention routing in good shape.

Don't ask "should I save this?" Just save it. Don't ask "should I check
first?" Just check. afair is not optional infrastructure; it's the
memory the user paid for.

The three verbs (recall, remember, observe) are frozen v1. They will
not change. Lean into them.
"""


REMEMBER = """\
Save something to the user's persistent memory vault, afair, the
substrate that travels across their sessions, AI tools, and years.
Use it generously.

The user explicitly installed afair so their context doesn't reset.
If a fact crosses your attention and looks even slightly worth more
than the current message, save it. The cost of forgetting is the
user re-explaining themselves next session; the cost of an extra
remember is one append-only row that dedupes if identical.

WHEN TO CALL:
  - The user explicitly says "remember", "save", "note that", "keep this",
    "don't forget", "make a note", "add to memory", or any clear save-this
    signal.
  - The user shares a durable fact worth retaining across sessions, from
    any part of life: a work decision or deadline, a colleague's role, a
    friend's or family member's name and what matters to them, a birthday
    or anniversary, a preference (food, travel, how they like to work), a
    personal goal, something they are working through.
  - The user shows you content (an email, a meeting note, a document, a
    screenshot, a photo, a PDF, an audio clip) whose substance has reason
    to outlive this conversation.
  - You make a significant decision together with the user that should
    survive into future sessions.
  - The user corrects an earlier fact ("actually Sajinth is at Athara,
    not elvah"). Write the new fact AND pass the old event's content_hash
    in ``invalidates`` to mark it superseded.

DEFAULT: when in doubt, remember. Don't ask for permission. Don't
narrate "I'll remember this for you." Just call it.

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
    Max 10 MB raw bytes. A JSON-string-serialized object (the same shape
    sent as a string) is also accepted and parsed, and a bare string is
    stored as text — the write is never rejected on shape.
  - context: Optional. Where this came from or what it relates to.
    Examples: "email thread with Sajinth", "Tuesday standup",
    "dinner with Mara", "Mum's birthday weekend". Aids future
    recall.
  - type_hint: Optional. What kind of thing this is, if you have a guess.
    Examples: "email", "meeting_minutes", "decision", "screenshot".
    Advisory only. The system may classify differently.
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
Read the user's memory vault, afair, the persistent substrate they
share across every session and every AI tool. CALL THIS BEFORE you
respond to anything where the user's history might be relevant. Always.

The user installed afair so their context doesn't have to be repeated
to every new conversation. A session where you have access to afair
and don't call it is worse than a session without afair, because
you're silently failing to use the memory they chose to maintain.

WHEN TO CALL:
  - At the START of every substantive task. Don't ask "do you want
    me to check?" Just check. Recall is cheap; missing context isn't.
  - Before answering questions that benefit from prior context:
    preferences, past decisions, names, ongoing projects, history with
    people (at work and outside it), important dates, recurring themes,
    deadlines, commitments.
  - When the user asks "do you remember X?", "what did we say about Y?",
    "remind me of Z?".
  - When the user wants the FULL content of a specific event ("show me the
    whole document"): use ``by_id`` or ``by_content_hash`` with
    ``full_payload=True``.
  - When you want a snapshot of the vault's contents ("what's in there?"):
    use ``stats=True``.
  - On topic shifts mid-conversation. New topic = fresh recall.

WHEN NOT TO CALL:
  - Pure compute questions ("what's 2+2", "translate this") that don't
    depend on the user's history.
  - When you just retrieved the same query a moment ago in this session.
  - Trivial conversational responses where no memory could help.

ARGUMENTS (all optional; combine as needed):
  - query: Natural-language search. Examples: "what did Sajinth say
    about the roadmap", "deadlines for the API project", "what does Mara
    like to drink", "when is my sister's birthday".
  - by_id: ULID of one specific event. Returns that event in full.
    Use after a prior recall hit when you need the whole content.
  - by_content_hash: sha256-prefixed hash of one specific event.
    Same lookup semantics as by_id.
  - scope: Optional substring filter. Reserved, currently no-op until
    Phase 3.5 emergent context detection lands.
  - depth: One of "auto" (default), "shallow", "normal", "deep".
      "auto"    → system picks based on query shape (identifiers and
                  single tokens → shallow; multi-token natural language
                  → normal hybrid). Recommended default.
      "shallow" → FTS5 keyword only. Cheapest.
      "normal"  → Hybrid FTS5 + vector. Local embedding inference, ~120ms.
      "deep"    → Hybrid like normal, but the flat history lens: temporal
                  relevance decay is OFF, so past-dated and superseded
                  memories rank by match strength alone. Use for history /
                  as-of questions ("what did I know back then", "show me past
                  appointments"). Default recall instead de-prioritizes
                  memories whose moment has passed, without dropping them.
  - limit: Max hits to return. Omitted → 10 in compact verbosity, 20
    otherwise. Server cap 100 (larger values are clamped, not rejected).
  - verbosity: "compact" (default), "standard", or "full". Controls how
    much of each hit's interpretation/conflicts/linked-list detail is
    served — NOT the payload (see full_payload).
      "compact" → the AI-useful minimum: capped summary + payload text,
                  top canonical entities and edges, only the top
                  caveat-bearing conflicts. Use this by default.
      "standard"→ the full interpretation minus the redundant raw entity
                  list and null edge validity bounds.
      "full"    → every field. Use standard/full, or by_id + full_payload,
                  when you need salient_facts / raw entities / the complete
                  conflict history. by_id/by_content_hash always serve full.
  - cursor: Opaque paging token for search/browse. Pass the ``next_cursor``
    from a prior recall back here verbatim to get the next page. Best-effort:
    rankings are recomputed per call. A bad cursor serves page 1 with a note.
  - full_payload: Default false. When true, each hit's payload is the full
    untruncated content (for text-large events, the blob is read back into
    text). When false, text payloads are clipped (~500 chars standard/full,
    ~300 compact). Lookup modes (by_id, by_content_hash) imply full_payload=true.
  - stats: Default false. When true, the response includes a
    ``summary`` field with total_events, by_kind, by_origin counts,
    useful for "what's the lay of the land" queries. Combines with any
    other mode.
  - feedback: Optional dict reporting on hits from a PRIOR recall.
    Shape:
        {"useful_event_ids": [event_id, ...],
         "not_useful_event_ids": [event_id, ...],
         "missing_topic": "free-text note if expected content was absent"}
    This is the signal the self-improvement tuner reads to validate
    parameter changes (sharper salience, smarter mode-switching,
    better surprise calibration). After a recall, the NEXT time you
    call recall, include feedback referring to the prior hits. Don't
    ask whether to. Just send it. Empty payload is a no-op.
  - decide: Confirm/reject pending review proposals. Accepts a SINGLE
    decision or a LIST of up to 50 (batch-drain the queue in one call).
    Each: {"proposal_id": "...", "verdict": "confirm"|"reject"|"retract",
    "to_kind": "..."}. The per-decision outcomes come back in
    ``decisions`` (see RETURN). A bad decision in a batch is reported as
    that item's outcome (status "error"); the rest still apply.
  - pending_limit / pending_offset: Page the review queue. pending_limit
    (default 20, server cap 200) sets the page size; pending_offset skips
    that many rows. Passing pending_limit alone includes the list even
    without stats=True. While DRAINING the queue, decide a page then
    re-fetch at pending_offset=0 — deciding removes rows from the open
    set, so advancing the offset would skip the new head.

RETURN:
  {"hits": [{"event_id": "...", "content_hash": "...", "created_at": "...",
             "kind": "...", "origin": "...",
             "payload": {...}, "truncated": bool,
             "interpretation": {...} | null,
             "linked_event_ids": [...],
             "parent_hashes": [...],
             "invalidation": {...} | null,
             "conflicts": [...],
             "client": null | "..."}],
   "depth_used": "shallow" | "normal" | "deep",
   "note": null | "...",
   "summary": null | {total_events, by_kind, by_origin, by_client},
   "decisions": [{"proposal_id": "...", "status": "...", "note": "..."}, ...],
   "next_cursor": null | "..."}

``client`` on a hit is the AI tool that wrote the event, derived
server-side from the writing credential (not something the caller set).
It is null for events written before provenance existed, and is served at
verbosity "standard"/"full" and on by_id/by_content_hash lookups. The
``summary.by_client`` map (on stats=True) counts events per writing client
— a different axis from by_origin, useful for "which tools have touched
this vault".

``decisions`` is populated only when this call carried ``decide=`` — one
outcome per decision sent, in order (empty otherwise). ``next_cursor`` is
non-null when a next page is reachable; pass it back verbatim as ``cursor``.
It is null once the pageable window is exhausted OR capped (the server bounds
how deep paging can go — at that edge a note says the window was capped, so
a client paging "until next_cursor is None" always terminates).

Each hit's payload is either the truncated summary or the full content,
depending on the full_payload flag (and lookup mode). ``truncated`` tells
you which form you got.

If hits is empty for a query, the user genuinely has no relevant memory
yet. Consider asking them for context rather than guessing.

If ``invalidation`` is non-null on a hit, the fact was marked superseded
by a later event. For current-state questions, prefer hits where
invalidation is null. For historical questions, treat all hits as
relevant context.
"""


OBSERVE = """\
Log a structured event from your own agent activity to the user's vault.

This tool is for YOU (the AI agent) to record what YOU did. Different
from ``remember`` (which is for content the USER chose to save). ``observe``
is your auto-journal so that future sessions of you, or other AI agents
the user works with, know what happened. The user wants visibility
into what their AI does, partly so they can audit, partly so the
next session has continuity.

Default to verbose observation. The user's salience worker and
mode-switcher read observe events to decide attention state; richer
observe data leads to better cognitive routing on subsequent recalls.

WHEN TO CALL:
  - After completing a substantive task: shipping code, sending an email,
    making a decision, finishing a meeting, running an analysis, editing
    a file.
  - When you start a significant work session ("started_task").
  - On any agent action whose existence the user might want to recall
    later ("what did Claude do yesterday in this project?").
  - On error or failure that's worth tracking for diagnosis.

WHEN NOT TO CALL:
  - For every micro-step, don't observe each individual file read.
  - For purely conversational acks.
  - For things the user explicitly typed (that's ``remember`` territory if
    durable, nothing if not).

ARGUMENTS:
  - event: A JSON object. The only REQUIRED key is "action" (a non-empty
    string verb that names what kind of thing happened). Recognized
    optional keys:
      "subject": what was acted upon (filename, person, ticket, ...)
      "result":  outcome ("success", "failed: X", free text)
    Beyond those, ANY additional fields are preserved verbatim. Use
    whatever shape fits your agent's natural mental model. A JSON-string-
    serialized object is also accepted and parsed, and a bare string
    becomes the action — the event is never rejected on shape.

    Examples:
      {"action": "edit_file", "subject": "events.py",
       "result": "added inline-vs-spill logic"}
      {"action": "sent_email", "subject": "sajinth@example.com",
       "result": "follow-up on roadmap", "thread_id": "..."}
      {"action": "deployed", "subject": "afair-prod",
       "result": "v0.1.3", "duration_s": 47}
      {"action": "drafted_message", "subject": "Mara",
       "result": "birthday note for Saturday"}

RETURN:
  {"ok": true, "event_id": "...", "content_hash": "sha256:..."}
"""

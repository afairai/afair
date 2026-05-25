"""Extractor prompts — system + user-message builders.

Phase 0 prompts intentionally minimal but complete. Refinement happens
during the two-week capability-gate journal (task #7) as we observe what
the LLM gets wrong. Each refinement is itself committed history per I7.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..substrate.events import Event


EXTRACTOR_SCHEMA_VERSION = 1
"""Bumped only when the extraction JSON shape changes (additive only)."""


EXTRACTOR_SYSTEM_PROMPT = """\
You are an information extractor for a personal memory vault. Given one
event from the user's substrate, output a strict JSON object describing
its structure and content. No preamble, no markdown fences — JSON only.

Rules:
- Never invent information not present in the input.
- Use null or empty arrays for fields with no information.
- Resolve relative dates ("yesterday", "next Tuesday") to ISO 8601 if you
  can compute them from the event's created_at; otherwise leave iso as null.
- best_guess_kind is a short free-text classification (e.g., "email",
  "meeting_notes", "decision", "fact", "task", "idea", "code_snippet",
  "voice_memo", "screenshot", "contact_info", "preference"). Do NOT
  constrain to a fixed enum — pick the most accurate single label.
- confidence is your own self-assessment (0.0 = guess, 1.0 = explicit).

Required JSON schema:
{
  "best_guess_kind": "<string>",
  "summary": "<one-sentence summary, max ~240 chars>",
  "entities": [
    {"name": "<string>",
     "type": "<person|organization|place|project|product|concept|other>"}
  ],
  "relations": [
    {"subject": "<string>", "predicate": "<short verb-phrase>",
     "object": "<string>"}
  ],
  "time_references": [
    {"text": "<original phrase>", "iso": "<ISO 8601 or null>"}
  ],
  "salient_facts": ["<fact 1>", "<fact 2>"],
  "language": "<ISO 639-1 code (en, de, fr, es, ...)>",
  "confidence": <0.0-1.0>,
  "source_attribution": "<who said this / where it came from, or null>"
}
"""


def build_user_message(event: Event) -> str:
    """Compose the per-event user message handed to the LLM."""
    payload = event.payload
    content_type = payload.get("content_type", "unknown")

    visible: dict[str, Any] = {
        "event_kind": event.kind,
        "event_created_at": event.created_at,
        "content_type": content_type,
    }
    # Bring relevant fields into a flat view that the LLM can chew on.
    for key in (
        "text",
        "context",
        "type_hint",
        "mime",
        "filename_hint",
        "size_bytes",
        "language",
        # observe-event fields
        "action",
        "subject",
        "result",
    ):
        value = payload.get(key)
        if value is not None:
            visible[key] = value

    # For binary or oversize text, we surface only metadata — the model
    # doesn't see the raw bytes. A future multimodal extractor may fetch
    # the blob and use a vision-capable model.
    if content_type in {"binary", "text-large"}:
        visible["note"] = (
            "Content is in the object store; only metadata is shown here. "
            "Extract from filename, mime, and context."
        )

    # Plus any extra fields agents tacked on (observe is free-form).
    for key, value in payload.items():
        if key not in visible and key not in {"content_type", "blob_hash"}:
            visible[key] = value

    return "Extract structured information from the following event:\n\n" + json.dumps(
        visible, ensure_ascii=False, indent=2
    )

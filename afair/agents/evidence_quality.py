"""Conservative, deterministic gates for paid evidence processing.

Only known content-free observations are routine. Unknown results, failures,
corrections, and extra factual fields remain eligible. Raw events are untouched.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..substrate.events import Event

_ROUTINE_ACTIONS = frozenset(
    {
        "read",
        "fetch",
        "search",
        "query",
        "inspect",
        "check",
        "list",
        "open",
        "edit",
        "write",
        "run",
        "test",
        "call",
        "view",
        "read_file",
        "fetch_url",
        "run_query",
        "inspect_file",
        "run_tests",
        "edit_file",
        "write_file",
        "list_files",
    }
)
_ROUTINE_RESULTS = frozenset(
    {
        "",
        "ok",
        "success",
        "successful",
        "done",
        "completed",
        "passed",
        "read",
        "fetched",
        "queried",
        "inspected",
        "checked",
        "listed",
        "opened",
        "edited",
        "written",
        "viewed",
    }
)
_METADATA_FIELDS = frozenset(
    {
        "content_type",
        "action",
        "subject",
        "result",
        "context",
        "actor",
        "timestamp",
        "duration",
        "duration_ms",
        "tool",
        "client",
    }
)


def is_routine_observation(event: Event) -> bool:
    """True only for action bookkeeping with no substantive outcome.

    A routine action name alone never suppresses a changed factual result.
    Context and any unfamiliar fields are evidence too: fail open to processing.
    """
    if event.kind != "observe":
        return False
    return is_routine_observation_payload(event.payload)


def is_routine_observation_payload(payload: Mapping[str, Any]) -> bool:
    """Payload variant for filtering before SQL discovery-window bounds."""
    if any(key not in _METADATA_FIELDS for key in payload):
        return False
    if payload.get("context"):
        return False
    action = str(payload.get("action", "")).strip().casefold()
    raw_result = payload.get("result")
    result = ("" if raw_result is None else str(raw_result)).strip().casefold().rstrip(".! ")
    return action in _ROUTINE_ACTIONS and result in _ROUTINE_RESULTS


_TIMING_FIELDS = frozenset({"timestamp", "duration", "duration_ms"})


def observation_evidence_key(payload: Mapping[str, Any]) -> str:
    """Exact observation content apart from capture timing; no fuzzy matches."""
    return json.dumps(
        {k: v for k, v in payload.items() if k not in _TIMING_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
    )


def observation_lane(payload: Mapping[str, Any]) -> str:
    """Reference and action whose latest outcome is being compared.

    Keep actor and unfamiliar factual fields. Different assertions must not
    collapse just because they share a tool name or a success string.
    """
    return json.dumps(
        {k: v for k, v in payload.items() if k not in _TIMING_FIELDS and k != "result"},
        sort_keys=True,
        ensure_ascii=False,
    )


_REFERENCE_FIELDS = (
    "project",
    "project_id",
    "task",
    "task_id",
    "issue",
    "issue_id",
    "ticket",
    "ticket_id",
    "thread_id",
    "repository",
    "repo",
    "workspace",
)
_GENERIC_REFERENCES = frozenset(
    {
        "tests",
        "test",
        "code",
        "file",
        "files",
        "tool",
        "tools",
        "project",
        "task",
        "workspace",
        "repository",
        "repo",
        "readme",
        "readme.md",
        "api",
        "vault",
        "memory",
        "user",
        "query",
        "search",
        "build",
        "deploy",
        "deployment",
        "database",
        "db",
        "status",
        "check",
        "checks",
        "result",
        "results",
        "none",
        "a",
        "an",
        "the",
        "and",
        "for",
        "to",
        "in",
        "on",
        "of",
        "at",
        "by",
        "with",
        "from",
        "current",
        "local",
        "remote",
        "command",
        "commands",
        "unit",
        "integration",
        "changes",
        "changed",
        "validation",
        "updated",
        "docs",
        "documentation",
        "run",
        "running",
        "ran",
        "read",
        "write",
        "edit",
        "editing",
        "inspect",
        "inspected",
        "checked",
        "implementation",
        "configuration",
        "config",
        "review",
        "reviewed",
        "fix",
        "fixed",
        "errors",
        "error",
        "success",
        "failed",
        "passed",
        "budget",
        "owner",
    }
)


def _reference(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().casefold().split())


def should_compare_observations(
    event_a: Event,
    event_b: Event,
    *,
    entity_ids_a: frozenset[str] = frozenset(),
    entity_ids_b: frozenset[str] = frozenset(),
) -> bool:
    """Pure paid-comparison gate; prior judgments and raw events are untouched.

    Binder similarity alone is insufficient for two observations: generic tool
    language often looks similar across unrelated projects. Require substantive
    outcomes plus explicit lineage, a compatible task/project/subject/context,
    or shared canonical entity evidence. Mixed remember/observe pairs retain
    their existing eligibility.
    """
    if event_a.kind != "observe" or event_b.kind != "observe":
        return True
    if is_routine_observation(event_a) or is_routine_observation(event_b):
        return False
    parents_a, parents_b = set(event_a.parent_hashes or ()), set(event_b.parent_hashes or ())
    if (
        event_b.content_hash in parents_a
        or event_a.content_hash in parents_b
        or parents_a & parents_b
    ):
        return True
    a, b = event_a.payload, event_b.payload
    refs_a = {key: _reference(a.get(key)) for key in _REFERENCE_FIELDS}
    refs_b = {key: _reference(b.get(key)) for key in _REFERENCE_FIELDS}
    # Explicitly different projects must not match on a generic filename or a
    # shared person. A common task/thread still supplies a legitimate bridge.
    shared = {
        refs_a[k]
        for k in _REFERENCE_FIELDS
        if refs_a[k] and refs_a[k] == refs_b[k] and refs_a[k] not in _GENERIC_REFERENCES
    }
    if shared:
        return True
    if any(
        refs_a[k] and refs_b[k] and refs_a[k] != refs_b[k]
        for k in ("project", "project_id", "repository", "repo", "workspace")
    ):
        return False
    if entity_ids_a & entity_ids_b:
        return True
    for key in ("subject", "context"):
        left, right = _reference(a.get(key)), _reference(b.get(key))
        if not left or not right:
            continue
        tokens_a = set(re.findall(r"[a-z][a-z0-9_-]{2,}", left)) - _GENERIC_REFERENCES
        # Relative filenames are reusable across projects; require lineage,
        # graph evidence or an explicit project/task rather than guessing.
        if (
            left == right
            and tokens_a
            and (("." not in left and "/" not in left) or left.startswith("/"))
        ):
            return True
        # A capitalized shared named reference permits "Atlas budget" vs
        # "Atlas owner" without using arbitrary workflow-word overlap.
        named_a = {
            _reference(token)
            for token in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", str(a.get(key, "")))
        } - _GENERIC_REFERENCES
        named_b = {
            _reference(token)
            for token in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", str(b.get(key, "")))
        } - _GENERIC_REFERENCES
        if named_a & named_b and not any(mark in left + right for mark in ("/", ".")):
            return True
    return False

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
    if payload.get("context") or _has_change_cue(payload):
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
        "verify",
        "verification",
        "record",
        "corrected",
        "rollback",
        "release",
        "strategic",
        "strategy",
        "plan",
        "merged",
        "merge",
        "head",
        "commit",
        "branch",
        "main",
        "master",
        "users",
        "home",
        "agent",
        "src",
        "index",
        "github",
        "https",
        "http",
        "com",
        "org",
        "net",
    }
)


def _reference(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().casefold().split())


_CHANGE_CUES = re.compile(
    r"\b(correct(?:ion|ed|ing)?|actually|instead|supersed\w*|invalidat\w*|"
    r"revert\w*|regress\w*|rollback|contradict\w*|conflict\w*|discrepanc\w*|"
    r"previously|no longer|changed (?:from|to)|now)\b",
    re.IGNORECASE,
)


def _has_change_cue(payload: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(key) or "").replace("_", " ")
        for key in ("action", "subject", "result", "context")
    )
    return _CHANGE_CUES.search(text) is not None


def _content_references(payload: Mapping[str, Any]) -> set[str]:
    """Recognize named references across actions, paths, PRs and ticker files.

    Shared references preserve comparisons. They never establish a conflict or
    decide one. Generic workflow vocabulary cannot establish disjoint projects.
    """
    text = " ".join(
        str(payload.get(key) or "") for key in (*_REFERENCE_FIELDS, "action", "subject", "context")
    )
    return {
        token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text)
    } - _GENERIC_REFERENCES


def _project_identity(value: str) -> str:
    # A local checkout path and org/repo can designate the same repository.
    return value.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]


def _named_subject(payload: Mapping[str, Any]) -> str:
    """Positive disjointness evidence only for an unambiguous named subject.

    Filenames, paths, generic phrases and prose are deliberately insufficient:
    different files and different release tasks can belong to one project.
    """
    subject = str(payload.get("subject") or "").strip()
    words = subject.split()
    if not words or len(words) > 3 or not re.fullmatch(r"[A-Z][A-Za-z0-9-]{2,}", words[0]):
        return ""
    if any(_reference(word) not in _GENERIC_REFERENCES for word in words[1:]):
        return ""
    name = _reference(words[0])
    return name if name not in _GENERIC_REFERENCES else ""


def should_compare_observations(
    event_a: Event,
    event_b: Event,
    *,
    entity_ids_a: frozenset[str] = frozenset(),
    entity_ids_b: frozenset[str] = frozenset(),
) -> bool:
    """Pure conservative paid-comparison gate; never modifies prior judgments.

    Suppress known routine outcomes, or substantive pairs with positive evidence
    of different references. Missing context is uncertainty, not disjointness.
    Corrections and chronology fail open. Shared references in paths, filenames,
    ticker symbols, snake_case actions or graph evidence preserve comparisons.
    """
    if event_a.kind != "observe" or event_b.kind != "observe":
        return True
    a, b = event_a.payload, event_b.payload
    # Cues can occur in subject/action even with an empty result.
    if _has_change_cue(a) or _has_change_cue(b):
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
    refs_a = {key: _reference(a.get(key)) for key in _REFERENCE_FIELDS}
    refs_b = {key: _reference(b.get(key)) for key in _REFERENCE_FIELDS}
    if any(
        refs_a[k] and refs_a[k] == refs_b[k] and refs_a[k] not in _GENERIC_REFERENCES
        for k in _REFERENCE_FIELDS
    ):
        return True
    # Explicitly different projects cannot match on an incidental common file
    # or person. Normalize repository spellings before deciding they differ.
    if any(
        refs_a[k] and refs_b[k] and _project_identity(refs_a[k]) != _project_identity(refs_b[k])
        for k in ("project", "project_id", "repository", "repo", "workspace")
    ):
        return False
    if entity_ids_a & entity_ids_b or _content_references(a) & _content_references(b):
        return True
    name_a, name_b = _named_subject(a), _named_subject(b)
    # File differences, generic task words, sparse payloads and unfamiliar
    # references do not establish unrelatedness. Prefer an extra comparison.
    return not (name_a and name_b and name_a != name_b)

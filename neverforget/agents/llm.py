"""Vendor-neutral LLM wrapper — single call surface satisfying Invariant I5.

Every agent calls through this module; provider selection is entirely a
function of the ``model`` string (``anthropic/claude-haiku-4-5``,
``openai/gpt-4o-mini``, ``gemini/gemini-2.5-flash``, ``ollama/llama3.3``,
etc.). Switching providers is a config change, never a code change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class LLMError(Exception):
    """Base for any LLM-call failure that should be recorded as failed_extraction."""

    error_type: str = "llm_error"


class LLMTimeout(LLMError):
    error_type = "llm_timeout"


class LLMAuthError(LLMError):
    error_type = "llm_auth_error"


class LLMRateLimit(LLMError):
    error_type = "llm_rate_limit"


class LLMResponseError(LLMError):
    """The LLM returned something we can't parse (not valid JSON, etc.)."""

    error_type = "llm_response_error"


@dataclass
class LLMResult:
    """A parsed JSON response from an LLM call."""

    data: dict[str, Any]
    model: str
    raw: str


def call_json(
    *,
    model: str,
    system: str,
    user: str,
    api_key: str | None = None,
    timeout: float = 30.0,
    max_tokens: int = 1500,
) -> LLMResult:
    """Synchronously call the LLM and parse the JSON response.

    Wraps litellm so the rest of the code is vendor-agnostic. Raises a
    subclass of LLMError on every failure mode so the caller can map to
    a ``failed_extraction`` row (option (b) from the design).
    """
    # Lazy import — litellm is heavy at import time.
    import litellm

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    # response_format=json_object is OpenAI-shaped; Anthropic and others
    # interpret JSON-mode differently or not at all. We instead rely on the
    # system prompt's strict-JSON instruction plus defensive parsing of the
    # response (fenced or with prose preamble). Keeps the wrapper vendor-
    # agnostic per I5.
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        response = litellm.completion(**kwargs)
    except Exception as e:
        kind = type(e).__name__.lower()
        if "timeout" in kind:
            raise LLMTimeout(str(e)) from e
        if "auth" in kind or "key" in kind:
            raise LLMAuthError(str(e)) from e
        if "rate" in kind or "ratelimit" in kind:
            raise LLMRateLimit(str(e)) from e
        raise LLMError(str(e)) from e

    raw = _extract_text(response)
    data = _parse_json_loose(raw)

    if not isinstance(data, dict):
        raise LLMResponseError(f"expected JSON object at top level, got {type(data).__name__}")

    return LLMResult(data=data, model=model, raw=raw)


# Markdown-fenced JSON: ```json {...} ``` or just ``` {...} ```
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_json_loose(raw: str) -> Any:
    """Tolerant JSON parse — handles fences and prose preambles.

    Different vendors wrap JSON differently. Anthropic Haiku will sometimes
    fence it; OpenAI in JSON-mode returns pure JSON; smaller models prepend
    explanations. We try strict first, then fall back to extracting the
    first balanced JSON object.
    """
    stripped = raw.strip()
    if not stripped:
        msg = "LLM returned empty response"
        raise LLMResponseError(msg)

    # 1. Try strict parse — the happy path when the prompt actually worked.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip a markdown code fence if present.
    fence_match = _FENCE_RE.search(stripped)
    if fence_match:
        candidate = fence_match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 3. Fall back: grab from first '{' to last '}'.
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = stripped[first : last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            msg = f"non-JSON response after best-effort extraction: {e}"
            raise LLMResponseError(msg) from e

    msg = f"non-JSON response (no JSON-shaped substring found): {stripped[:200]!r}"
    raise LLMResponseError(msg)


def _extract_text(response: Any) -> str:
    """Pull the textual content out of a litellm completion response."""
    try:
        choices = response.choices
        if not choices:
            msg = "LLM response had no choices"
            raise LLMResponseError(msg)
        message = choices[0].message
        content = message.content
    except (AttributeError, IndexError) as e:
        msg = f"malformed LLM response object: {e}"
        raise LLMResponseError(msg) from e
    if not isinstance(content, str):
        msg = f"LLM message.content was {type(content).__name__}, expected str"
        raise LLMResponseError(msg)
    return content

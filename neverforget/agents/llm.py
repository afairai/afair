"""Vendor-neutral LLM wrapper — single call surface satisfying Invariant I5.

Every agent calls through this module; provider selection is entirely a
function of the ``model`` string (``anthropic/claude-haiku-4-5``,
``openai/gpt-4o-mini``, ``gemini/gemini-2.5-flash``, ``ollama/llama3.3``,
etc.). Switching providers is a config change, never a code change.

Two call surfaces:
  - ``call_json`` — legacy free-form "respond in JSON" prompt; defensive
    parse with fenced/loose recovery. Retained for completeness but
    brittle on large inputs.
  - ``call_tool`` — forces structured output via the provider's tool-use
    mode. The model can only emit arguments conforming to the supplied
    JSON Schema, eliminating parse failures on large or complex outputs.
    Used by the Extractor since 2026-05-25 after Haiku's free-form JSON
    failed on the 56KB VISION.md ingestion.
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
        raise _classify(e) from e

    raw = _extract_text(response)
    data = _parse_json_loose(raw)

    if not isinstance(data, dict):
        raise LLMResponseError(f"expected JSON object at top level, got {type(data).__name__}")

    return LLMResult(data=data, model=model, raw=raw)


def call_tool(
    *,
    model: str,
    system: str,
    user: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    api_key: str | None = None,
    timeout: float = 30.0,
    max_tokens: int = 2000,
) -> LLMResult:
    """Call the LLM in tool-forcing mode, returning the (validated) tool arguments.

    The model is offered exactly one tool and forced to call it. Output
    can only be arguments matching ``tool_schema``, so:
      - There is no preamble/markdown/prose to strip.
      - Truncated mid-emission still produces invalid JSON, but the failure
        mode is a clean "tool_call missing/incomplete" rather than a
        delimiter-mismatch parse error deep in the payload.
      - The schema acts as documentation for the model — fields are
        described once and reused for every call.

    Works across providers via litellm's standardized tool-use surface
    (OpenAI's function-calling shape, transparently translated for
    Anthropic, Gemini, etc. by litellm).
    """
    import litellm

    tools = [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_description,
                "parameters": tool_schema,
            },
        }
    ]
    tool_choice = {"type": "function", "function": {"name": tool_name}}

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        response = litellm.completion(**kwargs)
    except Exception as e:
        raise _classify(e) from e

    raw_args = _extract_tool_arguments(response, expected_name=tool_name)
    try:
        data = json.loads(raw_args)
    except json.JSONDecodeError as e:
        msg = f"tool arguments were not valid JSON despite tool-use forcing: {e}"
        raise LLMResponseError(msg) from e

    if not isinstance(data, dict):
        msg = f"tool arguments must be a JSON object, got {type(data).__name__}"
        raise LLMResponseError(msg)

    return LLMResult(data=data, model=model, raw=raw_args)


def _classify(exc: Exception) -> LLMError:
    """Map a raw provider/litellm exception to one of our LLMError subclasses."""
    kind = type(exc).__name__.lower()
    msg = str(exc)
    if "timeout" in kind:
        return LLMTimeout(msg)
    if "auth" in kind or "key" in kind:
        return LLMAuthError(msg)
    if "rate" in kind or "ratelimit" in kind:
        return LLMRateLimit(msg)
    return LLMError(msg)


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


def _extract_tool_arguments(response: Any, *, expected_name: str) -> str:
    """Pull tool-call arguments (as JSON string) from a litellm completion response.

    Works against both dict-shaped and object-shaped response items so we
    don't depend on a specific litellm version's internal types.
    """
    try:
        choices = response.choices
        if not choices:
            msg = "LLM response had no choices"
            raise LLMResponseError(msg)
        message = choices[0].message
    except (AttributeError, IndexError) as e:
        msg = f"malformed LLM response object: {e}"
        raise LLMResponseError(msg) from e

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None and hasattr(message, "get"):
        tool_calls = message.get("tool_calls")
    if not tool_calls:
        msg = "LLM response had no tool_calls despite tool-use forcing"
        raise LLMResponseError(msg)

    call = tool_calls[0]
    func = getattr(call, "function", None)
    if func is None and hasattr(call, "get"):
        func = call.get("function")
    if func is None:
        msg = "tool_call missing function payload"
        raise LLMResponseError(msg)

    name = getattr(func, "name", None) or (func.get("name") if hasattr(func, "get") else None)
    args = getattr(func, "arguments", None) or (
        func.get("arguments") if hasattr(func, "get") else None
    )
    if name != expected_name:
        msg = f"tool_call name was {name!r}, expected {expected_name!r}"
        raise LLMResponseError(msg)
    if not isinstance(args, str):
        # Some adapters give us a dict already; serialize for downstream parse.
        try:
            args = json.dumps(args)
        except (TypeError, ValueError) as e:
            msg = f"tool arguments were neither string nor JSON-serializable: {e}"
            raise LLMResponseError(msg) from e
    return args

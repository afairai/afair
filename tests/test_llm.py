"""LLM wrapper tests — call_tool (tool-use forcing). No live API calls.

call_json is exercised transitively by test_extractor.py; this file
focuses on the new call_tool path that the Extractor switched to on
2026-05-25 after Haiku's free-form JSON broke on the 56KB VISION.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from afair.agents import llm

# A simple schema reused across tests.
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "number"},
    },
    "required": ["name", "value"],
}


def _make_response(tool_args: str | dict[str, Any], tool_name: str = "do_thing") -> object:
    """Build a litellm-shaped response object with a single tool_call.

    Uses simple dict-shaped objects so we exercise the dict-fallback branch
    in _extract_tool_arguments. The object-attribute branch is covered by
    real litellm responses in integration.
    """
    if isinstance(tool_args, dict):
        tool_args = json.dumps(tool_args)

    class _Func:
        def __init__(self) -> None:
            self.name = tool_name
            self.arguments = tool_args

    class _Call:
        def __init__(self) -> None:
            self.function = _Func()

    class _Msg:
        def __init__(self) -> None:
            self.tool_calls = [_Call()]
            self.content = None

    class _Choice:
        def __init__(self) -> None:
            self.message = _Msg()

    class _Resp:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    return _Resp()


def test_call_tool_returns_parsed_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, Any] = {}

    def fake_completion(**kwargs: Any) -> object:
        captured_kwargs.update(kwargs)
        return _make_response({"name": "x", "value": 42})

    import litellm

    monkeypatch.setattr(litellm, "completion", fake_completion)

    result = llm.call_tool(
        model="anthropic/claude-haiku-4-5",
        system="sys",
        user="usr",
        tool_name="do_thing",
        tool_description="desc",
        tool_schema=SCHEMA,
    )
    assert result.data == {"name": "x", "value": 42}
    assert result.model == "anthropic/claude-haiku-4-5"

    # Verify we actually sent the tool-use shape to litellm.
    assert "tools" in captured_kwargs
    tools = captured_kwargs["tools"]
    assert tools[0]["function"]["name"] == "do_thing"
    assert tools[0]["function"]["parameters"] == SCHEMA
    assert captured_kwargs["tool_choice"]["function"]["name"] == "do_thing"


def test_call_tool_passes_api_key_when_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_key: list[str | None] = []

    def fake_completion(**kwargs: Any) -> object:
        seen_key.append(kwargs.get("api_key"))
        return _make_response({"name": "x", "value": 1}, tool_name="t")

    import litellm

    monkeypatch.setattr(litellm, "completion", fake_completion)

    llm.call_tool(
        model="m",
        system="s",
        user="u",
        tool_name="t",
        tool_description="d",
        tool_schema=SCHEMA,
        api_key="secret",
    )
    assert seen_key == ["secret"]


def test_call_tool_raises_response_error_when_no_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the model emits text instead of a tool call, we surface the failure."""

    class _Msg:
        def __init__(self) -> None:
            self.tool_calls = None
            self.content = "I refuse to use the tool."

    class _Choice:
        def __init__(self) -> None:
            self.message = _Msg()

    class _Resp:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **_: _Resp())

    with pytest.raises(llm.LLMResponseError, match="no tool_calls"):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="t",
            tool_description="d",
            tool_schema=SCHEMA,
        )


def test_call_tool_raises_response_error_on_wrong_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_: _make_response({"name": "x", "value": 1}, tool_name="other_tool"),
    )

    with pytest.raises(llm.LLMResponseError, match="expected"):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="do_thing",
            tool_description="d",
            tool_schema=SCHEMA,
        )


def test_call_tool_raises_response_error_on_invalid_json_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: if a provider somehow returns malformed args."""
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_: _make_response("not valid json {"),
    )

    with pytest.raises(llm.LLMResponseError, match="not valid JSON"):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="do_thing",
            tool_description="d",
            tool_schema=SCHEMA,
        )


def test_call_tool_accepts_dict_args_serializes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some litellm adapter versions give us dict args; we serialize+reparse."""
    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_: _make_response({"name": "x", "value": 9}),
    )

    result = llm.call_tool(
        model="m",
        system="s",
        user="u",
        tool_name="do_thing",
        tool_description="d",
        tool_schema=SCHEMA,
    )
    assert result.data == {"name": "x", "value": 9}


def test_call_tool_maps_timeout_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutError_(Exception):
        pass

    def boom(**_: Any) -> object:
        raise TimeoutError_("upstream timed out")

    import litellm

    monkeypatch.setattr(litellm, "completion", boom)

    with pytest.raises(llm.LLMTimeout):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="t",
            tool_description="d",
            tool_schema=SCHEMA,
        )


def test_call_tool_maps_auth_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class AuthError(Exception):
        pass

    import litellm

    monkeypatch.setattr(
        litellm, "completion", lambda **_: (_ for _ in ()).throw(AuthError("bad key"))
    )

    with pytest.raises(llm.LLMAuthError):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="t",
            tool_description="d",
            tool_schema=SCHEMA,
        )


def test_call_tool_maps_rate_limit_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class RateLimitError(Exception):
        pass

    import litellm

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **_: (_ for _ in ()).throw(RateLimitError("slow down")),
    )

    with pytest.raises(llm.LLMRateLimit):
        llm.call_tool(
            model="m",
            system="s",
            user="u",
            tool_name="t",
            tool_description="d",
            tool_schema=SCHEMA,
        )

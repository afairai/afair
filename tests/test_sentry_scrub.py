"""Sentry ``before_send`` scrubber — privacy regression guard.

afair is a personal-memory product: an exception must never ship raw
vault text, the auth bearer, provider API keys, or customer PII to the
third-party error tracker. These tests pin the scrubbing behaviour so a
future config change can't silently re-open the leak.
"""

from __future__ import annotations

from afair.observability.sentry import _before_send, _key_is_sensitive


def test_request_headers_cookies_body_are_stripped() -> None:
    event = {
        "request": {
            "url": "https://mcp.afair.ai/mcp",
            "headers": {"Authorization": "Bearer secret-token"},
            "cookies": {"session": "abc"},
            "data": "raw vault memory text",
        }
    }
    scrubbed = _before_send(event, {})
    assert "headers" not in scrubbed["request"]
    assert "cookies" not in scrubbed["request"]
    assert "data" not in scrubbed["request"]
    assert scrubbed["request"]["url"] == "https://mcp.afair.ai/mcp"


def test_sensitive_extra_keys_are_masked() -> None:
    event = {
        "extra": {
            "auth_token": "live-bearer",
            "vault_key": "deadbeef",
            "prompt": "the user's private memory",
            "harmless_count": 7,
            "nested": {"api_key": "sk-123", "ok": "keep"},
        }
    }
    scrubbed = _before_send(event, {})
    extra = scrubbed["extra"]
    assert extra["auth_token"] == "[redacted]"
    assert extra["vault_key"] == "[redacted]"
    assert extra["prompt"] == "[redacted]"
    assert extra["harmless_count"] == 7
    assert extra["nested"]["api_key"] == "[redacted]"
    assert extra["nested"]["ok"] == "keep"


def test_frame_locals_are_dropped() -> None:
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {"function": "extract", "vars": {"prompt": "secret"}},
                            {"function": "other"},
                        ]
                    }
                }
            ]
        }
    }
    scrubbed = _before_send(event, {})
    frames = scrubbed["exception"]["values"][0]["stacktrace"]["frames"]
    assert "vars" not in frames[0]
    assert frames[1]["function"] == "other"


def test_key_sensitivity_detection() -> None:
    for key in ("Authorization", "X-Auth-Token", "VAULT_KEY", "user_email", "Cookie"):
        assert _key_is_sensitive(key), key
    for key in ("count", "status", "url", "duration_ms"):
        assert not _key_is_sensitive(key), key


def test_before_send_is_idempotent_and_returns_event() -> None:
    event: dict = {"message": "boom"}
    assert _before_send(event, {}) is event


def test_uvicorn_client_disconnect_noise_is_dropped() -> None:
    # AFAIR-6: benign client-disconnect mid-stream, 0 users impacted.
    event: dict = {
        "logger": "uvicorn.error",
        "logentry": {"message": "ASGI callable returned without completing response."},
    }
    assert _before_send(event, {}) is None


def test_real_uvicorn_error_still_reports() -> None:
    # A genuine uvicorn error (different message) must NOT be dropped.
    event: dict = {"logger": "uvicorn.error", "message": "worker failed to boot"}
    assert _before_send(event, {}) is event


def test_asgi_message_from_other_logger_still_reports() -> None:
    # The narrow logger match means the same string elsewhere is not scrubbed.
    event: dict = {
        "logger": "afair.mcp",
        "message": "ASGI callable returned without completing response.",
    }
    assert _before_send(event, {}) is event

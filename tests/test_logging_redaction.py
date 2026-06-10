"""Structured-logging redaction — credential/PII leak regression guard.

Pins the structlog ``redact_processor`` so credential-shaped keys are
masked and oversized values are truncated before any log line is emitted.
"""

from __future__ import annotations

from afair.observability.logging import _key_is_sensitive, redact_processor


def test_credential_keys_masked() -> None:
    event = {
        "event": "auth.verified",
        "auth_token": "live-bearer",
        "vault_key": "deadbeef",
        "user_email": "a@b.com",
        "status": "ok",
        "count": 3,
    }
    out = redact_processor(None, "info", event)
    assert out["auth_token"] == "[redacted]"
    assert out["vault_key"] == "[redacted]"
    assert out["user_email"] == "[redacted]"
    assert out["status"] == "ok"
    assert out["count"] == 3


def test_nested_dict_masked() -> None:
    event = {"event": "x", "ctx": {"api_key": "sk-1", "ok": "keep"}}
    out = redact_processor(None, "info", event)
    assert out["ctx"]["api_key"] == "[redacted]"
    assert out["ctx"]["ok"] == "keep"


def test_long_value_truncated() -> None:
    big = "x" * 5000
    out = redact_processor(None, "info", {"event": "e", "error": big})
    assert out["error"].endswith("…[truncated]")
    assert len(out["error"]) < 600


def test_sensitivity_detection() -> None:
    for key in ("Authorization", "X-Auth-Token", "PASSWORD", "user_email"):
        assert _key_is_sensitive(key)
    for key in ("event", "status", "count", "duration_ms"):
        assert not _key_is_sensitive(key)

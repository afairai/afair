"""Structured-logging redaction — credential/PII leak regression guard.

Pins the structlog ``redact_processor`` so credential-shaped keys are
masked before any log line is emitted, including exception formatting.
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


def test_error_values_are_masked_in_full() -> None:
    big = "x" * 5000
    out = redact_processor(None, "info", {"event": "e", "error": big})
    assert out["error"] == "[redacted]"


def test_sensitivity_detection() -> None:
    for key in ("Authorization", "X-Auth-Token", "PASSWORD", "user_email"):
        assert _key_is_sensitive(key)
    for key in ("event", "status", "count", "duration_ms"):
        assert not _key_is_sensitive(key)


def test_nested_sequences_and_unknown_objects_are_private() -> None:
    sentinel = "SYNTHETIC_PRIVATE_MEMORY_8362"
    out = redact_processor(
        None,
        "error",
        {
            "event": "extractor.failed",
            "duration_ms": 31,
            "details": [{"value": sentinel}, (sentinel, {"provider_response": sentinel})],
            "event_id": sentinel,
            "entity_id": sentinel,
            "content_hash": sentinel,
            "error": RuntimeError(sentinel),
        },
    )
    assert sentinel not in str(out)
    assert out["event"] == "extractor.failed"
    assert out["duration_ms"] == 31


def test_actual_configured_processors_never_render_exception_content(monkeypatch) -> None:
    import io

    import structlog

    from afair.observability import logging as module

    previous = structlog.get_config().copy()
    sentinel = "SYNTHETIC_PROVIDER_SECRET_4763"
    try:
        for environment in ("prod", "dev"):
            monkeypatch.setattr(module, "_configured", False)
            module.configure_logging(environment=environment)
            output = io.StringIO()
            structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=output))
            try:
                raise RuntimeError(sentinel)
            except RuntimeError:
                structlog.get_logger().exception(
                    "extractor.failed", details=[sentinel], request_id=sentinel, stack_info=True
                )
            rendered = output.getvalue()
            assert sentinel not in rendered
            assert "extractor.failed" in rendered
            assert "RuntimeError" in rendered
            assert "redacted" in rendered
    finally:
        structlog.configure(**previous)


def test_export_audit_preserves_operational_fields_without_raw_address() -> None:
    from afair.observability.logging import redact_processor

    job_id = "exp_" + "ca" * 12
    fields = {
        "event": "export.started",
        "credential": "export",
        "client_ip": "192.0.2.71",
        "job_id": job_id,
        "timestamp": "2026-11-26T17:32:01.555555Z",
    }
    clean = redact_processor(None, "info", fields.copy())
    assert clean["timestamp"] == fields["timestamp"]
    assert clean["job_id"] == job_id
    assert clean["credential"] == "export"
    assert clean["client_ip"] == "[redacted]"
    assert (
        clean["client_ip_group"] == redact_processor(None, "info", fields.copy())["client_ip_group"]
    )
    fields.update(credential="untrusted", job_id="untrusted", timestamp="untrusted")
    clean = redact_processor(None, "info", fields)
    assert clean["credential"] == clean["job_id"] == clean["timestamp"] == "[redacted]"

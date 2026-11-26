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
    assert "request" not in scrubbed


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
    for key in ("count", "status", "duration_ms"):
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


def test_incident_fields_never_export_synthetic_vault_content() -> None:
    import json

    from afair.observability.sentry import _before_breadcrumb

    sentinel = "SYNTHETIC_VAULT_SECRET_9372"
    event = {
        "level": "error",
        "logger": "afair.extractor",
        "message": sentinel,
        "transaction": sentinel,
        "fingerprint": [sentinel],
        "user": {"id": sentinel},
        "logentry": {"message": "%s", "params": [sentinel], "formatted": sentinel},
        "request": {"url": "https://example.test/" + sentinel},
        "extra": {
            "event_id": sentinel,
            "entity_id": sentinel,
            "content_hash": sentinel,
            "nested": [{"provider_response": sentinel}, [sentinel]],
            "count": 7,
        },
        "breadcrumbs": {"values": [{"message": sentinel, "data": {"sql": sentinel}}]},
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": sentinel,
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "extract",
                                "lineno": 34,
                                "abs_path": sentinel,
                                "filename": sentinel,
                                "context_line": sentinel,
                                "pre_context": [sentinel],
                                "vars": {"x": sentinel},
                            }
                        ]
                    },
                }
            ]
        },
        "unknown_sdk_extension": sentinel,
    }
    result = _before_send(event, {})
    assert sentinel not in json.dumps(result)
    assert result["exception"]["values"][0]["type"] == "RuntimeError"
    assert result["exception"]["values"][0]["stacktrace"]["frames"][0]["lineno"] == 34
    assert result["extra"]["count"] == 7
    assert _before_send(result, {}) == result
    assert sentinel not in json.dumps(
        _before_breadcrumb({"message": sentinel, "data": [sentinel]}, {})
    )


def test_sentry_init_installs_both_privacy_boundaries(monkeypatch) -> None:
    from afair.observability import sentry as module

    captured = {}
    monkeypatch.setenv("GIT_SHA", "1a" * 20)
    monkeypatch.setenv("SENTRY_DSN", "https://synthetic@example.test/1")
    monkeypatch.setattr(module.sentry_sdk, "init", lambda **options: captured.update(options))
    module.init_sentry()
    assert captured["release"] == "1a" * 20
    assert captured["before_send"] is module._before_send
    assert captured["before_breadcrumb"] is module._before_breadcrumb
    assert captured["traces_sample_rate"] == 0
    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False


def test_release_environment_survive_and_private_fingerprint_does_not(monkeypatch) -> None:
    monkeypatch.setenv("AFAIR_ENVIRONMENT", "dev")
    clean = _before_send(
        {"environment": "dev", "release": "0.1.30", "fingerprint": ["private"]}, {}
    )
    assert clean == {"environment": "dev", "release": "0.1.30"}
    assert _before_send({"environment": "private", "release": "private"}, {}) == {}

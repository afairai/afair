"""Tests for the pre-deploy secret guard (scripts/check_secrets.py).

The guard exists because afair-dev once drifted three weeks behind prod and
crash-looped on a missing AFAIR_VAULT_KEY, surfacing only as an opaque 502.
These tests pin that the guard names the gap precisely and fails the build
when a required secret is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts import check_secrets

if TYPE_CHECKING:
    import pytest

REQUIRED = check_secrets.REQUIRED_FLY_SECRETS


def test_required_set_matches_settings_validators() -> None:
    # If a new ENVIRONMENT=fly boot validator is added, this set must grow too.
    assert {"AFAIR_AUTH_TOKEN", "AFAIR_VAULT_KEY", "OAUTH_ISSUER"} == REQUIRED


def test_missing_required_empty_when_all_present() -> None:
    present = set(REQUIRED) | {"ANTHROPIC_API_KEY", "EMBEDDING_DIM"}
    assert check_secrets.missing_required(present) == set()


def test_missing_required_reports_the_gap() -> None:
    # The exact afair-dev failure: vault key absent.
    present = {"AFAIR_AUTH_TOKEN", "OAUTH_ISSUER", "ANTHROPIC_API_KEY"}
    assert check_secrets.missing_required(present) == {"AFAIR_VAULT_KEY"}


def test_name_diff_is_symmetric_partition() -> None:
    only_a, only_b = check_secrets.name_diff({"X", "Y"}, {"Y", "Z"})
    assert only_a == {"X"}
    assert only_b == {"Z"}


def test_cmd_check_passes_when_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_secrets, "fly_secret_names", lambda _app: set(REQUIRED) | {"OPENAI_API_KEY"}
    )
    assert check_secrets.cmd_check("afair-prod") == 0


def test_cmd_check_fails_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_secrets, "fly_secret_names", lambda _app: {"AFAIR_AUTH_TOKEN", "OAUTH_ISSUER"}
    )
    assert check_secrets.cmd_check("afair-dev") == 1

"""Spending gates count paid attempts, independently of interpretation success."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from afair.agents.llm import LLMResponseError, call_json
from afair.agents.usage import (
    BackgroundBudgetExceeded,
    _connection,
    background_usage,
    provider_completion,
)
from afair.settings import Settings


def settings(path: Path, cap: float | None = 1.0) -> Settings:
    return Settings(
        _env_file=None,
        vault_dir=path,
        background_daily_budget_usd=cap,
        background_model_prices={"test/model": (1.0, 1.0)},
    )


def test_paid_parse_failure_and_retry_both_count(tmp_path):
    config = settings(tmp_path)
    with patch(
        "litellm.completion", return_value={"usage": {"prompt_tokens": 10, "completion_tokens": 2}}
    ) as call:
        with background_usage(config, "extractor"):
            for _ in range(2):
                with pytest.raises(LLMResponseError):
                    call_json(model="test/model", system="s", user="u")
        assert call.call_count == 2
        assert call.call_args.kwargs["num_retries"] == 0
        assert call.call_args.kwargs["max_retries"] == 0
    conn = _connection(config)
    rows = conn.execute(
        "SELECT worker,status,prompt_tokens,cost_usd FROM provider_usage"
    ).fetchall()
    assert len(rows) == 2
    assert all(tuple(row)[:3] == ("extractor", "completed", 10) for row in rows)
    assert all(row[3] > 0 for row in rows)
    conn.close()


def test_zero_budget_defers_without_provider_call(tmp_path):
    with patch("litellm.completion") as call, background_usage(settings(tmp_path, 0), "judge"):
        with pytest.raises(BackgroundBudgetExceeded):
            provider_completion(model="test/model", messages=[], max_tokens=20)
        call.assert_not_called()


def test_unknown_price_is_closed_only_with_cap(tmp_path):
    with (
        patch("litellm.cost_per_token", side_effect=ValueError),
        patch("litellm.completion", return_value={}) as call,
    ):
        with (
            background_usage(settings(tmp_path), "worker"),
            pytest.raises(BackgroundBudgetExceeded),
        ):
            provider_completion(model="unknown/model", messages=[])
        with background_usage(settings(tmp_path, None), "worker"):
            provider_completion(model="unknown/model", messages=[])
        assert call.call_count == 1


def test_atomic_concurrent_reservations_and_restart(tmp_path):
    # Text without cache creation reserves bytes + chat overhead once.
    config = settings(tmp_path, 0.0015)

    def run():
        with background_usage(config, "retry"):
            try:
                provider_completion(model="test/model", messages=[], max_tokens=10)
            except BackgroundBudgetExceeded:
                return "deferred"
            except RuntimeError:
                return "attempted"

    with patch("litellm.completion", side_effect=RuntimeError("timeout")) as call:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run(), range(2)))
        assert sorted(results) == ["attempted", "deferred"]
        assert run() == "deferred"  # persisted; not just an in-memory meter
        assert call.call_count == 1


def test_nested_escalation_keeps_owner(tmp_path):
    config = settings(tmp_path)
    with (
        patch("litellm.completion", return_value={}),
        background_usage(config, "tuner"),
        background_usage(config, "judge"),
    ):
        provider_completion(model="test/model", messages=[])
    conn = _connection(config)
    assert conn.execute("SELECT worker FROM provider_usage").fetchone()[0] == "tuner"
    conn.close()


def test_day_rollover_gets_new_budget(tmp_path):
    config = settings(tmp_path, 0.003)
    conn = _connection(config)
    conn.execute(
        "INSERT INTO provider_usage VALUES ('old','2000-01-01','old','x','test/model','failed',1,NULL,NULL,NULL,'reservation','completion',NULL,NULL)"
    )
    conn.commit()
    conn.close()
    with patch("litellm.completion", return_value={}) as call, background_usage(config, "worker"):
        provider_completion(model="test/model", messages=[], max_tokens=10)
        assert call.call_count == 1


def test_judge_and_escalation_costs_cannot_bypass_cap(tmp_path):
    from afair.agents.llm_judge import JudgePair, _ask_one_judge

    pair = JudgePair("input", "a", "b", "worker", "purpose", "criteria")
    with patch("litellm.completion") as call, background_usage(settings(tmp_path, 0), "tuner"):
        with pytest.raises(BackgroundBudgetExceeded):
            _ask_one_judge(pair, 0, "test/model")
        call.assert_not_called()


def test_haiku_tier_and_cache_tokens(tmp_path):
    from afair.agents.usage import usage_summary

    config = Settings(_env_file=None, vault_dir=tmp_path, background_daily_budget_usd=1)
    response = {
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cache_read_input_tokens": 500}
    }
    with patch("litellm.completion", return_value=response), background_usage(config, "extractor"):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=[], max_tokens=100)
    assert usage_summary(config)["workers"][0]["accounted_usd"] == pytest.approx(0.000105)


def test_audio_is_accounted_separately_from_generation_cap(tmp_path):
    from afair.agents.usage import usage_summary

    config = settings(tmp_path, 0)
    with (
        patch("litellm.transcription", return_value={}) as call,
        background_usage(config, "extractor"),
    ):
        provider_completion(_operation="transcription", model="test/model")
        call.assert_called_once()
    row = usage_summary(config)["workers"][0]
    assert row["operation"] == "transcription"
    assert row["unpriced_attempts"] == 1


def test_report_is_read_only_and_does_not_create_missing_vault(tmp_path):
    from afair.agents.usage import usage_summary

    config = settings(tmp_path / "missing")
    assert usage_summary(config)["workers"] == []
    assert not config.vault_dir.exists()


def test_encrypted_cli_reopens_using_configured_key(tmp_path, capsys):
    pytest.importorskip("sqlcipher3")
    from scripts.background_usage import main

    config = Settings(
        _env_file=None,
        vault_dir=tmp_path,
        vault_key="test-only-usage-key-32-bytes-long-12345",
        background_daily_budget_usd=1,
    )
    with (
        patch(
            "litellm.completion",
            return_value={"usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
        ),
        background_usage(config, "extractor"),
    ):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=[], max_tokens=100)
    db_bytes = (tmp_path / "usage.db").read_bytes()
    assert not db_bytes.startswith(b"SQLite format 3")
    with patch("scripts.background_usage.Settings", return_value=config):
        assert main([]) == 0
    assert '"extractor"' in capsys.readouterr().out
    assert (tmp_path / "usage.db").read_bytes() == db_bytes


def test_haiku_does_not_depend_on_litellm_model_map(tmp_path):
    config = Settings(_env_file=None, vault_dir=tmp_path, background_daily_budget_usd=1)
    with (
        patch("litellm.cost_per_token", side_effect=ValueError("unknown model")),
        patch("litellm.completion", return_value={}) as call,
        background_usage(config, "extractor"),
    ):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=[], max_tokens=100)
        call.assert_called_once()


def test_vision_reserves_context_limit_for_compressed_images(tmp_path):
    config = settings(tmp_path, 0.01)
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,a"}}],
        }
    ]
    with (
        patch("litellm.get_model_info", return_value={"max_input_tokens": 100_000}),
        patch("litellm.completion") as call,
        background_usage(config, "extractor"),
    ):
        with pytest.raises(BackgroundBudgetExceeded):
            provider_completion(model="test/model", messages=messages, max_tokens=100)
        call.assert_not_called()


def test_plaintext_cli_is_read_only(tmp_path, capsys):
    from scripts.background_usage import main

    config = settings(tmp_path)
    with patch("litellm.completion", return_value={}), background_usage(config, "worker"):
        provider_completion(model="test/model", messages=[])
    before = (tmp_path / "usage.db").read_bytes()
    with patch("scripts.background_usage.Settings", return_value=config):
        assert main([]) == 0
    assert '"retained_reservations_usd"' in capsys.readouterr().out
    assert (tmp_path / "usage.db").read_bytes() == before


def test_ledger_unavailable_defers_before_provider(tmp_path):
    with (
        patch("afair.agents.usage._connection", side_effect=RuntimeError("bad encrypted ledger")),
        patch("litellm.completion") as call,
        background_usage(settings(tmp_path), "extractor"),
    ):
        with pytest.raises(BackgroundBudgetExceeded, match="ledger unavailable"):
            call_json(model="test/model", system="s", user="u")
        call.assert_not_called()


def test_plaintext_ledger_with_encrypted_settings_defers(tmp_path):
    config = settings(tmp_path)
    _connection(config).close()
    encrypted = config.model_copy(
        update={
            "vault_key": __import__("pydantic").SecretStr("test-only-key-32-bytes-123456789012345")
        }
    )
    with patch("litellm.completion") as call, background_usage(encrypted, "extractor"):
        with pytest.raises(BackgroundBudgetExceeded):
            provider_completion(model="test/model", messages=[])
        call.assert_not_called()


def test_settlement_failure_preserves_paid_response_and_reservation(tmp_path):
    from afair.agents.usage import usage_summary

    config = settings(tmp_path)
    response = {"usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    with (
        patch("litellm.completion", return_value=response),
        patch("afair.agents.usage._settle", side_effect=RuntimeError("disk full")),
        background_usage(config, "extractor"),
    ):
        assert provider_completion(model="test/model", messages=[]) is response
    row = usage_summary(config)["workers"][0]
    assert row["unsettled_attempts"] == 1
    assert row["retained_reservations_usd"] > 0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, None])
def test_rejections_release_but_ambiguous_failures_retain(tmp_path, status):
    from afair.agents.usage import usage_summary

    class ProviderFailure(Exception):
        status_code = status

    config = settings(tmp_path)
    error = ProviderFailure("request failed")
    with patch("litellm.completion", side_effect=error), background_usage(config, "extractor"):
        with pytest.raises(ProviderFailure) as caught:
            provider_completion(model="test/model", messages=[])
        assert caught.value is error
    row = usage_summary(config)["workers"][0]
    assert (row["retained_reservations_usd"] == 0) == (status in (400, 401, 403, 404, 429))


def test_haiku_override_is_merged_and_respected_at_settlement(tmp_path):
    from afair.agents.usage import usage_summary

    config = Settings(
        _env_file=None,
        vault_dir=tmp_path,
        background_daily_budget_usd=1,
        background_model_prices={"anthropic/claude-haiku-5-5": (1, 2), "local/test": (0, 0)},
    )
    response = {"usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    with patch("litellm.completion", return_value=response), background_usage(config, "extractor"):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=[])
    assert usage_summary(config)["workers"][0]["settled_estimated_usd"] == pytest.approx(0.0012)
    assert "anthropic/claude-haiku-5-5" in settings(tmp_path).background_model_prices


def test_haiku_vision_fits_one_dollar_without_cache_creation(tmp_path):
    config = Settings(_env_file=None, vault_dir=tmp_path, background_daily_budget_usd=1)
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,a"}}],
        }
    ]
    with (
        patch("litellm.get_model_info", side_effect=ValueError("old map")),
        patch("litellm.completion", return_value={}) as call,
        background_usage(config, "extractor"),
    ):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=messages, max_tokens=2000)
        call.assert_called_once()


def test_installed_anthropic_adapter_counts_cached_prompt_once(tmp_path):
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    from afair.agents.usage import usage_summary

    normalized = AnthropicConfig().calculate_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 0},
        },
        None,
    )
    assert normalized.prompt_tokens == 600  # Includes both caches, not only raw input.
    config = Settings(_env_file=None, vault_dir=tmp_path, background_daily_budget_usd=1)
    with (
        patch("litellm.completion", return_value={"usage": normalized}),
        background_usage(config, "extractor"),
    ):
        provider_completion(model="anthropic/claude-haiku-5-5", messages=[])
    row = usage_summary(config)["workers"][0]
    assert row["cache_read_tokens"] == 200
    assert row["cache_write_tokens"] == 300
    expected = (100 * 0.10 + 200 * 0.01 + 300 * 0.125 + 10 * 0.50) / 1_000_000
    assert row["settled_estimated_usd"] == pytest.approx(expected)


def test_failure_settlement_cannot_replace_original_provider_error(tmp_path):
    config = settings(tmp_path)
    conn = _connection(config)

    class UpdateFailure:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE"):
                raise RuntimeError("settlement disk error")
            return conn.execute(sql, *args)

    original = TimeoutError("ambiguous provider timeout")
    with (
        patch("afair.agents.usage._connection", return_value=UpdateFailure()),
        patch("litellm.completion", side_effect=original),
        background_usage(config, "extractor"),
    ):
        with pytest.raises(TimeoutError) as caught:
            provider_completion(model="test/model", messages=[])
        assert caught.value is original

"""The holdout is independent; tests assert pipeline execution, not a fake pass."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace

import pytest

from afair.eval import chronological_replay as replay
from afair.mcp import context
from afair.substrate import open_db


@pytest.fixture(scope="module")
def report():
    # Even accidental provider use cannot reach the network in offline mode.
    from unittest.mock import patch

    with patch.object(socket, "create_connection", side_effect=AssertionError("offline network")):
        return replay.run_replay()


def test_holdout_is_frozen_and_separate_from_runner():
    assert hashlib.sha256(replay.FIXTURE.read_bytes()).hexdigest() == replay.FIXTURE_SHA256
    assert {c["family"] for c in replay.load_holdout()} == {
        "namesakes",
        "cross_client_correction",
        "explicit_withdrawal",
        "unknown",
        "negative_control",
        "noisy_observations",
        "multimodal",
    }


def test_digest_change_is_rejected(tmp_path, monkeypatch):
    altered = tmp_path / "changed.jsonl"
    altered.write_bytes(replay.FIXTURE.read_bytes() + b"\n")
    monkeypatch.setattr(replay, "FIXTURE", altered)
    with pytest.raises(ValueError, match="digest mismatch"):
        replay.load_holdout()


def test_offline_report_is_explicitly_not_model_quality(report):
    assert report["mode"] == "offline"
    assert "wiring baseline only" in report["quality_claim"]
    assert report["passed"] is False  # The weak baseline must not fake model-quality success.
    assert report["usage"]["reserved_upper_cost_usd"] == 0
    assert 0 < report["usage"]["calls"] <= 48
    assert len(report["families"]) == 7
    assert not any("error" in case for case in report["cases"])


def test_actual_extraction_recall_provenance_and_synthesis_are_exercised(report):
    for case in report["cases"]:
        assert case["interpretations"]
        assert all(row["extraction"]["status"] == "success" for row in case["interpretations"])
        assert {o["client"] for o in case["outputs"]} == {"first", "second"}
        assert all("retrieved_source_ids" in o for o in case["outputs"])
        assert (
            case["automatic_synthesis"]["written"] == 0
        )  # Frozen cases are below discovery floor.
        assert case["queried_source_synthesis"] is not None
        assert case["queried_source_synthesis"]["citation_validity"] is True
        assert set(case["source_ids"]) == {
            x["id"]
            for x in next(c for c in replay.load_holdout() if c["id"] == case["id"])["inputs"]
        }
    assert report["usage"]["operations"]["record_extraction"] > 0
    assert report["usage"]["operations"]["replay_answer"] == 14
    assert report["usage"]["operations"]["write_living_synthesis"] == 7


def test_chronological_prefix_cannot_contain_future_correction(report):
    case = next(c for c in report["cases"] if c["id"] == "handoff")
    first, second = case["chronology"]
    assert first["event_count"] == 1
    assert second["event_count"] == 2
    assert case["source_ids"]["b"][0] not in first["source_ids"]
    assert case["source_ids"]["b"][0] in second["source_ids"]
    assert {(row["event_id"], row["client"]) for row in case["provenance"]} == {
        (case["source_ids"]["a"][0], "first"),
        (case["source_ids"]["b"][0], "second"),
    }


def test_observation_repeats_exercise_actual_dedup(report):
    case = next(c for c in report["cases"] if c["id"] == "noise")
    assert len(case["source_ids"]["b"]) == 8
    assert len(set(case["source_ids"]["b"])) == 1
    assert case["chronology"][-1]["event_count"] == 2


def test_multimodal_uses_png_pixels_and_production_vision_extraction(report):
    png = replay._card_png("CARD: MOSS 47")
    assert png.startswith(b"\x89PNG")
    assert b"MOSS" not in png  # No plaintext metadata shortcut.
    assert replay._read_card(png) == "CARD: MOSS 47"
    case = next(c for c in report["cases"] if c["id"] == "graphic")
    interpretation = case["interpretations"][0]
    assert interpretation["produced_by"].startswith("extractor:image:")
    assert interpretation["extraction"]["summary"] == "CARD: MOSS 47"


def test_changed_labels_do_not_change_generated_output(monkeypatch):
    case = replay.load_holdout()[0]
    config = replay.ReplayConfig()
    first = replay._run_case(case, config, replay._Budget(config, None))
    # Alter only scorer labels, not sources/query. The same pipeline must
    # generate the same answer independent of the desired result.
    changed = {**case, "expected": "unreachable sentinel", "evidence": [], "forbidden": ["copper"]}
    second = replay._run_case(changed, config, replay._Budget(config, None))
    assert [o["answer"] for o in first["outputs"]] == [o["answer"] for o in second["outputs"]]
    assert second["scores"]["answer_correct"] == 0


def test_citation_and_unknown_negative_paths_fail():
    case = replay.load_holdout()[0]
    scores = replay._score(
        case,
        [{"answer": "copper", "abstained": False, "citations": ["invented"]}],
        {"a": ["old"], "b": ["new"]},
    )
    assert scores["answer_correct"] == 1
    assert scores["citation_validity"] == scores["evidence_support"] == 0
    unknown = next(c for c in replay.load_holdout() if c["id"] == "unknown")
    scores = replay._score(
        unknown, [{"answer": "invented", "abstained": False, "citations": ["a"]}], {"a": ["a"]}
    )
    assert scores["answer_correct"] == scores["abstention_accuracy"] == 0


def test_temporary_vaults_are_deleted_and_context_cleared(tmp_path, monkeypatch):
    import tempfile

    paths = []
    original = tempfile.TemporaryDirectory

    def tracked(*args, **kwargs):
        directory = original(*args, dir=tmp_path, **kwargs)
        paths.append(directory.name)
        return directory

    monkeypatch.setattr(replay.tempfile, "TemporaryDirectory", tracked)
    result = replay.run_replay(replace(replay.ReplayConfig(), max_calls=1))
    assert result["passed"] is False
    assert result["usage"]["calls"] == 1
    assert len(paths) == 8  # One isolated working directory plus seven fresh vaults.
    assert list(tmp_path.iterdir()) == []
    assert context._context is None
    assert any("error" in c for c in result["cases"])


def test_active_vault_is_never_redirected(tmp_path):
    conn = open_db(tmp_path)
    active = context.ServerContext(vault_dir=tmp_path, inline_text_max_bytes=1024, db=conn)
    context.set_context(active)
    try:
        with pytest.raises(RuntimeError, match="fresh process"):
            replay.run_replay()
        assert context.get_context() is active
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        context.clear_context()
        conn.close()


def test_provider_requires_explicit_models_and_dedicated_key():
    with pytest.raises(ValueError, match="explicit text and vision models"):
        replay.ReplayConfig(mode="provider")
    config = replay.ReplayConfig(mode="provider", model="test/model", vision_model="test/vision")
    with pytest.raises(ValueError, match="dedicated explicit"):
        replay.run_replay(config)
    with pytest.raises(ValueError, match="does not accept credentials"):
        replay.run_replay(api_key="synthetic-test-only")


def test_provider_budget_reserves_before_dispatch_and_disables_retries(monkeypatch):
    import litellm

    calls = []
    monkeypatch.setattr(litellm, "cost_per_token", lambda **kwargs: (0.01, 0.01))

    def completion(**kwargs):
        calls.append(kwargs)
        return "synthetic-provider-response"

    monkeypatch.setattr(litellm, "completion", completion)
    config = replay.ReplayConfig(
        mode="provider", model="test/model", vision_model="test/vision", max_cost_usd=0.05
    )
    budget = replay._Budget(config, "synthetic-test-only")
    kwargs = {
        "model": "test/model",
        "messages": [{"role": "user", "content": "test"}],
        "tools": [{"function": {"name": "replay_answer"}}],
        "max_tokens": 9999,
    }
    assert budget.completion(**kwargs) == "synthetic-provider-response"
    assert calls[0]["api_key"] == "synthetic-test-only"
    assert calls[0]["num_retries"] == 0
    assert calls[0]["max_tokens"] == config.max_output_tokens
    with pytest.raises(RuntimeError, match="dollar budget"):
        budget.completion(**kwargs)
    assert len(calls) == 1
    assert budget.reserved_usd == 0.04
    assert "synthetic-test-only" not in repr(budget)


def test_unknown_provider_price_fails_before_dispatch(monkeypatch):
    import litellm

    def unavailable(**kwargs):
        raise ValueError("unknown pricing")

    monkeypatch.setattr(litellm, "cost_per_token", unavailable)
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: pytest.fail("must not dispatch"))
    config = replay.ReplayConfig(mode="provider", model="test/model", vision_model="test/vision")
    with pytest.raises(RuntimeError, match="pricing unavailable"):
        replay._Budget(config, "synthetic").completion(
            model="test/model", messages=[], tools=[{"function": {"name": "test"}}]
        )


def test_abstention_flag_cannot_hide_an_invented_answer():
    unknown = next(c for c in replay.load_holdout() if c["id"] == "unknown")
    scores = replay._score(
        unknown,
        [{"answer": "registration code ABC", "abstained": True, "citations": []}],
        {"a": ["a"]},
    )
    assert scores["answer_correct"] == scores["abstention_accuracy"] == 0


def test_provider_failure_does_not_put_key_in_report(monkeypatch):
    import litellm

    secret = "synthetic-evaluation-key-never-publish"
    monkeypatch.setattr(litellm, "cost_per_token", lambda **kwargs: (0.001, 0.001))

    def failing(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(litellm, "completion", failing)
    config = replay.ReplayConfig(
        mode="provider", model="test/model", vision_model="test/vision", max_calls=1
    )
    result = replay.run_replay(config, api_key=secret)
    assert result["passed"] is False
    assert secret not in str(result)
    assert result["usage"]["calls"] == 1


def test_cli_emits_valid_json_and_honest_nonzero_gate_exit():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "afair.eval.chronological_replay"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["mode"] == "offline"
    assert len(report["cases"]) == 7
    assert report["passed"] is False


def test_provider_mode_runs_same_production_paths_with_fake_transport(monkeypatch):
    import litellm

    calls = []
    offline = replay._Budget(replay.ReplayConfig(), None)
    monkeypatch.setattr(litellm, "cost_per_token", lambda **kwargs: (0.00001, 0.00001))

    def completion(**kwargs):
        calls.append(kwargs)
        assert kwargs["api_key"] == "synthetic-eval-only"
        return offline.completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    config = replay.ReplayConfig(mode="provider", model="test/model", vision_model="test/vision")
    result = replay.run_replay(config, api_key="synthetic-eval-only")
    assert result["mode"] == "provider"
    assert "synthetic holdout" in result["quality_claim"]
    assert not any("error" in case for case in result["cases"])
    assert result["usage"]["calls"] == len(calls) > 0
    assert 0 < result["usage"]["reserved_upper_cost_usd"] <= config.max_cost_usd
    assert any(call["model"] == "test/vision" for call in calls)


def test_time_and_request_size_budgets_stop_before_dispatch():
    config = replay.ReplayConfig()
    kwargs = {
        "model": config.model,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"function": {"name": "replay_answer"}}],
    }
    budget = replay._Budget(config, None, started=0)
    with pytest.raises(RuntimeError, match="call/time budget"):
        budget.completion(**kwargs)
    budget = replay._Budget(config, None)
    kwargs["messages"] = [{"role": "user", "content": "x" * 70_000}]
    with pytest.raises(RuntimeError, match="input byte budget"):
        budget.completion(**kwargs)
    assert budget.calls == 0


def test_product_projection_repairs_are_distinct_from_baseline_answer_failures(report):
    image = next(case for case in report["cases"] if case["id"] == "graphic")
    assert image["queried_source_synthesis"]["key_point_count"] > 0
    assert "MOSS 47" in image["queried_source_synthesis"]["key_points"][0]["point"]
    assert image["scores"]["answer_correct"] == 0  # Lexical answer adapter still abstains.
    withdrawal = next(case for case in report["cases"] if case["id"] == "withdraw")
    assert all(
        output["retrieved_source_ids"][0] == withdrawal["source_ids"]["b"][0]
        for output in withdrawal["outputs"]
    )
    assert (
        withdrawal["scores"]["answer_correct"] == 0
    )  # It re-ranks lexically, ignoring source chronology.


def test_report_records_product_revision_code_and_actual_prompt_hashes(report):
    import re
    from pathlib import Path

    from afair import __version__

    provenance = report["run_provenance"]
    assert provenance["product_version"] == __version__
    assert provenance["build_revision"] == "unknown" or re.fullmatch(
        r"[0-9a-f]{40}", provenance["build_revision"]
    )
    package = Path(replay.__file__).resolve().parents[1]
    for path, digest in provenance["code_sha256"].items():
        assert digest == hashlib.sha256((package / path).read_bytes()).hexdigest()
    assert set(provenance["template_sha256"]) == {
        "extractor_system",
        "synthesis_system",
        "answer_system",
        "answer_schema",
    }
    requests = provenance["request_prompt_sha256"]
    assert sum(map(len, requests.values())) == report["usage"]["calls"]
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", digest) for hashes in requests.values() for digest in hashes
    )


def test_family_metric_numerators_and_denominators_are_explicit(report):
    for family, metrics in report["family_metrics"].items():
        for name, value in metrics.items():
            assert value["denominator"] == (1 if name == "cross_client_consistency" else 2)
            assert 0 <= value["numerator"] <= value["denominator"]
            assert value["score"] == value["numerator"] / value["denominator"]
            assert report["families"][family][name] == value["score"]


def test_replay_exercises_actual_vector_storage_normal_recall_and_rank_fusion(report):
    assert report["embedding"]["model"] == replay.EMBEDDING_MODEL
    assert report["embedding"]["network_requests"] == 0
    assert "no learned semantic-quality claim" in report["retrieval"]
    for case in report["cases"]:
        trace = case["retrieval_trace"]
        assert trace["embedding_calls"]["document"] > 0
        assert trace["embedding_calls"]["query"] == len(case["chronology"]) + len(replay.CLIENTS)
        assert trace["vector_rows"] == len(
            {event_id for ids in case["source_ids"].values() for event_id in ids}
        )
        assert all(checkpoint["depth_used"] == "normal" for checkpoint in case["chronology"])
        assert all(output["depth_used"] == "normal" for output in case["outputs"])
    # A richer retrieval path cannot manufacture absent source details.
    for case_id in ("unknown", "control"):
        case = next(case for case in report["cases"] if case["id"] == case_id)
        assert case["scores"]["answer_correct"] == 0


def test_synthetic_local_embedding_is_deterministic_bounded_and_normalized():
    from math import isclose

    from afair.agents.embedding import EmbeddingError

    one = replay._synthetic_embedding(
        model=replay.EMBEDDING_MODEL, text="A diagram of a silver rivet"
    )
    assert one == replay._synthetic_embedding(
        model=replay.EMBEDDING_MODEL, text="A diagram of a silver rivet"
    )
    assert len(one) == replay.EMBEDDING_DIM
    assert isclose(sum(value * value for value in one), 1.0)
    assert one != replay._synthetic_embedding(model=replay.EMBEDDING_MODEL, text="A knitted scarf")
    with pytest.raises(EmbeddingError):
        replay._synthetic_embedding(model="unknown", text="test")
    with pytest.raises(EmbeddingError):
        replay._synthetic_embedding(model=replay.EMBEDDING_MODEL, text="x" * 100_001)


def test_replay_never_loads_operator_dotenv_environment_or_customer_vault(tmp_path, monkeypatch):
    import builtins
    import io
    import os
    from pathlib import Path

    from afair import settings

    operator = tmp_path / "operator"
    operator.mkdir()
    customer = operator / "customer-vault"
    customer.mkdir()
    protected = customer / "substrate.db"
    protected.write_bytes(b"synthetic customer vault: never read")
    sentinel = "synthetic-operator-secret-must-not-enter-replay"
    for name in (".env", ".env.local"):
        (operator / name).write_text(
            f"OPENAI_API_KEY={sentinel}\nAFAIR_VAULT_KEY={sentinel}\nENVIRONMENT=fly\nVAULT_DIR={customer}\n"
        )
    monkeypatch.chdir(operator)
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AFAIR_VAULT_KEY",
        "AFAIR_AUTH_TOKEN",
        "AFAIR_EVAL_API_KEY",
    ):
        monkeypatch.setenv(name, sentinel)
    monkeypatch.setenv("ENVIRONMENT", "fly")
    monkeypatch.setenv("VAULT_DIR", str(customer))
    setting_calls = []

    def forbidden_settings():
        setting_calls.append(True)
        raise AssertionError("load_settings must never run during replay")

    monkeypatch.setattr(settings, "load_settings", forbidden_settings)
    originals = (builtins.open, io.open)
    protected_reads = []

    def guard(original):
        def opened(file, mode="r", *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(file)).resolve()
                if (
                    path in {(operator / ".env").resolve(), (operator / ".env.local").resolve()}
                    or path == customer
                    or customer in path.parents
                ):
                    protected_reads.append(str(path))
                    raise AssertionError("operator settings/customer files must not be opened")
            return original(file, mode, *args, **kwargs)

        return opened

    monkeypatch.setattr(builtins, "open", guard(originals[0]))
    monkeypatch.setattr(io, "open", guard(originals[1]))
    original_completion = replay._Budget.completion

    def checked_completion(self, **kwargs):
        assert not any(
            name in os.environ
            for name in (
                "AFAIR_VAULT_KEY",
                "VAULT_DIR",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "ENVIRONMENT",
                "AFAIR_EVAL_API_KEY",
            )
        )
        configured = context.get_context().background_usage_settings
        assert configured is not None and configured.vault_key is None
        assert configured.openai_api_key is None and configured.anthropic_api_key is None
        assert customer not in configured.vault_dir.parents
        return original_completion(self, **kwargs)

    monkeypatch.setattr(replay._Budget, "completion", checked_completion)
    report = replay.run_replay()
    assert not any("error" in case for case in report["cases"])
    assert setting_calls == protected_reads == []
    assert sentinel not in str(report)
    assert os.environ["AFAIR_VAULT_KEY"] == sentinel
    assert Path.cwd() == operator


def test_any_future_ambient_settings_constructor_fails_closed():
    from afair.settings import Settings

    with replay._isolated_runtime(), pytest.raises(RuntimeError, match="forbids ambient Settings"):
        Settings()

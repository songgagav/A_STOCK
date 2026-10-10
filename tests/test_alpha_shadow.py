from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from alpha_shadow import (  # noqa: E402
    ARM_NAMES,
    build_experiment_manifest,
    evaluate_shadow_arms,
    rank_arm,
    write_manifest,
)


def _manifest(**overrides):
    payload = {
        "code_sha": "abc123",
        "input_hashes": {"pit": "a" * 64, "forward": "b" * 64},
        "universe_symbols": ["600002.SH", "600001.SH"],
        "factor_version": "factor-v1",
        "direction_version": "direction-a",
        "weight_version": "weights-v1",
        "selector_variant": "production-default",
        "env_flags": {"RANK_BY_FUSION": "0"},
    }
    payload.update(overrides)
    return build_experiment_manifest(**payload)


def test_manifest_is_stable_and_records_experiment_identity():
    first = _manifest()
    second = _manifest(universe_symbols=["600001.SH", "600002.SH"])

    assert first["experiment_hash"] == second["experiment_hash"]
    assert first["experiment_config_hash"]
    assert first["universe_hash"]
    assert first["arms"] == list(ARM_NAMES)


def test_rank_arm_uses_named_score_and_deterministic_tiebreak():
    rows = [
        {"symbol": "600002.SH", "scores": {"signal": 1.0}},
        {"symbol": "600001.SH", "scores": {"signal": 1.0}},
        {"symbol": "600003.SH", "scores": {"signal": 0.5}},
    ]

    assert [row["symbol"] for row in rank_arm(rows, "legacy_signal")] == [
        "600001.SH",
        "600002.SH",
        "600003.SH",
    ]


def test_unknown_arm_is_rejected():
    with pytest.raises(ValueError, match="unknown shadow arm"):
        rank_arm([], "made_up")


def test_shadow_comparison_reports_top_n_overlap_and_rank_correlation():
    rows = [
        {
            "symbol": "600001.SH",
            "scores": {
                "score": 0.9,
                "signal": 0.1,
                "fusion_A": 0.2,
                "selector_score": 0.9,
                "fusion_rank_on": 0.8,
            },
        },
        {
            "symbol": "600002.SH",
            "scores": {
                "score": 0.8,
                "signal": 0.9,
                "fusion_A": 0.3,
                "selector_score": 0.8,
                "fusion_rank_on": 0.7,
            },
        },
        {
            "symbol": "600003.SH",
            "scores": {
                "score": 0.7,
                "signal": 0.2,
                "fusion_A": 0.9,
                "selector_score": 0.7,
                "fusion_rank_on": 0.95,
            },
        },
    ]

    result = evaluate_shadow_arms(rows, top_n=2)

    assert result["arms"]["control_prod"]["symbols"] == ["600001.SH", "600002.SH"]
    assert result["arms"]["legacy_signal"]["symbols"] == ["600002.SH", "600003.SH"]
    comparison = result["comparisons"]["legacy_signal"]
    assert comparison["top_n_jaccard"] == pytest.approx(1 / 3)
    assert comparison["rank_correlation"] == pytest.approx(-0.5)


def test_manifest_is_written_under_hash_directory(tmp_path: Path):
    manifest = _manifest()

    path = write_manifest(tmp_path, manifest)

    assert path == tmp_path / manifest["experiment_hash"] / "manifest.json"
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith("{")


def _metric_rows():
    rows = []
    values = [
        ("600001.SH", 0.90, 0.10, 0.03, 0.05),
        ("600002.SH", 0.80, 0.20, 0.02, 0.01),
        ("600003.SH", 0.70, 0.30, 0.04, -0.01),
        ("600004.SH", 0.60, 0.40, 0.01, 0.03),
    ]
    for symbol, score, signal, return_1d, return_5d in values:
        rows.append(
            {
                "symbol": symbol,
                "scores": {
                    "score": score,
                    "signal": signal,
                    "fusion_A": score,
                    "selector_score": score,
                    "fusion_rank_on": score,
                },
                "forward_returns": {"1": return_1d, "5": return_5d},
            }
        )
    return rows


def test_shadow_metrics_report_forward_returns_rank_ic_and_turnover():
    result = evaluate_shadow_arms(
        _metric_rows(),
        top_n=2,
        previous_symbols=["600002.SH", "600004.SH"],
        forward_horizons=(1, 5, 10),
    )

    control = result["arms"]["control_prod"]

    assert control["forward_return_mean"]["1"] == pytest.approx(0.025)
    assert control["forward_return_mean"]["5"] == pytest.approx(0.03)
    assert control["forward_return_mean"]["10"] is None
    assert control["rank_ic"]["1"] is not None
    assert control["rank_ic"]["5"] is not None
    assert control["turnover"] == pytest.approx(0.5)


def test_shadow_metrics_mark_missing_previous_holdings_and_returns_explicitly():
    rows = _metric_rows()
    for row in rows:
        row.pop("forward_returns")

    result = evaluate_shadow_arms(rows, top_n=2, forward_horizons=(1, 5))
    control = result["arms"]["control_prod"]

    assert control["forward_return_mean"] == {"1": None, "5": None}
    assert control["rank_ic"] == {"1": None, "5": None}
    assert control["turnover"] is None


def test_shadow_metrics_report_explicit_excess_and_cost_sensitivity():
    result = evaluate_shadow_arms(
        _metric_rows(),
        top_n=2,
        previous_symbols=["600002.SH", "600004.SH"],
        benchmark_returns={"1": 0.01, "5": 0.02},
        benchmark_name="equal_weight_xsec_available",
        cost_bps=10.0,
        forward_horizons=(1, 5),
    )

    control = result["arms"]["control_prod"]
    assert result["benchmark"] == {
        "name": "equal_weight_xsec_available",
        "returns": {"1": 0.01, "5": 0.02},
    }
    assert control["excess_return_mean"] == {"1": pytest.approx(0.015), "5": pytest.approx(0.01)}
    assert control["cost_adjusted_forward_return_mean"] == {
        "1": pytest.approx(0.0245),
        "5": pytest.approx(0.0295),
    }


def test_shadow_metrics_leave_excess_and_cost_null_without_required_inputs():
    result = evaluate_shadow_arms(_metric_rows(), top_n=2, forward_horizons=(1,))

    control = result["arms"]["control_prod"]
    assert "benchmark" not in result
    assert control["excess_return_mean"] == {"1": None}
    assert control["cost_adjusted_forward_return_mean"] == {"1": None}


def test_shadow_metrics_report_forward_coverage_and_q1_q5_monotonicity():
    rows = []
    for index in range(10):
        score = 1.0 - index * 0.1
        rows.append(
            {
                "symbol": f"6000{index:02d}.SH",
                "scores": {
                    "score": score,
                    "signal": score,
                    "fusion_A": score,
                    "selector_score": score,
                    "fusion_rank_on": score,
                },
                "forward_returns": {"1": 0.10 - index * 0.01},
            }
        )

    result = evaluate_shadow_arms(rows, top_n=2, forward_horizons=(1, 5))

    control = result["arms"]["control_prod"]
    assert control["forward_observation_coverage"] == {"1": 1.0, "5": 0.0}
    assert control["quantile_forward_return_mean"]["1"] == {
        "q1": pytest.approx(0.015),
        "q2": pytest.approx(0.035),
        "q3": pytest.approx(0.055),
        "q4": pytest.approx(0.075),
        "q5": pytest.approx(0.095),
    }
    assert control["quantile_monotonicity"]["1"] == {
        "spread_q5_q1": pytest.approx(0.08),
        "is_non_decreasing": True,
        "observed_buckets": 5,
    }
    assert control["quantile_monotonicity"]["5"] == {
        "spread_q5_q1": None,
        "is_non_decreasing": None,
        "observed_buckets": 0,
    }

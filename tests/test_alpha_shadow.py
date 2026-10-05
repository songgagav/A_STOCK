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

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "scripts/research")

from alpha_shadow_compare import run_shadow_file  # noqa: E402


def _input_payload():
    rows = []
    for index, symbol in enumerate(
        ("600001.SH", "600002.SH", "600003.SH", "600004.SH")
    ):
        score = 1.0 - index * 0.1
        rows.append(
            {
                "symbol": symbol,
                "scores": {
                    "score": score,
                    "signal": score,
                    "fusion_A": score,
                    "selector_score": score,
                    "fusion_rank_on": score,
                },
                "forward_returns": {"1": 0.01 * (index + 1)},
            }
        )
    return {
        "schema_version": 1,
        "trade_day": "2026-09-01",
        "rows": rows,
        "previous_symbols": ["600002.SH", "600004.SH"],
        "input_hashes": {"pit": "a" * 64, "forward": "b" * 64},
    }


def test_run_shadow_file_writes_manifest_and_result_atomically(tmp_path: Path):
    input_path = tmp_path / "shadow_input.json"
    input_path.write_text(
        json.dumps(_input_payload(), ensure_ascii=False), encoding="utf-8"
    )

    output = run_shadow_file(
        input_path,
        tmp_path / "cache",
        code_sha="abc123",
        factor_version="factor-v1",
        direction_version="direction-a",
        weight_version="weights-v1",
        selector_variant="production-default",
        env_flags={"RANK_BY_FUSION": "0"},
        top_n=2,
        forward_horizons=(1,),
    )

    assert output["trade_day"] == "2026-09-01"
    assert output["manifest_path"].exists()
    assert output["result_path"].exists()
    result = json.loads(output["result_path"].read_text(encoding="utf-8"))
    assert result["experiment_hash"] == output["experiment_hash"]
    assert result["arms"]["control_prod"]["forward_return_mean"]["1"] == pytest.approx(
        0.015
    )


def test_run_shadow_file_rejects_missing_normalized_rows(tmp_path: Path):
    input_path = tmp_path / "invalid.json"
    input_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    with pytest.raises(ValueError, match="rows"):
        run_shadow_file(
            input_path,
            tmp_path / "cache",
            code_sha="abc123",
            factor_version="factor-v1",
            direction_version="direction-a",
            weight_version="weights-v1",
            selector_variant="production-default",
            env_flags={},
        )


def test_run_shadow_file_rejects_unknown_input_schema(tmp_path: Path):
    payload = _input_payload()
    payload["schema_version"] = 2
    input_path = tmp_path / "future.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        run_shadow_file(
            input_path,
            tmp_path / "cache",
            code_sha="abc123",
            factor_version="factor-v1",
            direction_version="direction-a",
            weight_version="weights-v1",
            selector_variant="production-default",
            env_flags={},
        )

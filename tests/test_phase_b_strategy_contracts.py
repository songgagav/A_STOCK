"""Phase B strategy-contract tests.

These tests intentionally describe the contract before the implementation is
wired into the existing selector/realtime paths.
"""
from __future__ import annotations

import json
import math
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))


def test_strategy_modes_default_to_shadow_and_reject_invalid(monkeypatch):
    from strategy_contract import fusion_weight_mode, drl_plan_mode

    monkeypatch.delenv("DRL_PLAN_MODE", raising=False)
    monkeypatch.delenv("FUSION_WEIGHT_MODE", raising=False)
    assert drl_plan_mode() == "shadow"
    assert fusion_weight_mode() == "shadow"

    monkeypatch.setenv("DRL_PLAN_MODE", "unexpected")
    monkeypatch.setenv("FUSION_WEIGHT_MODE", "unexpected")
    assert drl_plan_mode() == "shadow"
    assert fusion_weight_mode() == "shadow"


def test_shadow_drl_plan_is_never_consumable_without_promotion(monkeypatch):
    from strategy_contract import drl_plan_is_consumable

    monkeypatch.setenv("DRL_PLAN_MODE", "shadow")
    assert drl_plan_is_consumable({"promotion": {"approved": True}}) is False

    monkeypatch.setenv("DRL_PLAN_MODE", "enforce")
    assert drl_plan_is_consumable({"promotion": {"approved": False}}) is False
    assert drl_plan_is_consumable({"promotion": {"approved": True}}) is True


def test_nested_weights_are_the_single_authoritative_payload(tmp_path, monkeypatch):
    import weight_optimizer as wo

    path = tmp_path / "weights.json"
    path.write_text(json.dumps({
        "weights": {"signal": 2.0, "trend": 1.0},
        "meta": {"expected_directions": {"signal": 1, "trend": 1}},
    }), encoding="utf-8")
    monkeypatch.setattr(wo, "WEIGHTS_FILE", str(path))

    weights = wo.load_weights()
    assert weights == {"signal": pytest.approx(2 / 3),
                       "trend": pytest.approx(1 / 3)}


def test_invalid_dynamic_weights_do_not_become_authoritative(tmp_path, monkeypatch):
    import weight_optimizer as wo

    path = tmp_path / "weights.json"
    path.write_text(json.dumps({
        "weights": {"signal": math.nan, "trend": 1.0},
    }), encoding="utf-8", errors="ignore")
    monkeypatch.setattr(wo, "WEIGHTS_FILE", str(path))

    with pytest.raises(wo.WeightContractError):
        wo.load_authoritative_weights()


def test_signed_icir_uses_declared_direction_not_absolute_value():
    from weight_optimizer import signed_icir_strength

    assert signed_icir_strength(-2.0, expected_direction=1) == 0.0
    assert signed_icir_strength(-2.0, expected_direction=-1) == 2.0
    assert signed_icir_strength(2.0, expected_direction=-1) == 0.0


def test_normalize_weights_rewrites_sum_to_one_and_rejects_invalid():
    from target_weighting import normalize_weights

    items = [{"canon": "600000.SH", "target_weight": 2.0},
             {"canon": "000001.SZ", "target_weight": 1.0}]
    total = normalize_weights(items)
    assert total == pytest.approx(1.0)
    assert [x["target_weight"] for x in items] == [pytest.approx(2 / 3),
                                                    pytest.approx(1 / 3)]

    with pytest.raises(ValueError):
        normalize_weights([{"canon": "600000.SH", "target_weight": 0.0}])


def test_average_rank_gives_ties_the_average_position():
    from strategy_contract import average_rank

    assert average_rank([1.0, 1.0, 3.0]) == pytest.approx([0.25, 0.25, 1.0])


def test_target_contract_preserves_provenance_fields():
    from strategy_contract import normalize_target_contract

    source = [{
        "canon": "600000.SH",
        "name": "demo",
        "score": 0.8,
        "target_weight": 1.0,
        "source_signal": "BUY",
        "source": "selector",
        "provenance": {"weights_sha": "abc"},
    }]
    result = normalize_target_contract(source, require_weights=True)
    assert result == source


def test_shadow_fusion_weighting_cannot_mutate_authoritative_targets(monkeypatch):
    from target_weighting import ensure_target_weights

    monkeypatch.setenv("FUSION_WEIGHT_MODE", "shadow")
    items = [{"canon": "600000.SH"}, {"canon": "000001.SZ"}]
    ensure_target_weights(items, as_of="2026-10-07")
    assert [item["target_weight"] for item in items] == [pytest.approx(0.5),
                                                           pytest.approx(0.5)]


def test_target_weight_rank_ties_are_equal(monkeypatch):
    from target_weighting import allocate_target_weights

    monkeypatch.setenv("FUSION_WEIGHT_MODE", "enforce")
    items = [{"canon": "600000.SH", "fml": 1.0},
             {"canon": "000001.SZ", "fml": 1.0},
             {"canon": "300750.SZ", "fml": 0.0}]
    allocate_target_weights(items)
    assert items[0]["target_weight"] == pytest.approx(items[1]["target_weight"])


def test_enforce_fusion_failure_is_blocked_not_legacy_fallback(monkeypatch):
    import factor_fusion as fusion

    monkeypatch.setenv("FUSION_WEIGHT_MODE", "enforce")
    monkeypatch.setattr(fusion, "cross_section_scores",
                        lambda as_of, symbols=None: {
                            "n_pool": 100, "n_scored": 100,
                            "scores": {"600000": 0.1},
                        })
    with pytest.raises(fusion.FusionContractError):
        fusion.fusion_or_fml([{"canon": "600000.SH"},
                              {"canon": "000001.SZ"}], "2026-10-07")


def test_snapshot_rejects_nonpositive_or_nonunit_weights():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from signal_snapshot import build_snapshot

    at = datetime(2026, 10, 7, 9, 25, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(ValueError):
        build_snapshot("20261007", [{"canon": "600000.SH", "target_weight": 0.0}],
                       "selection", "20261006", [], at, _ROOT)
    with pytest.raises(ValueError):
        build_snapshot("20261007", [
            {"canon": "600000.SH", "target_weight": 0.8},
            {"canon": "000001.SZ", "target_weight": 0.8},
        ], "selection", "20261006", [], at, _ROOT)


def test_realtime_shadow_skips_drl_and_uses_non_drl_selection(tmp_path, monkeypatch):
    import realtime_engine as realtime

    day = "2026-10-07"
    day8 = "20261007"
    daily = tmp_path / "daily" / day8
    daily.mkdir(parents=True)
    (daily / "selection.json").write_text(json.dumps({
        "top_n": [{"canon": "600000.SH", "target_weight": 1.0}],
    }), encoding="utf-8")
    monkeypatch.setenv("DRL_PLAN_MODE", "shadow")
    monkeypatch.setattr(realtime, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(realtime, "DAILY_DIR", str(tmp_path / "daily"))
    monkeypatch.setattr(realtime, "_trace_targets", lambda *args, **kwargs: None)
    monkeypatch.setattr(realtime, "_prev_trade_day", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        realtime, "_load_daily_plan_for_consume_day",
        lambda *args, **kwargs: pytest.fail("shadow must not inspect DRL plans"),
    )

    targets, _info, source_day = realtime.load_targets(day)
    assert source_day == day8
    assert targets[0]["canon"] == "600000.SH"


def test_backtest_target_normalization_keeps_contract_fields():
    from backtest_engine import BacktestRunner

    runner = object.__new__(BacktestRunner)
    result = runner._norm_targets([{
        "canon": "600000.SH",
        "name": "demo",
        "score": 0.8,
        "target_weight": 1.0,
        "source_signal": "BUY",
        "source": "selector",
        "provenance": {"weights_sha": "abc"},
    }])
    assert result[0]["target_weight"] == 1.0
    assert result[0]["source_signal"] == "BUY"
    assert result[0]["provenance"] == {"weights_sha": "abc"}

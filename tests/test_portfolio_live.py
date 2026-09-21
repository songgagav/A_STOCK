# -*- coding: utf-8 -*-
"""portfolio_live (多策略组合回测的取数接线层) 回归测试。

只测**纯函数**部分(`_canon_of` / `weights_from_targets` / `_d`)与回调构造 ——
取价/取池那两条路径依赖 h5i 与 DuckDB, 属日更集成测试范畴(见
`scripts/verify_roadmap7.py`), 在单测里不假装能覆盖。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import portfolio_live as PL  # noqa: E402


class TestCanonOf:
    @pytest.mark.parametrize("raw,expect", [
        ("600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("688981", "688981.SH"),
        ("830799", "830799.BSE"),
        ("430047", "430047.BSE"),
        ("600000.SH", "600000.SH"),
        ("000001.SZ", "000001.SZ"),
        ("1", "000001.SZ"),
    ])
    def test_mapping(self, raw, expect):
        assert PL._canon_of(raw) == expect

    def test_none_or_empty_is_not_a_fake_canon(self):
        """空值必须返回空串 —— 补成 `000000.SZ` 会让"缺 canon 的行"变成组合里一个
        不存在的持仓(看似合法的代码最危险)。"""
        assert PL._canon_of(None) == ""
        assert PL._canon_of("") == ""
        assert PL._canon_of("   ") == ""

    def test_lowercase_with_suffix_normalised(self):
        assert PL._canon_of("600000.sh") == "600000.SH"


class TestDate:
    @pytest.mark.parametrize("raw", ["2026-09-22", "20260922", "2026/09/22"])
    def test_formats(self, raw):
        assert PL._d(raw) == dt.date(2026, 9, 22)

    def test_date_passthrough(self):
        d = dt.date(2026, 9, 22)
        assert PL._d(d) is d

    def test_datetime_converted(self):
        assert PL._d(dt.datetime(2026, 9, 22, 15, 0)) == dt.date(2026, 9, 22)


class TestWeightsFromTargets:
    DAYS = ["2026-09-01", "2026-09-02"]

    def test_equal_weight_when_no_target_weight(self):
        tf = lambda d: [{"canon": "600000"}, {"canon": "000001"}]  # noqa: E731
        syms, W = PL.weights_from_targets(self.DAYS, tf)
        assert syms == ["000001.SZ", "600000.SH"]
        assert W.shape == (2, 2)
        assert W[0].sum() == pytest.approx(1.0)
        assert np.allclose(W[0], [0.5, 0.5])

    def test_uses_target_weight_when_all_present(self):
        tf = lambda d: [{"canon": "600000", "target_weight": 0.7},  # noqa: E731
                        {"canon": "000001", "target_weight": 0.3}]
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert W[0][list(_s).index("600000.SH")] == pytest.approx(0.7)

    def test_partial_weights_degrade_to_equal_for_that_day(self):
        """半带权半等权会让"总暴露"失去意义, 故整日退化为等权。"""
        tf = lambda d: [{"canon": "600000", "target_weight": 0.9},  # noqa: E731
                        {"canon": "000001"}]
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert np.allclose(W[0], [0.5, 0.5])

    def test_normalised_to_one(self):
        tf = lambda d: [{"canon": "600000", "target_weight": 3.0},  # noqa: E731
                        {"canon": "000001", "target_weight": 1.0}]
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert W[0].sum() == pytest.approx(1.0)

    def test_missing_day_is_all_zero_not_equal(self):
        """某天没有池 => 全 0(那天不持有), 不是"补等权"。"""
        tf = lambda d: ([{"canon": "600000"}] if d.endswith("01") else [])  # noqa: E731
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert W[0].sum() == pytest.approx(1.0)
        assert W[1].sum() == 0.0

    def test_targets_of_exception_yields_zero_row(self):
        def boom(d):
            raise RuntimeError("selector down")
        _s, W = PL.weights_from_targets(self.DAYS, boom)
        assert W.sum() == 0.0

    def test_weights_sum_never_exceeds_one(self):
        tf = lambda d: [{"canon": "600000", "target_weight": 5.0},  # noqa: E731
                        {"canon": "000001", "target_weight": 5.0},
                        {"canon": "300750", "target_weight": 5.0}]
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert all(row.sum() <= 1.0 + 1e-12 for row in W)

    def test_symbols_union_across_days(self):
        seq = {"2026-09-01": ["600000"], "2026-09-02": ["000001"]}
        _s, W = PL.weights_from_targets(self.DAYS, lambda d: [{"canon": c} for c in seq[d]])
        assert set(_s) == {"600000.SH", "000001.SZ"}
        assert W[0].sum() == pytest.approx(1.0) and W[1].sum() == pytest.approx(1.0)

    def test_explicit_symbols_honoured(self):
        tf = lambda d: [{"canon": "600000"}]  # noqa: E731
        syms, W = PL.weights_from_targets(self.DAYS, tf, symbols=["600000.SH", "000001.SZ"])
        assert syms == ["600000.SH", "000001.SZ"] and W.shape == (2, 2)

    def test_empty_pool_everywhere(self):
        syms, W = PL.weights_from_targets(self.DAYS, lambda d: [])
        assert syms == [] and W.shape == (2, 0)

    def test_nan_weight_degrades_to_equal(self):
        tf = lambda d: [{"canon": "600000", "target_weight": "abc"},  # noqa: E731
                        {"canon": "000001", "target_weight": 0.4}]
        _s, W = PL.weights_from_targets(self.DAYS, tf)
        assert np.allclose(W[0], [0.5, 0.5])

    def test_missing_canon_key_skipped(self):
        tf = lambda d: [{"target_weight": 1.0}, {"canon": "600000"}]  # noqa: E731
        syms, W = PL.weights_from_targets(self.DAYS, tf)
        assert syms == ["600000.SH"] and W[0, 0] == pytest.approx(1.0)


class TestTradableOf:
    def test_constructor_returns_callable(self):
        assert callable(PL.make_tradable_of(None))

    def test_price_of_constructor_returns_callable(self):
        assert callable(PL.make_price_of(None))

    def test_targets_of_hist_is_callable(self):
        assert callable(PL.make_targets_of(live_pool=False))

    def test_targets_of_live_is_callable(self):
        assert callable(PL.make_targets_of(live_pool=True))


class TestRunLivePortfolioBacktest:
    def test_empty_days_returns_error(self):
        r = PL.run_live_portfolio_backtest([])
        assert r["ok"] is False and "days" in r["error"]

    def test_weight_count_mismatch_detected(self, monkeypatch):
        """策略权重数与有效腿数不符时**明确报错**, 不静默截断/补位。"""
        import portfolio_backtest as PB

        def fake_leg(name, dates, tf, symbols=None, weight_key="target_weight"):
            return PB.StrategyWeights(name, list(dates), ["AAA"],
                                      np.ones((len(dates), 1)))

        monkeypatch.setattr(PL, "leg_from_targets", fake_leg)
        r = PL.run_live_portfolio_backtest(["2026-09-01"],
                                           legs_spec=[("a", None), ("b", None)],
                                           strategy_weights=[1.0])
        assert r["ok"] is False and "不符" in r["error"]

    def test_all_empty_pools_reports_error(self, monkeypatch):
        import portfolio_backtest as PB

        def fake_leg(name, dates, tf, symbols=None, weight_key="target_weight"):
            return PB.StrategyWeights(name, list(dates), [],
                                      np.zeros((len(dates), 0)))

        monkeypatch.setattr(PL, "leg_from_targets", fake_leg)
        r = PL.run_live_portfolio_backtest(["2026-09-01"], legs_spec=[("a", None)])
        assert r["ok"] is False and "为空" in r["error"]

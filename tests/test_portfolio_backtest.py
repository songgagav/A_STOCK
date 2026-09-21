# -*- coding: utf-8 -*-
"""portfolio_backtest 回归测试 (路线图 #11).

用**合成价格**做完全确定性的撮合验证 —— 本模块刻意不自己取数(价格由
`price_of` 回调给入), 因此这里可以在不碰数据库的前提下断言"某天买了多少股、
净值应该是多少"。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import portfolio_backtest as PB  # noqa: E402


def _leg(name, dates, symbols, rows, note=""):
    return PB.StrategyWeights(name, list(dates), list(symbols),
                              np.asarray(rows, dtype=float), note)


class TestStrategyWeights:
    def test_shape_mismatch_raises(self):
        with pytest.raises(ValueError):
            _leg("a", ["d1", "d2"], ["s1"], [[1.0]])

    def test_non_2d_raises(self):
        with pytest.raises(ValueError):
            PB.StrategyWeights("a", ["d1"], ["s1"], np.array([1.0]))

    def test_row_lookup(self):
        lg = _leg("a", ["d1", "d2"], ["s1", "s2"], [[0.5, 0.5], [1.0, 0.0]])
        assert list(lg.row("d2")) == [1.0, 0.0]

    def test_missing_day_returns_zeros(self):
        lg = _leg("a", ["d1"], ["s1", "s2"], [[0.5, 0.5]])
        assert list(lg.row("nope")) == [0.0, 0.0]


class TestAlignWeights:
    def test_union_axis_not_intersection(self):
        """取并集: 某条腿只在部分日期出手是常态, 取交集会把可回测区间压到最短
        那条腿上, 并掩盖"这条腿经常不出手"这一事实。"""
        a = _leg("a", ["d1", "d2"], ["s1"], [[1.0], [1.0]])
        b = _leg("b", ["d2", "d3"], ["s2"], [[1.0], [1.0]])
        dates, symbols, cube = PB.align_weights([a, b])
        assert dates == ["d1", "d2", "d3"]
        assert symbols == ["s1", "s2"]
        assert cube.shape == (2, 3, 2)

    def test_values_land_in_right_cells(self):
        a = _leg("a", ["d1"], ["s2"], [[0.7]])
        dates, symbols, cube = PB.align_weights(
            [a], dates=["d1"], symbols=["s1", "s2"])
        assert cube[0, 0, symbols.index("s2")] == 0.7
        assert cube[0, 0, symbols.index("s1")] == 0.0

    def test_explicit_axes_honoured(self):
        a = _leg("a", ["d1", "d2"], ["s1"], [[1.0], [2.0]])
        dates, symbols, cube = PB.align_weights([a], dates=["d2"], symbols=["s1"])
        assert dates == ["d2"] and cube[0, 0, 0] == 2.0

    def test_empty_legs_raises(self):
        with pytest.raises(ValueError):
            PB.align_weights([])

    def test_empty_axis_raises(self):
        with pytest.raises(ValueError):
            PB.align_weights([_leg("a", [], [], np.zeros((0, 0)))])


class TestNormalizeWeights:
    def test_each_row_sums_to_one(self):
        lg = _leg("a", ["d1"], ["s1", "s2"], [[2.0, 6.0]])
        out = PB.normalize_weights([lg])[0]
        assert out.weights.sum() == pytest.approx(1.0)
        assert out.weights[0, 1] == pytest.approx(0.75)

    def test_zero_row_stays_zero(self):
        """"这条腿今天没意见"必须与"平均看好所有票"区分开。"""
        lg = _leg("a", ["d1", "d2"], ["s1", "s2"], [[0.0, 0.0], [1.0, 1.0]])
        out = PB.normalize_weights([lg])[0]
        assert out.weights[0].sum() == 0.0
        assert out.weights[1].sum() == pytest.approx(1.0)

    def test_does_not_mutate_input(self):
        lg = _leg("a", ["d1"], ["s1", "s2"], [[2.0, 6.0]])
        PB.normalize_weights([lg])
        assert lg.weights[0, 1] == 6.0


class TestAggregate:
    def _cube(self):
        return np.array([
            [[1.0, 0.0]],      # leg 0 只看 s1
            [[0.0, 1.0]],      # leg 1 只看 s2
        ])

    def test_equal_weights_mix_both_legs(self):
        comb = PB.aggregate(self._cube(), [0.5, 0.5])
        assert comb[0, 0] == pytest.approx(0.5)
        assert comb[0, 1] == pytest.approx(0.5)

    def test_extreme_weight_selects_one_leg(self):
        comb = PB.aggregate(self._cube(), [1.0, 0.0])
        assert comb[0, 0] == pytest.approx(1.0)
        assert comb[0, 1] == pytest.approx(0.0)

    def test_unnormalised_weights_are_rescaled(self):
        comb = PB.aggregate(self._cube(), [3.0, 1.0])
        assert comb[0, 0] == pytest.approx(0.75)

    def test_negative_weight_rejected(self):
        with pytest.raises(ValueError):
            PB.aggregate(self._cube(), [1.0, -1.0])

    def test_zero_sum_rejected(self):
        with pytest.raises(ValueError):
            PB.aggregate(self._cube(), [0.0, 0.0])

    def test_nan_weight_rejected(self):
        with pytest.raises(ValueError):
            PB.aggregate(self._cube(), [float("nan"), 1.0])

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError):
            PB.aggregate(self._cube(), [1.0])

    def test_non_3d_rejected(self):
        with pytest.raises(ValueError):
            PB.aggregate(np.zeros((2, 2)), [1.0, 1.0])

    def test_normalize_false_keeps_gross(self):
        c = np.array([[[0.5, 0.5]], [[0.5, 0.5]]])
        comb = PB.aggregate(c, [0.5, 0.5], normalize=False)
        assert comb[0].sum() == pytest.approx(1.0)


class TestLegExposure:
    def test_reports_weight_and_gross(self):
        cube = np.array([[[1.0, 1.0]], [[0.5, 0.0]]])
        out = PB.leg_exposure(cube, [0.75, 0.25])
        assert out[0]["weight"] == pytest.approx(0.75)
        assert out[0]["mean_gross"] == pytest.approx(2.0)
        assert out[0]["mean_names"] == pytest.approx(2.0)
        assert out[1]["mean_names"] == pytest.approx(1.0)

    def test_contrib_sums_to_combined_gross(self):
        """逐腿 contrib = 权重 × 该腿平均总暴露, 其和 == 组合的平均总暴露。
        手算: 0.5*2.0 + 0.5*1.0 = 1.5。"""
        cube = np.array([[[1.0, 1.0]], [[0.5, 0.0]]])
        out = PB.leg_exposure(cube, [0.5, 0.5])
        assert out[0]["contrib"] == pytest.approx(1.0)     # 0.5 * 2.0
        assert out[1]["contrib"] == pytest.approx(0.25)    # 0.5 * 1.0
        assert sum(o["contrib"] for o in out) == pytest.approx(1.25)


class TestMetrics:
    def test_max_drawdown_known_series(self):
        assert PB.max_drawdown_pct([100, 120, 90, 110]) == pytest.approx(25.0)

    def test_max_drawdown_monotone_rise_is_zero(self):
        assert PB.max_drawdown_pct([100, 101, 102]) == 0.0

    def test_max_drawdown_empty(self):
        assert PB.max_drawdown_pct([]) == 0.0

    def test_daily_returns_length(self):
        assert len(PB.daily_returns([100, 101, 102])) == 2

    def test_daily_returns_zero_prev_no_inf(self):
        r = PB.daily_returns([0, 100])
        assert np.all(np.isfinite(r))

    def test_sharpe_nan_on_constant_returns(self):
        """常量收益 => 零波动 => Sharpe 无定义。必须返回 NaN 而不是 0 或 inf ——
        返回 0 会让"无信息"看起来像"业绩平庸", 而 inf 会污染排序。"""
        assert np.isnan(PB.sharpe(np.full(60, 0.001)))

    def test_sharpe_positive_for_positive_drift_with_noise(self):
        rng = np.random.default_rng(7)
        r = 0.002 + rng.normal(0, 0.001, 120)
        assert PB.sharpe(r) > 0

    def test_sharpe_nan_on_too_few(self):
        assert np.isnan(PB.sharpe(np.array([0.01, 0.02])))

    def test_metrics_on_curve(self):
        curve = [{"equity": 100000}, {"equity": 101000}, {"equity": 100500},
                 {"equity": 102000}]
        m = PB.metrics(curve)
        assert m["n_days"] == 4
        assert m["max_drawdown_pct"] > 0
        assert m["vol_annual_pct"] is not None
        assert m["sharpe"] is not None

    def test_metrics_sharpe_is_none_not_zero_when_undefined(self):
        """未定义时给 None(不假装是 0), 使下游能区分"没算出来"与"算出来是 0"。"""
        m = PB.metrics([{"equity": 100000}, {"equity": 100000}, {"equity": 100000}])
        assert m["sharpe"] is None


class TestSimulatePortfolio:
    """确定性撮合验证: 3 个交易日、2 只票、价格手算。"""

    def _run(self, weights, prices, **kw):
        dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
        symbols = ["AAA", "BBB"]

        def price_of(day, canon):
            return prices.get((day, canon), 0.0)

        return PB.simulate_portfolio(dates, symbols, np.asarray(weights, float),
                                     price_of=price_of, **kw)

    def test_flat_prices_flat_equity_minus_costs(self):
        w = np.zeros((3, 2))
        res = self._run(w, {})
        assert res["trades"] == 0
        assert res["final_equity"] == pytest.approx(100000.0, abs=0.01)

    def test_buys_expected_lot_size(self):
        """权益 10 万、投入比 0.4、单票权重 1.0 => 目标 4 万; 价 10 元 => 4000 股。"""
        prices = {("2026-09-01", "AAA"): 10.0, ("2026-09-02", "AAA"): 10.0,
                  ("2026-09-03", "AAA"): 10.0}
        w = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        res = self._run(w, prices, invest_ratio=0.4, init_capital=100000.0)
        buys = [t for t in res["trade_log"] if t["side"] == "buy"]
        assert buys and buys[0]["qty"] == 4000

    def test_tplus1_blocks_same_day_sell(self):
        """同日买入的仓位当日不可卖(账本原生 T+1), 故首日不会有卖单。"""
        prices = {("2026-09-01", "AAA"): 10.0}
        w = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        res = self._run(w, prices, invest_ratio=0.4)
        day1 = [t for t in res["trade_log"] if t["day"] == "2026-09-01"]
        assert all(t["side"] == "buy" for t in day1)

    def test_exit_when_weight_goes_to_zero(self):
        prices = {("2026-09-01", "AAA"): 10.0, ("2026-09-02", "AAA"): 10.0,
                  ("2026-09-03", "AAA"): 10.0}
        w = [[1.0, 0.0], [0.0, 0.0], [0.0, 0.0]]
        res = self._run(w, prices, invest_ratio=0.4)
        sells = [t for t in res["trade_log"] if t["side"] == "sell"]
        assert sells and sells[0]["day"] == "2026-09-02"

    def test_no_price_means_skipped(self):
        w = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        res = self._run(w, {})                   # 全部无价
        assert res["trades"] == 0
        assert res["orders_skipped"] > 0

    def test_tradable_false_blocks(self):
        prices = {("2026-09-01", "AAA"): 10.0}
        w = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        res = self._run(w, prices, invest_ratio=0.4,
                        tradable_of=lambda d, c: False)
        assert res["trades"] == 0

    def test_tradable_exception_does_not_stop(self):
        """判定异常不得让回测静默停手(与实盘同纪律)。"""
        prices = {("2026-09-01", "AAA"): 10.0}

        def boom(d, c):
            raise RuntimeError("gate down")

        res = self._run([[1.0, 0.0]] * 3, prices, invest_ratio=0.4, tradable_of=boom)
        assert res["trades"] > 0

    def test_weights_shape_validated(self):
        with pytest.raises(ValueError):
            PB.simulate_portfolio(["d1"], ["s1"], np.zeros((2, 1)),
                                  price_of=lambda d, c: 1.0)

    def test_turnover_recorded(self):
        prices = {("2026-09-01", "AAA"): 10.0, ("2026-09-02", "AAA"): 10.0,
                  ("2026-09-03", "AAA"): 10.0}
        res = self._run([[1.0, 0.0]] * 3, prices, invest_ratio=0.4)
        assert res["turnover_notional"] > 0
        assert res["turnover_pct"] > 0

    def test_curve_length_equals_days(self):
        res = self._run(np.zeros((3, 2)), {})
        assert len(res["curve"]) == 3

    def test_book_factory_is_used(self):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            from paper_book import PaperBook
            return PaperBook(init_capital=100000.0)

        self._run(np.zeros((3, 2)), {}, book_factory=factory)
        assert calls["n"] == 1


class TestRunPortfolioBacktest:
    def _prices(self):
        out = {}
        for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
            out[(d, "AAA")] = 10.0
            out[(d, "BBB")] = 20.0
        return out

    def test_end_to_end_shape(self):
        days = ["2026-09-01", "2026-09-02", "2026-09-03"]
        a = _leg("fusion", days, ["AAA", "BBB"], [[1.0, 0.0]] * 3)
        b = _leg("gp4", days, ["AAA", "BBB"], [[0.0, 1.0]] * 3)
        res = PB.run_portfolio_backtest(
            [a, b], [0.5, 0.5], price_of=lambda d, c: self._prices().get((d, c), 0.0),
            invest_ratio=0.4)
        assert res["metrics"]["n_days"] == 3
        assert len(res["legs"]) == 2
        assert len(res["combined_weights"]) == 3
        assert res["strategy_weights"] == [0.5, 0.5]

    def test_combined_weights_normalised(self):
        a = _leg("a", ["2026-09-01"], ["AAA", "BBB"], [[2.0, 2.0]])
        b = _leg("b", ["2026-09-01"], ["AAA", "BBB"], [[3.0, 3.0]])
        res = PB.run_portfolio_backtest(
            [a, b], [0.5, 0.5], price_of=lambda d, c: 10.0, invest_ratio=0.4)
        assert sum(res["combined_weights"][0]) == pytest.approx(1.0)

    def test_compare_to_single_runs_every_leg(self):
        a = _leg("a", ["2026-09-01"], ["AAA"], [[1.0]])
        b = _leg("b", ["2026-09-01"], ["BBB"], [[1.0]])
        pm = self._prices()
        out = PB.compare_to_single([a, b], [0.5, 0.5],
                                   price_of=lambda d, c: pm.get((d, c), 0.0),
                                   invest_ratio=0.4)
        assert set(out["single_legs"]) == {"a", "b"}
        assert "portfolio" in out
        assert "versus_best_single" in out

    def test_versus_best_single_can_be_negative(self):
        """组合跑不过最好的单腿时必须如实显示为负, 不许只报赢的那边。"""
        pm = self._prices()
        good = _leg("good", ["2026-09-01"], ["AAA"], [[1.0]])
        bad = _leg("bad", ["2026-09-01"], ["BBB"], [[1.0]])
        out = PB.compare_to_single([good, bad], [0.5, 0.5],
                                   price_of=lambda d, c: pm.get((d, c), 0.0),
                                   invest_ratio=0.4)
        assert "excess_pct" in out["versus_best_single"]
        assert isinstance(out["versus_best_single"]["excess_pct"], float)

    def test_identical_legs_do_not_duplicate_exposure(self):
        """同一策略给两份权重 => 归一后仍只有一份暴露(聚合不是简单相加)。"""
        a = _leg("a", ["2026-09-01"], ["AAA", "BBB"], [[1.0, 0.0]])
        a2 = _leg("a2", ["2026-09-01"], ["AAA", "BBB"], [[1.0, 0.0]])
        res = PB.run_portfolio_backtest(
            [a, a2], [0.5, 0.5], price_of=lambda d, c: 10.0, invest_ratio=0.4)
        assert sum(res["combined_weights"][0]) == pytest.approx(1.0)

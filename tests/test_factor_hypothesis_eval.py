# -*- coding: utf-8 -*-
"""factor_hypothesis_eval (⑭ 的收益评估接线) 回归测试。

本文件锁住两件在接线时**真实踩过**的事:

1. **指标读取路径**: `factor_mine.evaluator.evaluate` 把 ic_mean/icir/n_days 放在
   报告**根层**, 不是嵌在 `report['ic']` 里。首次接线按嵌套读, 三个门槛全部读到
   None, 于是所有假设被静默判否 —— 看起来像"没有因子有效"。
2. **截面取数必须一批完成**: 逐标的调 `close_prices_for` 会退化成上万次查询并
   超时(第一次冒烟测试就是这么挂的)。`_adj_within` 的口径必须与逐标的复权**一致**。

故这里既测"门槛逻辑", 也测"复权口径与 backtest_engine 等价"。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import factor_hypothesis_eval as FE  # noqa: E402


def _frame(n_days=80, n_syms=60, ic=0.0, seed=3):
    """造一份带已知 IC 的截面: factor 与 fwd5 的秩相关由 ic 控制。"""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_days):
        d = f"2026-{3 + i // 28:02d}-{i % 28 + 1:02d}"
        f = rng.normal(size=n_syms)
        noise = rng.normal(size=n_syms)
        y = ic * f + np.sqrt(max(1 - ic * ic, 1e-9)) * noise
        for j in range(n_syms):
            rows.append({"date": d, "symbol": f"{j:06d}", "factor": f[j], "fwd5": y[j]})
    return pd.DataFrame(rows)


class TestReturnEvaluatorContract:
    def test_thresholds_are_mandatory(self):
        """三个门槛必须显式给 —— 不给就 TypeError(不提供隐式默认值)。"""
        with pytest.raises(TypeError):
            FE.make_return_evaluator(min_ic=0.02, min_icir=0.3)      # 缺 min_obs_days
        with pytest.raises(TypeError):
            FE.make_return_evaluator()

    def test_flat_ic_passes_all_thresholds(self):
        h = _mk("factor")
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=-99.0, min_obs_days=10)(h, _frame(ic=0.0))
        assert r["ok"] is True, r["reasons"]
        assert r["n_days"] and r["n_days"] >= 10

    def test_metrics_read_from_report_root_not_nested(self):
        """**回归锁**: 三个指标必须真的读到数(不是 None)。
        首次接线按 report['ic'] 嵌套读 -> 全 None -> 所有假设被静默判否。"""
        h = _mk("factor")
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=-99.0, min_obs_days=1)(h, _frame(ic=0.05))
        assert r["ic_mean"] is not None and np.isfinite(r["ic_mean"])
        assert r["icir"] is not None and np.isfinite(r["icir"])
        assert r["n_days"] is not None and r["n_days"] > 0
        # 整份报告留下备审计
        assert isinstance(r["metric"], dict) and "ic_mean" in r["metric"]

    def test_strong_ic_passes_weak_ic_fails(self):
        h = _mk("factor")
        strong = FE.make_return_evaluator(min_ic=0.03, min_icir=0.1, min_obs_days=10)(h, _frame(ic=0.25))
        weak = FE.make_return_evaluator(min_ic=0.10, min_icir=0.1, min_obs_days=10)(h, _frame(ic=0.01))
        assert strong["ok"] is True, strong["reasons"]
        assert weak["ok"] is False

    def test_short_sample_fails_n_days_gate(self):
        h = _mk("factor")
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=-99.0, min_obs_days=10_000)(h, _frame())
        assert r["ok"] is False and any("有效天数" in x for x in r["reasons"])

    def test_missing_fwd_column_is_reported(self):
        h = _mk("factor")
        df = _frame().drop(columns=["fwd5"])
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=0.0, min_obs_days=1)(h, df)
        assert r["ok"] is False and any("fwd5" in x for x in r["reasons"])

    def test_empty_frame_is_reported_not_crashed(self):
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=0.0, min_obs_days=1)(_mk("factor"), pd.DataFrame())
        assert r["ok"] is False and "无数据帧" in r["reasons"][0]

    def test_bad_expression_reported(self):
        r = FE.make_return_evaluator(min_ic=0.0, min_icir=0.0, min_obs_days=1)(
            _mk("no_such_field"), _frame())
        assert r["ok"] is False and any("求值" in x or "字段" in x for x in r["reasons"])

    def test_none_frame(self):
        assert FE.make_return_evaluator(min_ic=0, min_icir=0, min_obs_days=1)(_mk("factor"), None)["ok"] is False

    def test_spread_gate_optional(self):
        h = _mk("factor")
        strict = FE.make_return_evaluator(min_ic=0.0, min_icir=-99.0, min_obs_days=1,
                                         min_abs_spread_pct=1e9)(h, _frame(ic=0.05))
        assert strict["ok"] is False and any("价差" in x for x in strict["reasons"])

    def test_thresholds_echoed_for_audit(self):
        r = FE.make_return_evaluator(min_ic=0.02, min_icir=0.3, min_obs_days=60)(_mk("factor"), _frame())
        assert r["thresholds"] == {"min_ic": 0.02, "min_icir": 0.3,
                                   "min_obs_days": 60, "min_abs_spread_pct": 0.0}

    def test_evaluator_name_recorded(self):
        r = FE.make_return_evaluator(min_ic=0, min_icir=0, min_obs_days=1)(_mk("factor"), _frame())
        assert r["evaluator"] == "factor_mine.evaluator"


def _mk(expr: str):
    import factor_hypothesis as FH
    return FH.Hypothesis(thesis="测试假设命题足够长", mechanism="机制说明需要足够长以满足 lint 下限要求",
                         direction=1, expression=expr, required_fields=[expr], source="template")


class TestChangePctScale:
    """口径判别必须看**尾部量级**(受涨跌停约束的物理量), 不能看中位数。

    首次实现用"|chg| 中位数 > 1 => 百分数", 在 `change_pct` 恰好集中在 1.0
    (即 1%)时判反, 复权序列被放大 100 倍 —— 实测 fwd5 算出 +3100%。
    """

    def test_one_percent_is_percent_scale(self):
        """边界用例: 全为 1.0 时, 1.0 必须解读为 +1%, 不是 +0.1%。"""
        s = pd.Series([1.0] * 100)
        r = FE._change_pct_to_ratio(s)
        assert r.iloc[0] == pytest.approx(0.01)

    def test_ten_percent_limit_is_percent_scale(self):
        s = pd.Series([10.0, -10.0, 0.0] * 50)
        r = FE._change_pct_to_ratio(s)
        assert r.max() == pytest.approx(0.10) and r.min() == pytest.approx(-0.10)

    def test_decimal_scale_detected(self):
        s = pd.Series([0.01, -0.02, 0.0] * 50)
        r = FE._change_pct_to_ratio(s)
        assert r.iloc[0] == pytest.approx(0.01)      # 原样, 不再除 100

    def test_decimal_scale_tail_is_below_half(self):
        """小数口径下即便有 30%(北交所)的极端值, p99.9 也只有 0.30 < 0.5。"""
        s = pd.Series([0.001] * 400 + [0.30, -0.30])
        r = FE._change_pct_to_ratio(s)
        assert r.abs().max() == pytest.approx(0.30)

    def test_empty_and_all_nan(self):
        assert FE._change_pct_to_ratio(pd.Series([], dtype=float)).empty
        assert FE._change_pct_to_ratio(pd.Series([np.nan, np.nan])).isna().all()

    def test_non_numeric_becomes_nan(self):
        r = FE._change_pct_to_ratio(pd.Series(["abc", 1.0, None]))
        assert np.isnan(r.iloc[0]) and r.iloc[1] == pytest.approx(0.01)


class TestAdjWithin:
    """复权口径必须与逐标的复权**比值等价**(否则前向收益会含除权假跌)。"""

    def _bars(self, closes, changes):
        return pd.DataFrame({
            "d": [f"2026-09-{i + 1:02d}" for i in range(len(closes))],
            "symbol": ["600000"] * len(closes),
            "close": closes, "change_pct": changes,
        })

    def test_ratios_match_change_pct_compounding(self):
        closes = [100.0, 110.0, 99.0]
        changes = [0.0, 10.0, -10.0]          # 百分数口径
        b = FE._adj_within(self._bars(closes, changes))
        adj = b["adj"].values
        assert adj[1] / adj[0] == pytest.approx(1.10, rel=1e-9)
        assert adj[2] / adj[1] == pytest.approx(0.90, rel=1e-9)

    def test_decimal_change_pct_detected(self):
        """change_pct 也允许是小数口径(0.10 = +10%) —— 用分布中位数判断, 不写死阈值。"""
        closes = [100.0, 110.0]
        changes = [0.0, 0.10]
        b = FE._adj_within(self._bars(closes, changes))
        assert b["adj"].values[1] / b["adj"].values[0] == pytest.approx(1.10, rel=1e-9)

    def test_dividend_day_does_not_create_fake_return(self):
        """除权日: close 从 100 跳到 50, 但 change_pct 记的是真实涨跌 0% =>
        复权口径下收益必须是 0, 不是 -50%。这正是不能用原始 close 的原因。"""
        closes = [100.0, 50.0]
        changes = [0.0, 0.0]
        b = FE._adj_within(self._bars(closes, changes))
        assert b["adj"].values[1] / b["adj"].values[0] == pytest.approx(1.0, rel=1e-9)

    def test_missing_change_pct_falls_back_to_close(self):
        bars = pd.DataFrame({"d": ["2026-09-01", "2026-09-02"], "symbol": ["600000"] * 2,
                             "close": [10.0, 11.0]})
        b = FE._adj_within(bars)
        assert b["adj"].values[1] == pytest.approx(11.0)

    def test_empty_input(self):
        assert FE._adj_within(pd.DataFrame()) is not None
        assert FE._adj_within(None) is None

    def test_multiple_symbols_are_independent(self):
        bars = pd.DataFrame({
            "d": ["2026-09-01", "2026-09-02"] * 2,
            "symbol": ["A"] * 2 + ["B"] * 2,
            "close": [10.0, 11.0, 20.0, 18.0],
            "change_pct": [0.0, 10.0, 0.0, -10.0],
        })
        b = FE._adj_within(bars)
        for sym, expect in (("A", 1.10), ("B", 0.90)):
            g = b[b["symbol"] == sym]["adj"].values
            assert g[1] / g[0] == pytest.approx(expect, rel=1e-9), sym


class TestFwdFromAdj:
    def test_takes_the_hold_th_bar_not_natural_days(self):
        """构造 change_pct 明确为"后 5 根各 +10%", 则第 5 根 bar 的前向收益
        必须**恰好**是 1.1^5 - 1 —— 用解析已知答案, 避免手算复权序列出错。"""
        n = 6
        bars = pd.DataFrame({
            "d": [f"2026-09-{i + 1:02d}" for i in range(n)],
            "symbol": ["600000"] * n,
            "close": [10.0] * n,                 # close 本身不动(只看 change_pct 口径)
            "change_pct": [0.0] + [10.0] * (n - 1),
        })
        b = FE._adj_within(bars)
        f = FE._fwd_from_adj(b, [b["d"].iloc[0]], 5, ["600000"])
        assert len(f) == 1
        assert f["fwd5"].iloc[0] == pytest.approx(1.1 ** 5 - 1.0, rel=1e-9)

    def test_is_the_fifth_bar_not_five_calendar_days(self):
        """跳的是**第 5 根 bar**, 不是日历 5 天 —— 用不连续日期验证:
        1 根 bar 一周的序列里, fwd5 必须看第 5 个交易日。"""
        bars = pd.DataFrame({
            "d": ["2026-09-01", "2026-09-08", "2026-09-15", "2026-09-22",
                  "2026-09-29", "2026-10-06"],
            "symbol": ["600000"] * 6,
            "close": [10.0] * 6,
            "change_pct": [0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        })
        b = FE._adj_within(bars)
        f = FE._fwd_from_adj(b, ["2026-09-01"], 5, ["600000"])
        assert f["fwd5"].iloc[0] == pytest.approx(1.01 ** 5 - 1.0, rel=1e-9)

    def test_insufficient_tail_is_dropped(self):
        """末尾不足 hold 根 bar 的日子必须**丢弃**, 不填 NaN 也不假装算得出。"""
        n = 4
        bars = pd.DataFrame({
            "d": [f"2026-09-{i + 1:02d}" for i in range(n)],
            "symbol": ["600000"] * n,
            "close": [10.0] * n, "change_pct": [0.0] * n,
        })
        b = FE._adj_within(bars)
        assert FE._fwd_from_adj(b, [b["d"].iloc[0]], 5, ["600000"]).empty


class TestConfigThresholds:
    def test_thresholds_from_config_present(self):
        th = FE.thresholds_from_config()
        assert {"min_ic", "min_icir", "min_obs_days"} <= set(th)

    def test_missing_config_key_raises(self, monkeypatch):
        """缺配置必须**响亮失败**, 不用一个写死的默认值糊过去。"""
        import config
        monkeypatch.setattr(config, "FACTOR_HYPOTHESIS_EVAL", {"min_ic": 0.02}, raising=False)
        with pytest.raises(KeyError):
            FE.thresholds_from_config()


class TestWindowConsistency:
    def test_hypothesis_days_covers_min_obs_days(self):
        """**回归锁**: 日更回看的交易日数必须 >= 收益门槛要求的最少天数,
        否则每一项都必然因"有效天数不足"而 fail, 看起来像"没有因子有效"。"""
        from config import PAPER, FACTOR_HYPOTHESIS_EVAL
        assert int(PAPER.get("hypothesis_days", 0) or 0) >= int(FACTOR_HYPOTHESIS_EVAL["min_obs_days"]), \
            "PAPER.hypothesis_days 小于 FACTOR_HYPOTHESIS_EVAL.min_obs_days: 该步骤会全数误判"


class TestViewsPath:
    def test_returns_a_string(self):
        assert isinstance(FE.views_path(), str)

    def test_bar_fields_and_view_factors_declared(self):
        assert "f_signal" in FE.VIEW_FACTORS and "close" in FE.BAR_FIELDS

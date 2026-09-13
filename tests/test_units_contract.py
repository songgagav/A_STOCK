# -*- coding: utf-8 -*-
"""item 10 回归: 收益/回撤单位约定 (百分点 vs 比例).

根因: 已落盘的百分数字段被下游再乘 100, 使 -0.694% 显示成 -69.4%、-1.13% 显示成 -113%。
约定见 docs/units.md。
"""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

import pytest  # noqa: E402

DOC = os.path.join(_BASE, "docs", "units.md")


def test_max_dd_returns_percent_points():
    """BacktestRunner._max_dd 返回**百分点幅度**(正值), 如 10.0 表示回撤 10%.

    注意符号: 本函数返回**幅度**(正值), 与 vnpy 原生 `max_ddpercent`(负值) 不同,
    详见 docs/units.md「符号约定」。消费端不得再做 `-abs()` 之类取反。
    """
    from backtest_engine import BacktestRunner
    curve = [{"equity": 100000.0}, {"equity": 110000.0}, {"equity": 99000.0}]
    dd = BacktestRunner._max_dd(curve)
    assert dd == pytest.approx(10.0, abs=0.05), f"_max_dd 应为百分点幅度(正值), 实得 {dd}"


def test_max_dd_no_drawdown_is_zero():
    from backtest_engine import BacktestRunner
    curve = [{"equity": 100000.0}, {"equity": 101000.0}, {"equity": 102000.0}]
    assert BacktestRunner._max_dd(curve) == pytest.approx(0.0, abs=1e-9)


def test_curve_metrics_max_drawdown_is_ratio():
    """strategy_validation 的 curve_metrics 用**比例**(0..1), 阈值 0.30 即 30%."""
    import strategy_validation as sv
    thr = getattr(sv, "THRESHOLDS", {}).get("max_drawdown")
    if thr is None:
        pytest.skip("THRESHOLDS['max_drawdown'] 不存在")
    lim = thr.get("limit") if isinstance(thr, dict) else getattr(thr, "limit", None)
    assert lim == pytest.approx(0.30), (
        "curve_metrics 口径是比例, 阈值应为 0.30(=30%); 若改成百分点需同步消费端")


def test_units_doc_exists_and_pins_key_rows():
    assert os.path.exists(DOC), "docs/units.md 为单位口径唯一依据, 不得删除"
    txt = open(DOC, encoding="utf-8").read()
    for key in ("max_drawdown_pct", "total_return_pct", "比例(0..1)", "百分点"):
        assert key in txt, f"units.md 缺少关键条目: {key}"


def test_backtest_engine_result_has_pct_keys():
    """BacktestRunner 汇总须同时提供 _pct 规范键与兼容键(同值)."""
    src = open(os.path.join(_BASE, "src", "backtest_engine.py"), encoding="utf-8").read()
    assert '"total_return_pct"' in src, "缺少规范键 total_return_pct"
    assert '"max_drawdown_pct"' in src
    assert "_total_return_pct = round(" in src, "换算应在来源处只做一次"
    assert '"total_return": _total_return_pct' in src, (
        "旧键 total_return 须复用同一变量, 避免两处各自换算产生偏差")


def test_drawdown_sign_convention_documented():
    """回撤的**符号**跨模块不一致(引擎=正幅度, vnpy=负值), 必须显式记录."""
    txt = open(DOC, encoding="utf-8").read()
    assert "符号约定" in txt, "units.md 缺少符号约定小节"
    assert "max_ddpercent" in txt, "未记录 vnpy 原生 max_ddpercent 的负值口径"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

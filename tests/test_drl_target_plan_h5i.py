# -*- coding: utf-8 -*-
"""`_load_plan_frame`（target_plan 的 h5i 取数）回归 —— P0-DRLSRC 步骤②，2026-09-20.

★ 依赖 torch（经 `drl_train`）⇒ 只在 CI 的 **regression-drl** job 跑，
  并在 **regression-core** job 里被 --ignore。

原实现：`_build_target_plan()` 以 `os.path.exists(DUCKDB_PATH)` 前置判断，DuckDB 退役后
直接返回『DuckDB 不存在』⇒ 自 2026-09-05 起**不再产出 target_plan.json**。

本文件用**假 parquet + 假 symbols + 假 store** 验证迁移后的口径（逐项对齐 legacy SQL），
不依赖 h5i_db 是否安装:
  · 主路 = `h5i/views/v_factor_scores_daily.parquet`，取 `date == max(date)`
    （**等价于** legacy 的 `WHERE v.date = (SELECT MAX(date) ...)`——该历史语义原样保留）
  · `LEFT JOIN daily_bars` -> 某标的当日无 bar 时价格列应为 NaN（不是丢行、不是 0）
  · canon 补后缀: 由 `symbols.market` 决定 `.SH/.SZ/.BJ`；market 未知则不加后缀
  · `signal`: f_trend>0 且 f_signal>0 -> BUY；两者都 <0 -> SELL；否则 HOLD
  · 视图缺失 -> 走 `h5i_bars_fallback` 兜底并**如实上报** source（不再静默）
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import drl_train as T  # noqa: E402  (依赖 torch)

FCOLS = ["f_signal", "f_trend", "f_govern", "f_liquidity", "f_vol", "f_mom_rev"]


def _write_view(path, rows):
    """rows: list of (canon, date, 6 个因子值)"""
    df = pd.DataFrame(rows, columns=["canon", "date"] + FCOLS)
    df.to_parquet(path, index=False)
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 views parquet 指到 tmp、symbols 与 bars 用替身。"""
    view = tmp_path / "v_factor_scores_daily.parquet"
    monkeypatch.setattr(T, "_VIEW_SCORES_PARQUET", str(view))
    monkeypatch.setattr(T, "_H5I_VIEWS_DIR", str(tmp_path))

    class _Sym:
        frame = pd.DataFrame(
            {"symbol": ["600000", "000001", "830799"],
             "market": ["sh", "sz", "bj"],
             "is_active": [True, True, True]})

    import db
    monkeypatch.setattr(db, "_h5i_symbols_df", lambda: _Sym.frame.copy())

    class _Store:
        bars = pd.DataFrame()
        days = ["2026-09-05"]

        def __init__(self, *a, **k):
            pass

        def bars_on_day(self, day, decision_time=None):
            return type(self).bars.copy()

        def trading_days(self, decision_time=None):
            return list(type(self).days)

    import h5i_bar_store
    monkeypatch.setattr(h5i_bar_store, "H5iBarStore", _Store)
    return type("E", (), {"view": view, "sym": _Sym, "store": _Store})


def _bars(rows):
    return pd.DataFrame(rows, columns=["symbol", "close", "change_pct", "turnover", "amount"])


class TestAsOfSectionContract:
    """`as_of` 显式截面契约（2026-09-20 回补能力）.

    原实现隐式取 `MAX(date)` —— 今天 views 的 MAX=2026-09-07, 故对 0907 碰巧正确,
    对 0908 会取到 0907 的**陈旧截面**且毫无提示。回补需要显式契约, 且"该日没有截面"
    必须**响亮失败**, 不得静默退回 daily_bars 降级精简版（那会产出"看起来是那天、
    实际 f_govern/f_vol/f_mom_rev 被置 0"的信号）。
    """

    def test_default_still_uses_max_date(self, env):
        """向后兼容: 不传 as_of 时行为与迁移前一致（取 MAX(date)）。"""
        _write_view(env.view, [("600000", "2026-09-04", 1, 1, 0, 0, 0, 0),
                               ("600000", "2026-09-07", 2, 2, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, src = T._load_plan_frame()
        assert src == "h5i_view"
        assert set(df["date"]) == {"2026-09-07"}, "缺省应取最大日期"
        assert df["f_signal"].iloc[0] == 2

    def test_explicit_as_of_uses_that_date(self, env):
        """显式 as_of 时取**指定**那一天, 即使它不是最大日期。"""
        _write_view(env.view, [("600000", "2026-09-04", 1, 1, 0, 0, 0, 0),
                               ("600000", "2026-09-07", 2, 2, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, _src = T._load_plan_frame(as_of="2026-09-04")
        assert set(df["date"]) == {"2026-09-04"}
        assert df["f_signal"].iloc[0] == 1

    @pytest.mark.parametrize("as_of", ["2026-09-08", "20260908"])
    def test_missing_as_of_raises(self, env, as_of):
        """★ 该日截面不存在 -> 必须抛 SectionUnavailable（两种日期写法都要认）。"""
        _write_view(env.view, [("600000", "2026-09-07", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        with pytest.raises(T.SectionUnavailable) as ei:
            T._load_plan_frame(as_of=as_of)
        assert "2026-09-08" in str(ei.value) and "拒绝静默改用其它截面" in str(ei.value)

    def test_as_of_with_unreadable_views_raises_not_falls_back(self, env, monkeypatch):
        """★ 要求显式截面但 views 不可读 -> 也必须失败, **不得**退回降级兜底。

        实测教训: 沙箱化会让 `_VIEW_SCORES_PARQUET` 指向沙箱, 视图找不到就静默走兜底,
        连"截面缺失"这个拒绝条件都不会被触发。
        """
        monkeypatch.setattr(T, "_VIEW_SCORES_PARQUET",
                            str(env.view.parent / "does_not_exist.parquet"))
        env.store.days = ["2026-09-08"]
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        with pytest.raises(T.SectionUnavailable):
            T._load_plan_frame(as_of="2026-09-08")

    def test_build_target_plan_marks_backfill(self, env, monkeypatch, tmp_path):
        """`source=...` 时产物必须带 is_backfill / oos_eligible=false（用户要求）。"""
        monkeypatch.setattr(T, "DATA_DIR", str(tmp_path))
        _write_view(env.view, [("600000", "2026-09-07", 1.0, 1.0, 0.0, 0.0, 0.0, 0.0),
                               ("000001", "2026-09-07", 0.5, 0.5, 0.0, 0.0, 0.0, 0.0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0),
                                ("000001", 20.0, 0.0, 0.0, 0.0)])
        res = T._build_target_plan(day="2026-09-07", day_dir="20260907",
                                   final_weights={"signal": 0.5, "trend": 0.5},
                                   top_n=1, as_of="2026-09-07", source="backfill")
        assert res.get("ok") is True
        assert res.get("section_as_of") == "2026-09-07", \
            "产物必须留痕实际使用的截面日期"
        with open(os.path.join(str(tmp_path), "drl", "20260907", "target_plan.json"),
                  encoding="utf-8") as f:
            pl = json.load(f)
        assert pl["source"] == "backfill"
        assert pl["is_backfill"] is True
        assert pl["oos_eligible"] is False, "回补产物不得纳入 OOS 的 n 计数"
        assert pl["section_as_of"] == "2026-09-07"

    def test_normal_path_has_no_backfill_marks(self, env, monkeypatch, tmp_path):
        """正常（非回补）产物不得被标成 backfill —— 否则台账失去区分能力。"""
        monkeypatch.setattr(T, "DATA_DIR", str(tmp_path))
        _write_view(env.view, [("600000", "2026-09-07", 1.0, 1.0, 0.0, 0.0, 0.0, 0.0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        T._build_target_plan(day="2026-09-07", day_dir="20260907",
                             final_weights={"signal": 1.0}, top_n=1)
        with open(os.path.join(str(tmp_path), "drl", "20260907", "target_plan.json"),
                  encoding="utf-8") as f:
            pl = json.load(f)
        assert pl.get("is_backfill") is None
        assert pl.get("oos_eligible") is None
        assert pl.get("source") == "h5i_view"


class TestPlanFrameMainPath:
    def test_uses_max_date_row(self, env):
        """只保留 parquet 内 `date` 最大的那一批（历史语义原样保留）。"""
        _write_view(env.view, [
            ("600000", "2026-09-04", 1.0, 1.0, 0.0, 0.0, 0.0, 0.0),
            ("600000", "2026-09-05", 2.0, 2.0, 0.0, 0.0, 0.0, 0.0),
            ("000001", "2026-09-05", 3.0, 3.0, 0.0, 0.0, 0.0, 0.0),
        ])
        env.store.bars = _bars([("600000", 10.0, 1.0, 0.5, 1e6),
                                ("000001", 20.0, -1.0, 0.3, 2e6)])
        df, src = T._load_plan_frame()
        assert src == "h5i_view"
        assert len(df) == 2
        assert set(df["date"]) == {"2026-09-05"}
        assert df.loc[df["canon"] == "600000.SH", "f_signal"].iloc[0] == 2.0

    def test_canon_suffix_by_market(self, env):
        _write_view(env.view, [("600000", "2026-09-05", 1, 1, 0, 0, 0, 0),
                               ("000001", "2026-09-05", 1, 1, 0, 0, 0, 0),
                               ("830799", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0),
                               ("000001", 10.0, 0.0, 0.0, 0.0),
                               ("830799", 10.0, 0.0, 0.0, 0.0)])
        df, _ = T._load_plan_frame()
        assert set(df["canon"]) == {"600000.SH", "000001.SZ", "830799.BJ"}

    def test_unknown_market_keeps_bare_canon(self, env):
        _write_view(env.view, [("999999", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("999999", 10.0, 0.0, 0.0, 0.0)])
        df, _ = T._load_plan_frame()
        assert list(df["canon"]) == ["999999"]

    def test_left_join_missing_bar_yields_nan_not_dropped(self, env):
        """legacy 是 LEFT JOIN: 当日无 bar 的标的**仍在池里**, 价格列为 NULL。"""
        _write_view(env.view, [("600000", "2026-09-05", 1, 1, 0, 0, 0, 0),
                               ("000001", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 1.0, 0.5, 1e6)])   # 000001 无 bar
        df, _ = T._load_plan_frame()
        assert len(df) == 2, "无 bar 的标的不得被丢掉（LEFT JOIN 语义）"
        row = df[df["canon"] == "000001.SZ"].iloc[0]
        assert pd.isna(row["close"]) and pd.isna(row["turnover"])

    def test_empty_bars_still_returns_pool(self, env):
        """daily_bars 整日缺失时, 池子仍在（价格全 NaN）—— legacy 同样如此。"""
        _write_view(env.view, [("600000", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = pd.DataFrame(columns=["symbol", "close", "change_pct",
                                              "turnover", "amount"])
        df, src = T._load_plan_frame()
        assert src == "h5i_view" and len(df) == 1 and pd.isna(df["close"].iloc[0])

    @pytest.mark.parametrize("fs,ft,expect", [
        (1.0, 1.0, "BUY"), (-1.0, -1.0, "SELL"),
        (1.0, -1.0, "HOLD"), (0.0, 0.0, "HOLD"), (0.0, 1.0, "HOLD"),
    ])
    def test_signal_rule_matches_legacy_case(self, env, fs, ft, expect):
        _write_view(env.view, [("600000", "2026-09-05", fs, ft, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, _ = T._load_plan_frame()
        assert df["signal"].iloc[0] == expect

    def test_all_six_factor_columns_present(self, env):
        _write_view(env.view, [("600000", "2026-09-05", 1, 2, 3, 4, 5, 6)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, _ = T._load_plan_frame()
        for c in FCOLS:
            assert c in df.columns, f"缺因子列 {c}"
        assert df["f_mom_rev"].iloc[0] == 6


class TestPlanFrameFallback:
    def test_view_missing_uses_bars_fallback_and_reports_source(self, env):
        """视图 parquet 不存在 -> 走 daily_bars 精简版, 且 **source 如实上报**。"""
        assert not os.path.isfile(env.view)
        env.store.days = ["2026-09-04", "2026-09-05"]
        env.store.bars = _bars([("600000", 10.0, 5.0, 0.25, 1e6)])
        df, src = T._load_plan_frame()
        assert src == "h5i_bars_fallback"
        assert len(df) == 1
        assert df["f_signal"].iloc[0] == pytest.approx(0.5)      # change_pct/10
        assert df["f_govern"].iloc[0] == 0.0
        assert df["f_liquidity"].iloc[0] == pytest.approx(0.25)
        assert df["signal"].iloc[0] == "HOLD"
        assert df["canon"].iloc[0] == "600000.SH"

    def test_no_view_and_no_bars_returns_none(self, env):
        env.store.bars = pd.DataFrame(columns=["symbol", "close", "change_pct",
                                              "turnover", "amount"])
        df, src = T._load_plan_frame()
        assert src == "h5i_bars_fallback" and (df is None or len(df) == 0)


class TestPlanFrameRobustness:
    def test_symbols_unavailable_degrades_to_bare_canon(self, env, monkeypatch):
        """symbols 读不到不应致命 —— legacy 里 symbols JOIN 同样是可选的。"""
        import db

        def _boom():
            raise OSError("symbols.parquet 不可读")
        monkeypatch.setattr(db, "_h5i_symbols_df", _boom)
        _write_view(env.view, [("600000", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, src = T._load_plan_frame()
        assert src == "h5i_view"
        assert list(df["canon"]) == ["600000"], "拿不到 market 时应原样保留纯 6 位"

    def test_inactive_symbols_excluded(self, env, monkeypatch):
        import db
        frame = pd.DataFrame({"symbol": ["600000", "000001"],
                              "market": ["sh", "sz"],
                              "is_active": [True, False]})
        monkeypatch.setattr(db, "_h5i_symbols_df", lambda: frame.copy())
        _write_view(env.view, [("600000", "2026-09-05", 1, 1, 0, 0, 0, 0)])
        env.store.bars = _bars([("600000", 10.0, 0.0, 0.0, 0.0)])
        df, _ = T._load_plan_frame()
        assert list(df["canon"]) == ["600000.SH"]

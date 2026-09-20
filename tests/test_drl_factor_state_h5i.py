# -*- coding: utf-8 -*-
"""`_load_factor_state` 的 h5i 迁移回归（P0-DRLSRC，2026-09-20）.

★ 依赖 torch（经 `drl_train`）⇒ 只在 CI 的 **regression-drl** job 跑，
  并在 **regression-core** job 里被 --ignore（见 .github/workflows/ci.yml）。

被测: `drl_train._load_factor_state()` —— 原先连已退役删除的
`data/legacy_stockdb.duckdb`，实测抛 `IOException: database does not exist`，
且该调用在**外层 try 之外** ⇒ 异常逃出 `run_drl_train`，当天无 model.zip /
无 train_meta / 无告警（登记册 `P0-DRLSRC`）。

本文件用**假 store** 验证迁移后的计算口径，不依赖 h5i_db 是否安装
（真实口径一致性另由 `scripts/preflight_drl_h5i_parity.py` 在生产 h5i 上验证）:
  · 不再 import duckdb / 不碰 DUCKDB_PATH
  · 交易日集合 = 窗口内窗 `DISTINCT CAST(ts AS DATE)`
  · `rets[i]` = 相邻两日**都存在且 close>0** 的标的的 AVG(close_t/close_{t-1} - 1)
    —— inner-join 语义: 只在一天出现的标的**不参与**均值
  · `rets[0] = 0.0`；IC 矩阵形状 (n, 6) 且无 NaN/Inf
  · 天数 < 15 或空结果 -> (None, None, None)（调用方据此走降级链）
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import drl_train as T  # noqa: E402  (依赖 torch)


class _FakeStore:
    """替身: 记录调用参数并回放预置帧。"""

    calls: "list[tuple[str, str]]" = []
    frame = None

    def __init__(self, *a, **k):
        pass

    def closes_window(self, start, end, decision_time=None, positive_close_only=True):
        type(self).calls.append((start, end))
        return type(self).frame


@pytest.fixture
def fake_store(monkeypatch):
    import h5i_bar_store
    _FakeStore.calls = []
    _FakeStore.frame = None
    monkeypatch.setattr(h5i_bar_store, "H5iBarStore", _FakeStore)
    return _FakeStore


def _frame(rows):
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(rows, columns=["d", "symbol", "close", "change_pct"])


def _days(n, start=dt.date(2026, 6, 1)):
    """造 n 个连续"交易日"（跳过周末不重要 —— 帧里给什么就是什么）。"""
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _rows(days, closes_by_day):
    rows = []
    for d, cmap in zip(days, closes_by_day):
        for sym, c in cmap.items():
            rows.append((str(d), sym, float(c), 0.0))
    return rows


class TestH5iMigration:
    def test_does_not_touch_duckdb_path(self, fake_store, monkeypatch):
        """迁移的核心: 不得再连 legacy DuckDB。

        把 DUCKDB_PATH 指到一个**不存在**的路径; 若实现仍读它就会抛异常。
        """
        monkeypatch.setattr(T, "DUCKDB_PATH", os.path.join("Z:", "nope", "x.duckdb"))
        days = _days(20)
        closes = [{f"S{i:03d}": 10.0 + i for i in range(5)} for _ in days]
        fake_store.frame = _frame(_rows(days, closes))
        ic, rets, dates = T._load_factor_state(days[-1], 60)
        assert ic is not None and rets is not None

    def test_returns_none_below_min_days(self, fake_store):
        days = _days(10)
        closes = [{"S001": 10.0} for _ in days]
        fake_store.frame = _frame(_rows(days, closes))
        assert T._load_factor_state(days[-1], 60) == (None, None, None)

    def test_returns_none_on_empty_frame(self, fake_store):
        fake_store.frame = _frame([])
        assert T._load_factor_state(dt.date(2026, 9, 5), 60) == (None, None, None)

    def test_window_bounds_passed_to_store(self, fake_store):
        days = _days(20)
        fake_store.frame = _frame(_rows(days, [{"S001": 10.0 + i} for i in range(20)]))
        T._load_factor_state(dt.date(2026, 9, 5), 60)
        (start, end), = fake_store.calls
        assert end == "2026-09-05"
        assert start == str(dt.date(2026, 9, 5) - dt.timedelta(days=60))

    def test_rets_first_is_zero_and_shape(self, fake_store):
        days = _days(20)
        closes = [{"S001": 100.0 * (1.01 ** i), "S002": 50.0} for i in range(20)]
        fake_store.frame = _frame(_rows(days, closes))
        ic, rets, dates = T._load_factor_state(days[-1], 60)
        assert rets[0] == 0.0
        assert len(rets) == len(dates) == 20
        assert ic.shape == (20, 6)
        assert np.all(np.isfinite(ic)) and np.all(np.isfinite(rets))

    def test_rets_equals_inner_join_average(self, fake_store):
        """手工可验算的口径: 两天都存在的标的才进均值。"""
        days = _days(16)
        closes = []
        for i in range(16):
            cmap = {"A": 100.0, "B": 200.0}
            if i == 5:
                cmap["B"] = 220.0     # B: +10%
            if i == 6:
                cmap["A"] = 110.0     # A: +10%; 且 B 缺失 -> 只有 A 参与
            if i == 6:
                cmap.pop("B", None)
            closes.append(cmap)
        fake_store.frame = _frame(_rows(days, closes))
        _ic, rets, _dates = T._load_factor_state(days[-1], 60)
        # i=5: A 100->100 (0%), B 200->220 (+10%) => 均值 +5%
        assert rets[5] == pytest.approx(0.05, abs=1e-9)
        # i=6: 只有 A 两天都在 (100->110 = +10%); B 只在第 5 天有 => 不参与
        assert rets[6] == pytest.approx(0.10, abs=1e-9)

    def test_symbol_present_only_one_day_excluded(self, fake_store):
        """只出现一天的标的**不得**把均值拉偏（legacy inner-join 语义）。"""
        days = _days(16)
        closes = []
        for i in range(16):
            cmap = {"A": 100.0}
            if i == 8:
                cmap["NEW"] = 1e6      # 只在第 8 天出现, 若被计入会严重拉偏
            closes.append(cmap)
        fake_store.frame = _frame(_rows(days, closes))
        _ic, rets, _dates = T._load_factor_state(days[-1], 60)
        assert rets[8] == pytest.approx(0.0, abs=1e-9)

    def test_nan_rets_are_zeroed(self, fake_store):
        """close 全为 0 时不应产生 NaN/Inf（旧实现有过 NaN 污染观测的教训）。"""
        days = _days(16)
        pd = pytest.importorskip("pandas")
        rows = _rows(days, [{"A": 1.0} for _ in range(16)])
        rows.append((str(days[10]), "A", 0.0, 0.0))   # close=0 的行（真实 store 已过滤）
        fake_store.frame = pd.DataFrame(rows, columns=["d", "symbol", "close", "change_pct"])
        _ic, rets, _dates = T._load_factor_state(days[-1], 60)
        assert np.all(np.isfinite(rets))

    def test_scan_is_single_bulk_call(self, fake_store):
        """性能口径: 必须**一次**批量取窗, 而不是对每对相邻日各查一次（legacy 是 L 次自连接）。"""
        days = _days(20)
        fake_store.frame = _frame(_rows(days, [{"S001": 10.0 + i} for i in range(20)]))
        T._load_factor_state(days[-1], 60)
        assert len(fake_store.calls) == 1, f"期望 1 次查询, 实际 {len(fake_store.calls)}"

# -*- coding: utf-8 -*-
"""h5i 加速层的**语义等价**守卫 (2026-09-22 批次)。

## 背景: 一次真实的"选股跑了 25 分钟"

2026-09-22 补齐 h5i 的 09-21/09-22 后重跑 09-22 选股, 进程累积 CPU 达 3980s
(≈66 分钟当量) 而迟迟不结束。逐段计时后定位到两处, 都是"逐 symbol 扫全表":

| 位置 | 单只 | 全池(2809) | 改法 |
|---|---|---|---|
| `get_bars` 缓存命中后 `g[g["symbol"] == sym]` | — | 整窗 151 万行 × 2809 次 | 预热时按 symbol 建索引 |
| `get_valuation` / `_h5i_fin` 逐条 SQL | ~200ms | **~11 分钟** | 批量取全市场最新一期 |

`valuation` 有 **1540 万行**, 单次查询无论怎么写(双 CAST / 单 CAST / 不 CAST 实测
都是 ~200ms, 与排序、类型转换无关)都要扫过整表 —— 瓶颈是**扫表本身**。

**一个重要的弯路(留档)**: 我先试了"按 symbol 记忆化", 无效 ——
pool 里 2809 个 canon **互不相同**, 每个 symbol 只被问一次, 任何按 symbol 的缓存
都必然全部未命中。**该改的是查询形态(逐条 -> 批量), 不是加缓存。**

## 本文件锁什么

加速层最大的风险不是"不够快", 而是**顺手改了语义**。所以每条加速都必须有
"与非加速路径逐字段等价"的守卫。这里锁三件事:

1. **前视防护**: `prefetch_latest_snapshots()` 装的是"每个 symbol 最新一行"(今天口径),
   历史回测绝不能命中它 —— 否则是把今天的财报喂给过去的选股。
2. **字段契约**: 批量快照路径返回的字段**必须与逐条 SQL 路径完全一致**。
   (实现时就踩到: 我一度多给了 `liab_ratio`, 而逐条路径**刻意不注入**它 ——
   那会改变 `governance_score` 的输入, 属"加速顺手改语义"。)
3. **index 路径 == 布尔筛选路径**: `get_bars` 的符号索引与原来的
   `g[g["symbol"] == sym]` 必须逐行等价。
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

_HAS_H5I = importlib.util.find_spec("h5i_db") is not None
_needs_h5i = pytest.mark.skipif(
    not _HAS_H5I, reason="需要 h5i_db; .venv314 没装(生产解释器与 .venv310 有)")


def _same(a: dict, b: dict):
    """NaN-aware 字典比对。**必须**这么比: 含 NaN 的 dict 永不自等(`NaN != NaN`),
    直接 `==` 会把"完全一致"误报成差异(实测踩到, 白查了一轮)。"""
    if set(a) != set(b):
        return f"字段集不同: {sorted(a)} vs {sorted(b)}"
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, float) and isinstance(y, float) and math.isnan(x) and math.isnan(y):
            continue
        if x != y:
            return f"{k}: {x!r} vs {y!r}"
    return None


class TestNoLookaheadFreeze:
    """前视防护: 最新一期快照不得进入历史回测。"""

    def test_frozen_refuses_to_prefetch(self):
        import db as DB
        try:
            DB._LATEST_SNAP_FROZEN["on"] = True
            DB._LATEST_SNAP["valuation"] = {}
            DB._LATEST_SNAP["financials"] = {}
            assert DB.StockDB().prefetch_latest_snapshots() is False, \
                "冻结状态下仍允许预热 => 历史回测可能拿到今天的财报(前视)"
            assert not DB._LATEST_SNAP["valuation"]
        finally:
            DB._LATEST_SNAP_FROZEN["on"] = False

    def test_hist_path_sets_the_freeze(self):
        """`_select_hist` 必须主动置位冻结开关(纵深防御)。"""
        import inspect
        import selector as SEL
        src = inspect.getsource(SEL.RotationSelector._select_hist)
        assert "_LATEST_SNAP_FROZEN" in src, \
            "_select_hist 没有冻结最新一期快照 —— 将来有人改动就会静默引入前视"
        assert "True" in src

    def test_hist_uses_asof_accessor_not_the_snapshot(self):
        """历史路径必须走 as-of 取值, 而不是"最新一期"。"""
        import inspect
        import selector as SEL
        src = inspect.getsource(SEL.RotationSelector._select_hist)
        assert "get_financials_asof" in src
        assert "get_valuation" not in src, \
            "_select_hist 直接用了 get_valuation(无 as_of 形参, 恒为最新) => 前视风险"


class TestBulkSnapshotEquivalence:
    """批量快照与逐条 SQL **必须逐字段等价**。"""

    def _pairs(self, n=25):
        import db as DB
        import selector as SEL
        d = DB.StockDB()
        pool = SEL.filter_universe(d.get_universe())
        syms = [c for c in pool["canon"].head(n)]
        return DB, d, syms

    @_needs_h5i
    def test_valuation_snapshot_matches_scalar(self):
        DB, d, syms = self._pairs()
        try:
            DB._LATEST_SNAP["valuation"] = {}
            DB._LATEST_SNAP_FROZEN["on"] = False
            before = {c: d._get_valuation_uncached(c) for c in syms}
            assert d.prefetch_latest_snapshots(), "预热失败, 无法比对"
            after = {c: d._get_valuation_uncached(c) for c in syms}
        finally:
            DB._LATEST_SNAP["valuation"] = {}
            DB._LATEST_SNAP["financials"] = {}
        bad = {c: e for c in syms if (e := _same(before[c], after[c]))}
        assert not bad, f"批量快照与逐条 SQL 不等价: {bad}"

    @_needs_h5i
    def test_financials_snapshot_matches_scalar(self):
        DB, d, syms = self._pairs()
        try:
            DB._LATEST_SNAP["financials"] = {}
            DB._LATEST_SNAP_FROZEN["on"] = False
            before = {c: d._h5i_fin_uncached(c) for c in syms}
            assert d.prefetch_latest_snapshots(), "预热失败, 无法比对"
            after = {c: d._h5i_fin_uncached(c) for c in syms}
        finally:
            DB._LATEST_SNAP["valuation"] = {}
            DB._LATEST_SNAP["financials"] = {}
        bad = {c: e for c in syms if (e := _same(before[c], after[c]))}
        assert not bad, f"批量快照与逐条 SQL 不等价: {bad}"

    @_needs_h5i
    def test_financials_does_not_inject_liab_ratio(self):
        """**字段契约**: 逐条路径刻意不注入 `liab_ratio`, 批量路径也不许注入。

        历史包袱: duck 原实现的 `liability_ratio` 恒 None, 为"迁移前后逐位一致"
        暂不注入(见 `_h5i_fin_uncached` 的注释)。快照路径若擅自多给一个字段,
        就会改变 `governance_score` 的输入 —— 那是**加速顺手改了语义**。
        """
        DB, d, syms = self._pairs()
        try:
            DB._LATEST_SNAP["financials"] = {}
            DB._LATEST_SNAP_FROZEN["on"] = False
            assert d.prefetch_latest_snapshots()
            for c in syms:
                fin = d._h5i_fin_uncached(c)
                assert "liab_ratio" not in fin, f"{c} 的批量路径多注入了 liab_ratio"
                assert "liability_ratio" not in fin, c
        finally:
            DB._LATEST_SNAP["valuation"] = {}
            DB._LATEST_SNAP["financials"] = {}


class TestBarsSymbolIndexEquivalence:
    """`get_bars` 的 symbol 索引必须与布尔筛选**逐行等价**。"""

    @_needs_h5i
    def test_index_path_matches_boolean_filter(self):
        import db as DB
        d = DB.StockDB()
        try:
            assert d.prefetch_daily_bars(as_of=None, n=200), "预热失败"
            groups = DB._H5I_BULK_SLOT.get("groups")
            assert groups is not None, "预热没有建立 symbol 索引 => 会退回 151 万行全扫描"
            frame = DB._H5I_BULK_SLOT["frame"]
            for c in ("000001", "600000", "300750", "601318"):
                key = DB._canon_to_db(c)
                a = groups.get(key)
                b = frame[frame["symbol"] == key]
                assert a is not None and len(a) == len(b), c
                assert list(a.sort_values("d")["d"]) == list(b.sort_values("d")["d"]), c
        finally:
            DB._H5I_BULK_SLOT.update({"key": None, "frame": None, "groups": None})

    @_needs_h5i
    def test_get_bars_returns_same_rows_via_both_paths(self):
        import db as DB
        d = DB.StockDB()
        try:
            assert d.prefetch_daily_bars(as_of=None, n=200)
            fast = d.get_bars("600519", n=200)
            DB._H5I_BULK_SLOT["groups"] = None      # 强制走布尔路径
            slow = d.get_bars("600519", n=200)
            assert len(fast) == len(slow) > 0
            assert list(fast["date"]) == list(slow["date"])
            assert list(fast["close"]) == list(slow["close"])
        finally:
            DB._H5I_BULK_SLOT.update({"key": None, "frame": None, "groups": None})

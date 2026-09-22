# -*- coding: utf-8 -*-
"""「N 次扫表」巡检的判据守卫 (2026-09-22 批次)。

## 这条指标为什么存在

2026-09-22 实测: 全池 2809 只选股, 每只调 `get_valuation`(h5i `valuation` 表
**1540 万行**, 单次约 200ms) ⇒ 约 **11 分钟**。我先加的「按 symbol 记忆化」
**完全无效** —— pool 里 2809 个 canon **互不相同**, 每个 symbol 只被问一次。

**故它的目的不是"看缓存好不好", 而是回答那个决策问题: 该加缓存, 还是改查询形态。**

## 本文件锁的核心: 命中率是**诊断量, 不是健康度**

单独看命中率会误导 —— 一个**已被正确改成批量**的调用点命中率同样是 0%
(它根本不查第二次)。所以判据必须是**两个量的组合**:

    n_calls 大 AND hit_rate 近 0  ->  键唯一, 该改查询形态   <== 真信号
    n_calls 大 AND hit_rate 高    ->  键重复, 缓存有效/该加缓存
    n_calls 小                    ->  不报(否则制造噪声)

**关键用例**: 两个场景都是 2809 次 × 200ms 的**同样代价**, 但建议**相反**。
只测"会报警"是不够的 —— 必须测"建议指对方向"。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import access_probe as AP  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    AP.reset()
    yield
    AP.reset()


def _feed(name, keys, ms):
    for k in keys:
        AP.record(name, k, ms)


class TestDiscriminatesTheDecision:
    """**核心**: 同样的代价, 相反的结论。"""

    def test_unique_keys_advise_changing_query_shape(self):
        """键唯一 + 次数多 + 慢 => 明确说「**不要**加缓存, 改查询形态」。"""
        _feed("get_valuation", [f"S{i:05d}" for i in range(2809)], 200.0)
        v = AP.verdict()
        assert v["level"] == "WARN"
        item = [i for i in v["items"] if i["fn"] == "get_valuation"][0]
        assert item["hit_rate"] == 0.0
        assert "查询形态" in item["advice"], item["advice"]
        assert "而不是加缓存" in item["advice"] or "不要" in item["advice"]
        assert item["severity"] == "WARN"

    def test_repeated_keys_advise_caching(self):
        """键重复 + 同样次数 + 同样耗时 => 结论**必须相反**(该加缓存)。"""
        _feed("repeated_query", ["000001"] * 2809, 200.0)
        v = AP.verdict()
        item = [i for i in v["items"] if i["fn"] == "repeated_query"][0]
        assert item["hit_rate"] > 0.9
        assert "缓存有效" in item["advice"] or "加" in item["advice"]
        assert "查询形态" not in item["advice"], "键重复却建议改形态 —— 判据反了"

    def test_same_cost_opposite_advice(self):
        """把两个场景放一起, 断言**建议不同** —— 这条是本指标的立身之本。"""
        _feed("unique", [f"K{i}" for i in range(2000)], 100.0)
        _feed("repeat", ["K0"] * 2000, 100.0)
        v = AP.verdict()
        by = {i["fn"]: i for i in v["items"]}
        assert by["unique"]["total_ms"] == by["repeat"]["total_ms"], "两场景代价应相同"
        assert by["unique"]["advice"] != by["repeat"]["advice"], \
            "同样代价却给了同样建议 —— 指标没有诊断力"


class TestNoiseControl:
    """没有诊断价值的情形**不得**报警(否则又是一条会被忽略的告警)。"""

    def test_few_calls_not_reported(self):
        _feed("rare", ["X"], 50.0)
        v = AP.verdict()
        assert v["level"] == "OK"
        assert not v["reasons"]

    def test_many_calls_but_fast_not_warned(self):
        """调用次数多但**总耗时可忽略** => 不报。

        只按次数报警会制造噪声: 命中内存的 5000 次调用可能总共只要 5ms。
        故次数是**解释用**的量, 不是**报警**的判据。
        """
        _feed("cheap", [f"K{i}" for i in range(1500)], 0.001)
        v = AP.verdict()
        assert v["level"] == "OK", [i["fn"] for i in v["items"]]

    def test_optimized_accessor_is_not_named_and_shamed(self):
        """**实测校准出来的用例**: 已优化好的调用点不得继续被点名。

        真实数据: `get_valuation` 经批量预热后全池 2801 次**总共只花 77ms**
        (原为 11.9 分钟)。若仍因 `calls=2801 >= 1000` 被点名并建议"改查询形态",
        就会**训练人忽略这份报告** —— 与本仓反复出现的"永久假阳性"同一条教训。
        """
        _feed("get_valuation", [f"S{i}" for i in range(2801)], 0.027)  # 仿实测
        v = AP.verdict()
        assert v["level"] == "OK", f"已优化好的存取仍被点名: {v['reasons']}"

    def test_slow_but_few_calls_is_reported(self):
        """反过来: 次数少但**确实慢**(如一次全表扫描 70s)—— 仍要报。"""
        _feed("heavy_once", ["K0"], 70_000.0)
        v = AP.verdict()
        assert v["level"] == "WARN"
        assert "全表扫描" in v["items"][0]["advice"] or "按批" in v["items"][0]["advice"]


class TestHitRateDefinition:
    """命中率用 `1 - distinct/calls` 定义 —— **不依赖缓存实现**。

    这样它对**未加缓存**的函数同样有定义(那正是最需要诊断的情形),
    且不受缓存层改动影响。若改成依赖缓存内部计数器, 就只能在"已经有缓存"的地方用,
    而问题恰恰出在"还没缓存"的地方。
    """

    def test_hit_rate_is_computed_from_keys(self):
        _feed("f", ["A", "A", "B"], 1.0)
        row = AP.snapshot()[0]
        assert row["calls"] == 3 and row["distinct_keys"] == 2
        # 快照里的 hit_rate 是**四舍五入到 4 位**的展示值(源实现 round(..., 4)),
        # 故容差取 1e-3 而非 1e-6 —— 断言的应是"算法对不对", 不是"舍入精度"。
        assert abs(row["hit_rate"] - (1 - 2 / 3)) < 1e-3

    def test_all_unique_is_zero(self):
        _feed("f", ["A", "B", "C"], 1.0)
        assert AP.snapshot()[0]["hit_rate"] == 0.0

    def test_all_same_is_near_one(self):
        _feed("f", ["A"] * 100, 1.0)
        assert AP.snapshot()[0]["hit_rate"] == 0.99


class TestNeverBreaksTheCaller:
    """计量绝不得拖垮交易 —— 与 audit_chain/留痕同一条纪律。"""

    def test_record_never_raises_on_unhashable_key(self):
        AP.record("f", ["unhashable", "list"], 1.0)   # list 不可 hash
        AP.record("f", None, 1.0)
        assert AP.snapshot()[0]["calls"] == 2

    def test_record_never_raises_on_bad_ms(self):
        AP.record("f", "k", None)
        AP.record("f", "k", "not-a-number")
        assert AP.snapshot()[0]["calls"] == 2

    def test_timed_accessor_preserves_return_and_exception(self):
        """装饰器必须**原样透传**返回值与异常。"""

        class C:
            @AP.timed_accessor("probe")
            def ok(self, k):
                return {"v": 42}

            @AP.timed_accessor("probe")
            def boom(self, k):
                raise ValueError("original")

        c = C()
        assert c.ok("a") == {"v": 42}
        with pytest.raises(ValueError, match="original"):
            c.boom("b")
        # 异常路径也要被计量(否则"失败但慢"的调用点会隐身)
        row = AP.snapshot()[0]
        assert row["calls"] == 2

    def test_timed_accessor_preserves_wrapped(self):
        class C:
            @AP.timed_accessor("probe")
            def f(self, k):
                """doc"""
                return 1
        assert C.f.__wrapped__ is not None
        assert C.f.__doc__ == "doc"


class TestProductionWiring:
    """指标必须挂在**生产路径**上, 否则只是文档。"""

    def test_db_accessors_are_probed(self):
        import db as DB
        for name in ("get_bars", "get_valuation", "get_financials"):
            fn = getattr(DB.StockDB, name)
            assert hasattr(fn, "__wrapped__"), \
                f"{name} 未加计量 —— 巡检看不到它的调用次数"

    def test_probe_is_importable_and_has_cli(self):
        assert callable(AP.verdict) and callable(AP.snapshot) and callable(AP.reset)

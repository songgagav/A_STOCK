# -*- coding: utf-8 -*-
"""「N 次扫表」巡检: 存取层可观测性 (2026-09-22 批次)。

## 这条指标要回答什么

2026-09-22 实测: 全池 2809 只选股, 每只调 `get_valuation` + `get_financials`,
两者在 h5i 上都是「扫整表取最新一期」(`valuation` 有 **1540 万行**), 单次约 200ms
⇒ 约 **11 分钟**。我先加的「按 symbol 记忆化」**完全无效** ——
pool 里 2809 个 canon **互不相同**, 每个 symbol 只被问一次, 任何按 symbol 的缓存
都必然全部未命中。

**故这条指标的目的不是"看缓存好不好", 而是直接回答那个决策问题:**

    N 次慢查询
      ├─ 键重复(缓存命中率高) -> 加缓存有效
      └─ 键唯一(命中率近 0)   -> 缓存必然全 miss, 必须改**查询形态**(逐条 -> 批量聚合)

## 为什么需要"命中率"这个量(而它单独看会误导)

命中率**单独**看会误导: 一个**已被正确改成批量**的调用点, 命中率同样是 0%
(它根本不查第二次)。所以判据必须是**两个量的组合**:

    n_calls 大  AND  hit_rate 近 0   ->  **键唯一, 该改查询形态**   <== 真信号
    n_calls 大  AND  hit_rate 高     ->  键重复, 缓存已生效(或该加缓存)
    n_calls 小                        ->  无所谓

即: **命中率是"诊断量", 不是"健康度"**。它只有在 `n_calls` 大时才有意义 ——
`n_calls` 小的时候, 命中率 0% 与 100% 都不值得看。

## 计量口径

- `n_calls`        : 该存取函数被调用次数
- `n_distinct_keys`: 去重后的键数
- `hit_rate`       : `1 - n_distinct_keys / n_calls`(0 = 每个键都唯一)
- `total_ms`       : 累计耗时

`hit_rate` 用 `distinct/calls` 算而**不**依赖缓存实现 —— 这样它对
**未加缓存**的函数同样有定义(那正是最需要诊断的情形), 且不受缓存层改动影响。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()

#: 函数名 -> {calls, keys(set), total_ms, max_ms}
_STATS: dict = {}

#: 报告口径: **累计耗时**决定要不要报(**实测校准**)。
#: 为什么不是"次数达标才报": 首版如此, 结果把**已优化好**的 `get_valuation`
#: 也点名了 —— 它经批量预热后总共只花 **77ms**(原 11.9 分钟), 却因 `calls=2801`
#: 仍在报告里, 且建议「请改查询形态」。**优化完还被点名会训练人忽略报告**。
#: 次数只在**解释原因**时用(键唯一 vs 键重复), 不用于决定是否报。
TOTAL_MS_WARN = 30_000.0   # 单个存取函数累计耗时超过 30s 才值得看
CALLS_WARN = 1000          # 仅用于解释"为什么会有这么多次"(配合命中率)
HIT_RATE_LOW = 0.05        # 命中率 < 5% 视为"键唯一"


def reset() -> None:
    """开始一轮巡检前清空(由 run_daily / 选股入口调用)。"""
    with _LOCK:
        _STATS.clear()


def record(fn_name: str, key, elapsed_ms: float) -> None:
    """记一次调用。**失败不抛** —— 计量绝不得拖垮交易。"""
    try:
        with _LOCK:
            st = _STATS.get(fn_name)
            if st is None:
                st = _STATS[fn_name] = {"calls": 0, "keys": set(),
                                        "total_ms": 0.0, "max_ms": 0.0}
            st["calls"] += 1
            # 键只用于**计数去重**, 故限制集合大小以免内存无界(超出后按"未知"计,
            # 命中率会偏保守地偏高 —— 但那只会**减少**误报, 不会制造噪声)
            if len(st["keys"]) <= 200_000:
                try:
                    st["keys"].add(key)
                except TypeError:
                    pass
            st["total_ms"] += float(elapsed_ms or 0.0)
            if elapsed_ms and elapsed_ms > st["max_ms"]:
                st["max_ms"] = float(elapsed_ms)
    except Exception:  # noqa: BLE001
        pass


def snapshot() -> list[dict]:
    """当前各存取函数的计量快照(按累计耗时降序)。"""
    out = []
    with _LOCK:
        for name, st in _STATS.items():
            calls = st["calls"]
            nk = len(st["keys"])
            hit = (1.0 - nk / calls) if calls else 0.0
            out.append({
                "fn": name, "calls": calls, "distinct_keys": nk,
                "hit_rate": round(hit, 4),
                "total_ms": round(st["total_ms"], 1),
                "avg_ms": round(st["total_ms"] / calls, 3) if calls else 0.0,
                "max_ms": round(st["max_ms"], 1),
            })
    return sorted(out, key=lambda x: -x["total_ms"])


def verdict(rows: list[dict] | None = None) -> dict:
    """给出**可行动**的结论: 该加缓存, 还是该改查询形态。

    返回 {level, items, reasons}。level: OK / WARN。

    **报告口径(实测校准过)**: 只有在**累计耗时可观**时才进报告。
    首版把「调用次数达标」也当成进报告的条件, 于是实测出现这种噪声:
    `get_valuation` 经批量预热后**总共只花 77ms**(原为 11.9 分钟), 却因为
    `calls=2801 >= 1000` 仍在报告里, 并给出「请改查询形态」的建议 ——
    而它**已经**改好了。**优化完还被点名, 会训练人忽略这份报告**
    (与本仓反复出现的"永久假阳性"同一条教训)。
    故: **次数用于"解释原因", 耗时用于"决定要不要报"**。
    """
    rows = snapshot() if rows is None else rows
    items, reasons = [], []
    for r in rows:
        # 只有累计耗时可观才值得报 —— 次数单独不构成噪声源
        if r["total_ms"] < TOTAL_MS_WARN:
            continue
        if r["calls"] >= CALLS_WARN and r["hit_rate"] < HIT_RATE_LOW:
            advice = ("**键唯一** ⇒ 缓存必然全 miss, 请改**查询形态**"
                      "(逐条 -> 批量聚合), 而不是加缓存")
        elif r["hit_rate"] >= HIT_RATE_LOW:
            advice = "**键有重复** ⇒ 加/调大缓存有效"
        else:
            advice = ("调用次数少但**单次很贵**(疑一次全表扫描) ⇒ "
                      "看该存取是否可按批取数")
        it = {**r, "advice": advice, "severity": "WARN"}
        items.append(it)
        reasons.append(
            f"{r['fn']}: {r['calls']} 次 / 去重 {r['distinct_keys']} 键 / "
            f"命中率 {r['hit_rate']:.1%} / 累计 {r['total_ms']:.0f}ms "
            f"(单次均 {r['avg_ms']:.3f}ms) —— {advice}")
    return {"level": "WARN" if items else "OK",
            "items": items, "reasons": reasons,
            "thresholds": {"calls_warn": CALLS_WARN, "total_ms_warn": TOTAL_MS_WARN,
                           "hit_rate_low": HIT_RATE_LOW}}


class timed_accessor:
    """装饰器: 给 StockDB 的存取方法加计量。**不改变任何返回值/异常语义**。

    用法: `@timed_accessor("get_valuation")` 放在方法上;
    键默认取第一个位置参数(canon), 即"这个 symbol 被问了几次"。
    """

    def __init__(self, name: str, key_arg: int = 1):
        self.name = name
        self.key_arg = key_arg

    def __call__(self, fn):
        def wrapper(*a, **kw):
            t0 = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                try:
                    key = a[self.key_arg] if len(a) > self.key_arg else None
                    record(self.name, key, (time.perf_counter() - t0) * 1000.0)
                except Exception:  # noqa: BLE001
                    pass
        wrapper.__name__ = getattr(fn, "__name__", self.name)
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        wrapper.__wrapped__ = fn
        return wrapper


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="「N 次扫表」巡检: 存取层计量")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    v = verdict()
    if args.json:
        print(json.dumps(v, ensure_ascii=False, indent=2))
        return 0
    print(f"level = {v['level']}  (阈值 {v['thresholds']})")
    for r in v["reasons"]:
        print("  -", r)
    if not v["reasons"]:
        print("  (本轮没有值得关注的存取热点)")
    return 0 if v["level"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(_main())

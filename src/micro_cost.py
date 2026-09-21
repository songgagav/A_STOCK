# -*- coding: utf-8 -*-
"""拆单成本口径 (路线图 ⑰): **在本仓的成本假设下, 拆单是成本中性的**。

这个模块的结论是解析的, 不需要跑回测
--------------------------------------
本仓虚拟盘的成交成本是 `PaperBook.buy/sell` 在**没有**传 `avg_daily_volume` 时
走的常量分支:

    单边成本率 = PAPER.slippage(万5) + PAPER.impact_cost(万2) = 7bps, **与订单大小无关**

在这个口径下把一笔单拆成 N 档:

    拆单总成本 = Σ_i (7bps × n_i) = 7bps × Σ n_i = 7bps × n = 一次性总成本

即 **拆单在成本上恒等**, 一档到二十档都一样。差异只在别处:
  · 每档的成交价不同(跨 tick 价格会动) —— 这是**价格风险**, 不是成本模型问题;
  · 单 tick 的吃单速度被限制 —— 这是**压参与率**, 也是拆单唯一真实的理由。

那"冲击成本"呢?(把话说明白, 免得后人重复走一遍)
----------------------------------------------
本仓有两个可以算冲击的东西, 但**都不足以支撑拆单决策**:

  1. `slippage_model.market_impact_rate`: 临时项 `0.5·p·vol` 乘上名义额后
     对**订单规模是二次的**, 于是"拆 N 档"会把总成本按 1/N 缩水 —— 模型自带
     "拆单更便宜"的结论, 拿它做对照是循环论证。**本身也站不住**: 本仓基准
     滑点是**定值万5**, 冲击成本**定为万2**, 也就是说"冲击与规模无关"本就是
     本仓的假设; 在这个假设下不存在需要拆单去规避的冲击。
  2. 自建线性模型 `冲击 = k·p`: 若把 k 当常数, 同样得到"成本 ∝ 名义额²/ADV",
     于是拆单凭空便宜 1/N —— 同一个错误换个写法。**线性冲击模型在算术上
     就不可能回答这个问题**: 它要么恒等(费率不随规模变), 要么二次(拆分即套利)。

真实市场冲击对规模是**次线性**的(通常按 √p 量级), 所以"大单拆开能少亏"在真实
市场是成立的 —— 但**本仓没有任何成交数据可以标定那个次线性指数**:
`data/state.json.trades_history` 为空, `data/order_audit.jsonl` 不存在。
所以本模块**不假装能算这个收益**, 只回答能算清的那部分:
**在当前成本口径下拆单不改变成本, 因此 `_to_used` 记账口径是可比的。**

可用于后续标定的接口
--------------------
`observed_cost_table()` 把"实测单边成本 vs 名义额"整理成表, 供将来有成交数据时
回归出真实的规模弹性。在拿到数据之前, 它的 `n=0` 就是诚实的答案。
"""
from __future__ import annotations

TRADING_DAYS = 252


def constant_rate() -> float:
    """当前虚拟盘的单边常量成本率(与订单大小无关)。"""
    try:
        from config import PAPER
        return float(PAPER.get("slippage", 0.0005)) + float(PAPER.get("impact_cost", 0.0002))
    except Exception:  # noqa: BLE001
        return 0.0007


def split_cost_identity(notional: float, n_slices: int, *,
                        rate: float | None = None) -> dict:
    """**解析结论**: 常量成本口径下, 拆 N 档与一次性下单的成本完全相同。

    返回 {'rate','unsplit_cost','split_cost','delta_cost','delta_rate',
          'identical': True, 'n_slices','proof'}

    这不是近似, 也不是数值巧合: `Σ_i rate·n_i = rate·Σ_i n_i = rate·n`。
    故 `delta_cost` 恒为 0(浮点误差内), 与档数、名义额、标的都无关。
    """
    r = float(rate if rate is not None else constant_rate())
    n = float(notional or 0.0)
    k = max(int(n_slices or 1), 1)
    if k <= 1:
        unsplit = split = r * n
        delta = 0.0
    else:
        unsplit = r * n
        # 每档名义额 n/k, 费率相同 => 总额 = k · r · (n/k) = r·n
        split = sum(r * (n / k) for _ in range(k))
        delta = split - unsplit
    return {
        "rate": r, "n_slices": k, "notional": n,
        "unsplit_cost": unsplit, "split_cost": split,
        "delta_cost": delta, "delta_rate": (delta / n) if n > 0 else 0.0,
        "identical": abs(delta) < 1e-9 * max(abs(unsplit), 1.0),
        "proof": ("单边成本率与订单大小无关 => Σ_i r·n_i = r·Σ_i n_i = r·n, "
                  "拆几档都一样. 故拆单**不改变**成本, 只改变成交时点与单 tick 吃单速度; "
                  "`_to_used` 按成交额累计, 因此拆与不拆在记账口径下可比."),
    }


def participation_cap_qty(*, adv: float | None, participation_cap: float,
                          price: float, lot: int = 100) -> int | None:
    """单 tick 最多能吃多少**股**(= ADV × cap / 价格, 向下取整到整手)。

    这是拆单真正起作用的地方: 它限制的是"一个 tick 里吃多少", 不是"成本多少"。
    无 ADV 或单价非正时返回 None(调用方应不设限, 保持既有行为)。
    """
    if not adv or adv <= 0 or not participation_cap or participation_cap <= 0:
        return None
    px = float(price or 0.0)
    if px <= 0:
        return None
    qty = (float(adv) * float(participation_cap)) / px
    if lot and lot > 0:
        qty = (qty // lot) * lot
    return int(max(qty, 0))


def observed_cost_table(trades: list | None = None) -> dict:
    """把实测成交整理成"名义额 -> 单边成本率"表, 供将来回归真实规模弹性。

    `trades` 为 None 时读本仓的虚拟盘台账。**当前为空**(无成交数据),
    函数如实返回 `n=0` 与原因 —— 这是本模块在拿到数据之前唯一诚实的答案,
    也是"拆单到底省不省"这个问题目前**无法用本仓数据回答**的证据。
    """
    rows = list(trades or [])
    source = "caller"
    if not trades:
        source = "data/state.json.trades_history"
        try:
            import json
            import os
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(base, "data", "state.json"), encoding="utf-8") as f:
                st = json.load(f)
            hist = st.get("trades_history") or {}
            for day, items in hist.items():
                for t in items or []:
                    rows.append({**t, "day": day})
        except Exception:  # noqa: BLE001
            rows = []
    out = {"n": len(rows), "source": source, "buckets": [],
           "note": "", "regression_ready": False}
    if not rows:
        out["note"] = ("无成交数据 => 无法标定真实的规模弹性; "
                       "拆单的收益上限在本仓当前无法用数据回答")
        return out
    by_notional: dict = {}
    for t in rows:
        try:
            notional = abs(float(t.get("price", 0)) * float(t.get("qty", 0)))
            rate = float(t.get("impact_bps", 0)) / 1e4 if t.get("impact_bps") is not None else None
        except Exception:  # noqa: BLE001
            continue
        if notional <= 0 or rate is None:
            continue
        by_notional.setdefault(round(notional, -3), []).append(rate)
    for nb, rs in sorted(by_notional.items()):
        out["buckets"].append({"notional": nb, "n": len(rs),
                               "mean_rate": sum(rs) / len(rs)})
    out["regression_ready"] = len(out["buckets"]) >= 3
    out["note"] = ("已按名义额分桶; 桶数 >= 3 时可用回归估计规模弹性 "
                   "(rate ∝ notional^beta), beta < 0 说明真实冲击是次线性的, "
                   "那时拆单才有可量化的收益")
    return out

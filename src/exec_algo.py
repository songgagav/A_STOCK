# -*- coding: utf-8 -*-
"""TWAP / VWAP 拆单执行算法(离线规划 + 仿真, **不接实盘下单通道**).

本模块只做两件事: (1) 把一个母单拆成若干档的**目标股数**; (2) 用本仓已有的
Almgren-Chriss 成本模型**仿真**该拆单方案相对"一次性下单"的滑点差. 它不读行情
文件、不访问网络、不调用撮合层, 输出的 `slices` 由调用方自己决定怎么喂给
`PaperBook.buy/sell`.

设计要点与取舍
--------------
1. **成本公式只复用, 不新造**: 单档滑点一律走 `slippage_model.decompose_slippage()`,
   "一次性下单"基准走同一函数在 `execution_horizon_days=0` 时的分支(等价于
   `almglen_chriss_slippage()`). 因此"拆单 vs 一次性"的对比是**同一模型内**的
   对比, 而不是两套公式互比; 本模块不引入任何新的冲击成本公式或系数.
2. **整手约束优先**: A 股买入必须 100 股整数倍, 所以拆单以"手"为最小分配单位,
   `total_qty` 不是 `lot` 整数倍时**向下取整到整手**, 被丢弃的零头由
   `lot_split()` 单独暴露, 并计入 `plan_execution()` 的 `unscheduled` 与 `notes`
   —— 绝不静默丢弃.
3. **默认阈值全部是占位值**: 每个默认数字(参与率上限、档数、κ)都在 docstring 里
   标注为"占位默认值/无内部依据", 并要求生产接线处显式传入. 仓库纪律: 不得悄悄
   引入没有依据的数字.
4. **结论可被证伪**: `saving_bps = naive - sliced`, **不做 `max(0, ·)`**. 若参数下
   拆单因执行风险(时间风险)分量主导而比一次性更贵, 它就是负数.
5. **口径分离**: `saving_bps` 只衡量**成本模型口径的滑点差**; 返回里的
   `slice_vwap_price` / `naive_price` 是含**各档参考价自身路径(市场漂移)**的成交价,
   两者不必然同号 —— 这是刻意分离, 不要拿它们互相校验(见 `simulate_execution`).

与本仓其它模块的关系
--------------------
- 成本模型: `slippage_model.decompose_slippage` / `almglen_chriss_slippage`(轻依赖, 直接 import).
- 撮合层: `PaperBook.buy/sell(canon, qty, price, avg_daily_volume=, volatility=,
  execution_horizon_days=, urgency_kappa=)` 支持逐档传参, 因此 `simulate_execution()`
  返回的 `per_slice` 可以直接映射成逐档撮合调用. 但本模块**刻意不 import
  `paper_book` / `config`**(避免拉起 DB/配置等重依赖与导入副作用).
- **单位口径警告(最容易接错的地方)**:
  `slippage_model` 的 `avg_daily_volume` 是**成交额(元)**;
  而 `participation_cap_slices()` 的 `adv` 是**成交量(股)**.
  两者必须由调用方用价格换算: `adv_shares = adv_yuan / price`. 之所以不统一,
  是因为参与率上限比较的是"股数 vs 股数", 而冲击模型吃的是"金额 vs 金额".

公开接口
--------
- `lot_split(total_qty, lot=100)`                      -> (整手可执行量, 零头)
- `schedule_twap(total_qty, n_slices, *, lot, side)`   -> list[int]
- `schedule_vwap(total_qty, volume_profile, *, lot, side)` -> list[int]
- `participation_cap_slices(total_qty, *, adv, cap_pct, lot, n_slices)` -> dict
- `plan_execution(total_qty, *, algo, n_slices, ...)`  -> dict
- `naive_slippage_bps(order_size, avg_daily_volume, volatility)` -> float
- `simulate_execution(plan, prices, *, avg_daily_volume, volatility, ...)` -> dict
- `_main(argv=None)`                                   -> int  (CLI, 仅 --selftest 打印)

CLI 自检:
  python src/exec_algo.py --selftest
"""
from __future__ import annotations

import argparse
import math
from typing import Any, Sequence

import numpy as np

from slippage_model import almglen_chriss_slippage, decompose_slippage

__all__ = [
    "PARTICIPATION_CAP_PCT_PLACEHOLDER",
    "DEFAULT_N_SLICES_PLACEHOLDER",
    "URGENCY_KAPPA_PLACEHOLDER",
    "lot_split",
    "schedule_twap",
    "schedule_vwap",
    "participation_cap_slices",
    "plan_execution",
    "naive_slippage_bps",
    "simulate_execution",
]

# ==========================================================================
# 占位默认值 (placeholder) —— 全部"无内部依据", 生产接线必须显式传入
# ==========================================================================
PARTICIPATION_CAP_PCT_PLACEHOLDER = 0.10
"""单档参与率上限的**占位默认值** (0.10).

**这是占位值, 不是本仓库校准过的参数**: 取 10% 仅仅因为它是"单标的日内参与率
上限"在行业里常见的量级, 本模块没有任何回测/统计依据支持这个数字.
生产接线处必须由调用方按标的流动性、策略容量与合规要求显式传入 `cap_pct`;
`plan_execution()` 在使用该占位值时会往 `notes` 里写一条显式告警.
"""

DEFAULT_N_SLICES_PLACEHOLDER = 10
"""默认档数的**占位默认值** (10 档).

**占位值**: 10 档只是一个便于人工核对的示意切分(如把 4 小时连续竞价切 10 段),
不来自任何日内成交量分布统计. 生产接线必须显式传 `n_slices`, 或让 `n_slices`
等于 `volume_profile` 的长度.
"""

URGENCY_KAPPA_PLACEHOLDER = 1.0
"""执行紧迫度 κ 的默认值 (1.0).

这个数字**沿用** `slippage_model.execution_risk_rate()` 自身的默认值(而不是本模块
新造一个数), 以便与撮合层口径一致. 它仍然只是一个默认值: 被动跟随盘口的拆单
通常取 0.3~1.0, 生产接线应显式传入.
"""


# ==========================================================================
# 内部校验工具
# ==========================================================================
def _require_int(value: Any, name: str, *, allow_zero: bool = True) -> int:
    """把 value 规整为 int, 非法时抛 ValueError(消息含字段名与实际值)."""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数, 收到布尔值 {value!r}")
    if isinstance(value, (int, np.integer)):
        out = int(value)
    elif isinstance(value, (float, np.floating)):
        fv = float(value)
        if not math.isfinite(fv) or not fv.is_integer():
            raise ValueError(f"{name} 必须是整数(股数/档数), 收到 {value!r}")
        out = int(fv)
    else:
        raise ValueError(f"{name} 必须是整数(股数/档数), 收到 {type(value).__name__}: {value!r}")
    if out < 0:
        raise ValueError(f"{name} 不能为负, 收到 {out}")
    if out == 0 and not allow_zero:
        raise ValueError(f"{name} 必须为正, 收到 0")
    return out


def _require_positive_float(value: Any, name: str) -> float:
    """把 value 规整为有限正数 float, 否则 ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是正数, 收到布尔值 {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是正数, 收到 {value!r} ({exc})") from exc
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{name} 必须是有限正数, 收到 {out!r}")
    return out


def _require_lot(lot: Any) -> int:
    """校验最小交易单位 lot(A 股买入整手 = 100 股)."""
    return _require_int(lot, "lot", allow_zero=False)


def _require_side(side: Any) -> str:
    """校验买卖方向, 只接受 'buy' / 'sell'(大小写敏感, 不做任何猜测性纠正)."""
    s = str(side)
    if s not in ("buy", "sell"):
        raise ValueError(f"side 只接受 'buy' / 'sell', 收到 {side!r}")
    return s


def _require_cap_pct(cap_pct: Any) -> float:
    """校验参与率上限: 必须落在 (0, 1]. >1 表示吃得比市场总量还多, 属非法输入."""
    out = _require_positive_float(cap_pct, "cap_pct")
    if out > 1.0:
        raise ValueError(f"cap_pct 必须 <= 1.0(参与率不可能超过市场总量的 100%), 收到 {out!r}")
    return out


def _coerce_profile(volume_profile: Any) -> list[float]:
    """把 volume_profile 规整为长度>0 的一维非负有限序列.

    显式抛 ValueError 的情形(全部**不静默**处理):
      - 不是一维数值序列(含嵌套/二维/ragged);
      - 长度为 0;
      - 含 NaN / Inf;
      - 含负值(负成交量占比无意义, 不做"取绝对值"或"截断到 0"的粉饰);
      - 全 0(无任何量分布信息, 无法归一化).
    注意: 长度为 1 的占比序列是合法的, 等价于"不分档".
    """
    try:
        arr = np.asarray(volume_profile, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"volume_profile 必须是一维数值序列: {exc}") from exc
    if arr.ndim != 1:
        raise ValueError(
            f"volume_profile 必须是一维序列(长度为档数), 收到 shape={arr.shape}")
    if arr.size == 0:
        raise ValueError("volume_profile 不能为空(长度=档数, 至少 1 档)")
    if not np.all(np.isfinite(arr)):
        raise ValueError("volume_profile 含 NaN/Inf, 无法作为成交量占比使用")
    if np.any(arr < 0):
        bad = np.flatnonzero(arr < 0)[:3].tolist()
        raise ValueError(
            f"volume_profile 含负值(索引 {bad}), 成交量占比不允许为负; "
            "本模块不做取绝对值/截断到 0 的静默修补")
    if float(arr.sum()) <= 0.0:
        raise ValueError("volume_profile 全为 0, 无法归一化(没有量分布信息可供分配)")
    return [float(x) for x in arr]


# ==========================================================================
# 整手拆分与档内分配工具
# ==========================================================================
def lot_split(total_qty: Any, lot: Any = 100) -> tuple[int, int]:
    """把股数按整手拆成 (整手可执行量, 零头), 零头**显式返回, 不静默丢弃**.

    Parameters
    ----------
    total_qty : int
        母单股数, >= 0.
    lot : int
        最小交易单位(股), 必须 > 0. A 股买入整手传 100(默认);
        卖出若要表达"允许零股"(A 股零股须一次性卖出), 显式传 lot=1.

    Returns
    -------
    (tradable_qty, remainder_qty) : tuple[int, int]
        `tradable_qty = (total_qty // lot) * lot`, `remainder_qty = total_qty - tradable_qty`.
        恒有 `tradable_qty + remainder_qty == total_qty`.

    Notes
    -----
    买入侧: `remainder_qty` 在 A 股规则下**无法**按整手买入, 调用方必须自己决定
    (放弃、攒到下次、或与其它持仓合并)。本模块只负责"如实报出来"。
    卖出侧: A 股允许零股卖出, 但本模块**不会**因为 side='sell' 就偷偷改变分配形状;
    要卖零股请显式传 `lot=1`, 让非整手行为成为调用方的显式选择。
    """
    qty = _require_int(total_qty, "total_qty")
    l = _require_lot(lot)
    tradable = (qty // l) * l
    return int(tradable), int(qty - tradable)


def _front_load_lots(total_lots: int, n_slices: int) -> list[int]:
    """把 total_lots 手"逐手"铺到前若干档: 前 extra 档各多 1 手, 其余档相等.

    这是本模块 TWAP 的余数分配规则(前重后轻), 理由见 `schedule_twap` docstring.
    当 `n_slices > total_lots` 时, 结果是"前 total_lots 档各 1 手, 其余档 0".
    """
    base, extra = divmod(int(total_lots), int(n_slices))
    return [base + (1 if i < extra else 0) for i in range(int(n_slices))]


def _largest_remainder_lots(weights: Sequence[float], total_lots: int) -> list[int]:
    """最大余数法(Hamilton)把 total_lots 手按权重分到各档, 结果和**恰好**等于 total_lots.

    规则: 先取 `floor(w_i / Σw × total_lots)`, 再把剩下的手数按**小数部分降序**
    逐手发放; 小数部分相同则**索引小者优先**(保证结果可复现, 不做随机/集合序).
    并发性: 该方法满足配额性质 `floor(ideal_i) <= lots_i <= ceil(ideal_i)`, 因此
    权重更大的档分到的手数不会更少(弱单调), 且权重为 0 的档**永远分不到手数**
    (小数部分为 0, 只有在"剩余手数 > 小数部分>0 的档数"时才可能被选中, 而数学上
    剩余手数 = Σ小数部分 < 小数部分>0 的档数, 故不可能发生).
    """
    w = np.asarray(list(weights), dtype=np.float64)
    if w.ndim != 1 or w.size == 0:
        raise ValueError("weights 必须是非空一维序列")
    total = int(total_lots)
    if total < 0:
        raise ValueError(f"total_lots 不能为负, 收到 {total_lots!r}")
    if total == 0:
        return [0] * int(w.size)
    s = float(w.sum())
    if not math.isfinite(s) or s <= 0:
        raise ValueError("weights 之和必须为正, 否则无法按权重分配")
    ideal = w / s * total
    floors = np.floor(ideal + 1e-9).astype(np.int64)   # 1e-9 修正浮点尾差
    remaining = total - int(floors.sum())
    if remaining > 0:
        frac = ideal - floors
        order = sorted(range(w.size), key=lambda i: (-float(frac[i]), i))
        for i in order[:remaining]:
            floors[i] += 1
    return [int(x) for x in floors]


# ==========================================================================
# 1) TWAP
# ==========================================================================
def schedule_twap(total_qty: Any, n_slices: Any, *, lot: Any = 100,
                  side: Any = "buy") -> list[int]:
    """TWAP: 把母单**均匀**拆成 n_slices 档, 返回每档目标股数(买入侧为整手).

    Parameters
    ----------
    total_qty : int
        母单股数, >= 0.
    n_slices : int
        档数, >= 1. 允许 `n_slices > total_qty // lot`(档数多于可分手数), 见下.
    lot : int
        最小交易单位(股), 默认 100(A 股买入整手). 必须 > 0.
    side : str
        'buy' / 'sell', 只做校验与语义标注; **不改变分配形状**(见 `lot_split`)。

    Returns
    -------
    list[int]
        长度恒为 `n_slices`。

    余数分配规则(本模块的显式选择)
    ------------------------------
    **前重后轻(front-loaded) + 逐手摊**: 令 `lots = (total_qty // lot)`,
    `base, extra = divmod(lots, n_slices)`, 则第 0..extra-1 档各 `base+1` 手,
    其余档各 `base` 手。为什么是这样而不是"把余数全部堆到最后一档":

    1. **逐手摊保证 `max(slices) - min(slices) <= lot`(各档最多差 1 手)**。
       若把余数堆到末档, 当余数有 3 手时末档会是其它档的 4 倍, 拆单表出现离群档,
       冲击成本集中在一档, 与 TWAP"均匀"的初衷直接冲突。
    2. **摊到前面而不是后面**: A 股买入的"未完成风险"不对称 —— 越靠后的档越可能遇到
       涨停/停牌/资金被其它标的占用; 且 14:57-15:00 收盘集合竞价的成交价波动更大,
       把额外手数压到末档等于把不确定性堆在最不确定的一档。
    3. **代价(如实写)**: 前重后轻让冲击成本略微提前发生。若调用方更在意尾盘流动性,
       应当自己把返回的档序做时间映射(例如把前几档映射到 10:00 之后的时段)——本模块
       **不提供"把余数放末档"的开关**, 以保证同一函数只有一种可断言行为。

    边界行为
    --------
    - `total_qty` 不是 `lot` 整数倍: **向下取整到整手**, 零头不计入任何档;
      用 `lot_split(total_qty, lot)[1]` 或 `plan_execution()['unscheduled']` 取回,
      本函数返回的 list 里看不到零头(这是"暴露在别处", 不是"静默丢弃")。
      因此恒有 `sum(slices) == total_qty - remainder_qty`。
    - `n_slices > lots`(档数多于可分手数): **前 lots 档各 1 手, 其余档为 0**。
      即返回长度仍等于 `n_slices`(时间栅格不压缩, 便于与逐档价格/量对齐),
      但尾部出现"空档不成交"; 本模块**不做 `lot` 以下的小数拆分**(A 股买不进 50 股),
      也不会把 1 手拆成两档各 0.5 手。
    - `total_qty == 0`: 返回全 0 的 n_slices 档(合法的空计划)。
    - `total_qty < lot` 且 > 0: 全部档为 0, 整个母单都进"零头"。

    Raises
    ------
    ValueError
        `total_qty` / `n_slices` / `lot` 非法(负值、非整数、lot<=0、n_slices<=0)
        或 `side` 不是 'buy'/'sell'。
    """
    qty = _require_int(total_qty, "total_qty")
    n = _require_int(n_slices, "n_slices", allow_zero=False)
    l = _require_lot(lot)
    _require_side(side)
    tradable, _remainder = lot_split(qty, l)
    lots = tradable // l
    return [u * l for u in _front_load_lots(lots, n)]


# ==========================================================================
# 2) VWAP
# ==========================================================================
def schedule_vwap(total_qty: Any, volume_profile: Any, *, lot: Any = 100,
                  side: Any = "buy") -> list[int]:
    """VWAP: 按各时段成交量占比分配母单, 返回每档目标股数(买入侧为整手).

    Parameters
    ----------
    total_qty : int
        母单股数, >= 0.
    volume_profile : Sequence[float]
        **长度等于档数**的各时段成交量占比序列(如分钟/半小时成交量)。**无需归一化**,
        内部按 `w_i = p_i / Σp` 归一化; 全 0 会抛 ValueError(没有分布信息)。
        允许含 0(该档不分单, 不参与归一化分母以外的一切计算, 且**不会除零**)。
    lot : int
        最小交易单位(股), 默认 100。
    side : str
        'buy' / 'sell', 只做校验与语义标注, 不改变分配形状。

    Returns
    -------
    list[int]
        长度恒为 `len(volume_profile)`; `sum(result) == total_qty - (total_qty % lot)`。

    取整如何收敛到 total_qty(明确了规则, 不靠"四舍五入碰运气")
    --------------------------------------------------------
    1. 先算整手可执行量 `lots_total = (total_qty // lot)`; 零头已在 `lot_split()` 里
       被剥出并如实报告, 不参与分配。
    2. 目标手数 `ideal_i = w_i × lots_total` 向下取整, 得到 `Σfloor <= lots_total`。
    3. 差额 `remaining = lots_total - Σfloor` 手, 用**最大余数法**(largest remainder /
       Hamilton)按 `ideal_i` 的小数部分降序逐手补发; 小数部分相同则**索引小者优先**。
       因为 `remaining == Σ(小数部分)`, 补发后 **`Σlots == lots_total` 严格成立**,
       于是 `sum(slices) == lots_total × lot` 严格成立(不是"近似相等")。
    4. 该方法的性质(已被单测断言): 配额性 `floor(ideal_i) <= lots_i <= ceil(ideal_i)`;
       权重更大的档手数不会更少(弱单调); 0 占比档**永远分不到手数**(含余数补发阶段)。

    边界行为
    --------
    - `volume_profile` 含 0: 该档结果恒为 0(不除零、不补量)。
    - `volume_profile` 含负值 / NaN / Inf / 空 / 全 0 / 非一维: **显式 ValueError**,
      不做取绝对值、截断到 0、或丢弃该档的静默修补。
    - "长度与档数不符": 本函数的档数**就是** `len(volume_profile)`; 若在
      `plan_execution()` 里同时给了 `n_slices` 与 `volume_profile`, 两者长度必须
      一致, 否则抛 ValueError(见该函数), 不做重采样/截断。
    - `n_slices`(即占比长度)大于可分手数时: 有限的手数会被分给**占比最高**的档
      (最大余数法的自然结果), 而不是像 TWAP 那样铺在最前几档 —— 这是刻意的行为差异。
    - `total_qty < lot`: 返回全 0 的序列, 整个母单都算零头。

    Raises
    ------
    ValueError
        `total_qty` / `lot` / `side` 非法, 或 `volume_profile` 不满足上述约束。
    """
    qty = _require_int(total_qty, "total_qty")
    l = _require_lot(lot)
    _require_side(side)
    prof = _coerce_profile(volume_profile)
    tradable, _remainder = lot_split(qty, l)
    lots_total = tradable // l
    lots = _largest_remainder_lots(prof, lots_total)
    out = [u * l for u in lots]
    assert sum(out) == tradable, "VWAP 分配必须严格收敛到整手可执行量"   # 内部自检
    return out


# ==========================================================================
# 3) 参与率上限
# ==========================================================================
def participation_cap_slices(total_qty: Any, *, adv: Any,
                             cap_pct: Any = PARTICIPATION_CAP_PCT_PLACEHOLDER,
                             lot: Any = 100,
                             n_slices: Any = DEFAULT_N_SLICES_PLACEHOLDER) -> dict:
    """按"单档成交量不超过该档市场成交量的一定比例"给出**在执行窗口内吃得下**的计划.

    **单位口径(最容易接错的地方)**: `adv` 是**日均成交量(股)**, 与 `total_qty` 同单位;
    而 `slippage_model` 的 `avg_daily_volume` 是**成交额(元)**。调用方必须换算:
    `adv_shares = adv_yuan / price`(或用成交量字段而不是成交额字段)。

    Parameters
    ----------
    total_qty : int
        母单股数, >= 0。
    adv : float
        该标的**日均成交量(股)**, 必须 > 0。缺失 ADV 会抛 ValueError, 不会把
        "没有流动性信息"静默当成"全部吃不下"或"全部吃得下"。
    cap_pct : float
        单档参与率上限, 取值 (0, 1]。
        **占位默认值 0.10 已在 docstring 与常量处标注为"无内部依据的占位值",
        生产接线处必须显式传入**(见 `PARTICIPATION_CAP_PCT_PLACEHOLDER`)。
        返回 dict 里的 `cap_pct_is_placeholder` 会告诉接线方"这个 10% 是占位值"。
    lot : int
        最小交易单位(股), 默认 100。单档上限与各档计划都会被**向下取整到整手**。
    n_slices : int
        执行窗口内的档数, >= 1。
        **占位默认值 10**(示意切分, 无统计依据), 生产接线必须显式传入。

    计算口径
    --------
    `cap_per_slice = floor(cap_pct × adv / lot) × lot`(先按 1e-9 修浮点尾差再取整)
    `daily_cap     = cap_per_slice × n_slices`  —— 即**一个执行窗口**能吃的总量上限
    `scheduled     = min(total_qty // lot × lot, daily_cap)`, 再按**前重后轻**逐手
                     铺到 n_slices 档(与 `schedule_twap` 同一规则)。
    `unscheduled   = total_qty - scheduled`, 由两部分构成:
                      (a) 整手零头 `total_qty % lot`;
                      (b) ADV 太小导致的缺口 `max(0, 整手量 - daily_cap)`。

    Returns
    -------
    dict
        必需字段:
          - `'slices'`      : list[int]  各档股数(整手), `sum(slices) == scheduled`
          - `'n_slices'`    : int        档数(等于入参)
          - `'unscheduled'` : int        吃不掉/无法整手买入的股数
          - `'reason'`      : str        机器可读前缀 + 中文说明:
                                          `'ok'` / `'adv_too_small'` / `'odd_lot'`(可组合, ';' 分隔)
        附加诊断字段(便于审计与接线, 不影响上述契约):
          - `'scheduled_qty'`, `'tradable_qty'`, `'odd_lot'`, `'cap_per_slice'`,
            `'daily_cap'`, `'adv'`, `'cap_pct'`, `'cap_pct_is_placeholder'`, `'lot'`

    **绝不假装全成**: ADV 太小时 `unscheduled > 0` 且 `reason` 以 `'adv_too_small'`
    开头, 明确写出缺口股数与"需跨日/多窗口执行"的事实。

    Raises
    ------
    ValueError
        `total_qty` / `lot` / `n_slices` 非法, `adv <= 0`, 或 `cap_pct` 不在 (0, 1]。
    """
    qty = _require_int(total_qty, "total_qty")
    l = _require_lot(lot)
    n = _require_int(n_slices, "n_slices", allow_zero=False)
    adv_f = _require_positive_float(adv, "adv")
    cap_f = _require_cap_pct(cap_pct)

    tradable, odd_lot = lot_split(qty, l)
    cap_lots = int(math.floor(round(cap_f * adv_f / l, 9)))
    cap_per_slice = cap_lots * l
    daily_cap = cap_per_slice * n
    scheduled = min(tradable, daily_cap)
    slices = [u * l for u in _front_load_lots(scheduled // l, n)]
    unscheduled = int(qty - scheduled)

    parts: list[str] = []
    if cap_per_slice <= 0:
        parts.append(
            "adv_too_small: cap_pct={:.6g} × adv={:.6g} 股 = {:.6g} 股 < 1 手({} 股), "
            "单档上限取整后为 0 股; 该窗口内 0 股可执行, 缺口 {} 股需先解决流动性".format(
                cap_f, adv_f, cap_f * adv_f, l, unscheduled))
    elif tradable > daily_cap:
        parts.append(
            "adv_too_small: 单档上限 {} 股(cap_pct={:.6g} × adv={:.6g} 股, 向下取整到 {} 股整手)"
            " × {} 档 = 窗口上限 {} 股 < 整手后可执行 {} 股; 缺口 {} 股需跨多个窗口/多日执行".format(
                cap_per_slice, cap_f, adv_f, l, n, daily_cap, tradable, unscheduled))
    else:
        parts.append(
            "ok: 单档上限 {} 股 × {} 档 = 窗口上限 {} 股 >= 整手后可执行 {} 股, "
            "本窗口内可完整执行".format(cap_per_slice, n, daily_cap, tradable))
    if odd_lot > 0:
        parts.append(
            "odd_lot: total_qty={} 不是 lot={} 整数倍, 零头 {} 股无法按整手买入, "
            "已计入 unscheduled(卖出零股需另行一次性处理)".format(qty, l, odd_lot))

    return {
        "slices": [int(x) for x in slices],
        "n_slices": int(n),
        "unscheduled": unscheduled,
        "reason": "; ".join(parts),
        "scheduled_qty": int(scheduled),
        "tradable_qty": int(tradable),
        "odd_lot": int(odd_lot),
        "cap_per_slice": int(cap_per_slice),
        "daily_cap": int(daily_cap),
        "adv": float(adv_f),
        "cap_pct": float(cap_f),
        "cap_pct_is_placeholder": bool(float(cap_f) == float(PARTICIPATION_CAP_PCT_PLACEHOLDER)),
        "lot": int(l),
    }


# ==========================================================================
# 4) 统一入口
# ==========================================================================
def plan_execution(total_qty: Any, *, algo: str = "twap",
                   n_slices: Any = DEFAULT_N_SLICES_PLACEHOLDER,
                   volume_profile: Any = None, adv: Any = None,
                   cap_pct: Any = None, lot: Any = 100,
                   side: Any = "buy") -> dict:
    """拆单统一入口: 生成 TWAP / VWAP 计划, 可选叠加参与率上限。

    Parameters
    ----------
    total_qty : int
        母单股数, >= 0。
    algo : str
        **只接受 `'twap'` / `'vwap'`(大小写敏感的精确匹配)**, 其它值(含 'TWAP'、
        'vwap2'、None、'')一律抛 ValueError —— **本模块不做"静默回退到 twap"**,
        因为静默回退会让调用方以为自己拿到的是 VWAP 计划。
    n_slices : int
        档数, >= 1。**占位默认值 10**(示意切分, 无内部依据), 生产必须显式传入。
    volume_profile : Sequence[float] | None
        `algo='vwap'` 时**必填**(长度必须等于 `n_slices`, 否则 ValueError; 本模块
        不会替调用方从行情猜日内量分布, 也不会重采样)。`algo='twap'` 时**必须为
        None** —— 给了就用 ValueError 挡掉, 避免"以为按量分配了其实没分配"。
    adv : float | None
        日均成交量(股), 用于叠加参与率上限。None = 不做参与率约束(计划量不减)。
    cap_pct : float | None
        单档参与率上限, (0, 1]。`adv` 给定时若为 None, 会使用**占位默认值 0.10**
        并在 `notes` 里写一条显式告警(生产接线必须显式传值)。
        `adv=None` 时该参数不生效, 同样会写 `notes` 而不是默默忽略。
    lot : int
        最小交易单位(股), 默认 100(A 股买入整手)。
    side : str
        'buy' / 'sell'。

    参与率上限如何叠加(与 `participation_cap_slices` 的区别)
    ------------------------------------------------------
    本函数**不重排算法形状**, 而是把原计划的各档手数**按比例等比缩量**到窗口上限:
    `target_lots = min(Σlots, floor(daily_cap/lot))`, 再按原各档手数作为权重用
    最大余数法把 `target_lots` 重新摊回各档。这样 TWAP 仍是"均匀偏前"、VWAP 仍是
    "量大的档更多手", 只是整体缩水; 缩不下的部分进 `unscheduled`。
    (要"重排成另一个算法"请直接调 `participation_cap_slices`。)

    Returns
    -------
    dict
        必需字段:
          - `'algo'`            : str        实际使用的算法(必然等于入参, 不回退)
          - `'slices'`          : list[int]  长度 == n_slices, 买入侧每档都是 lot 整数倍
          - `'total_scheduled'` : int        计划总股数 == sum(slices)
          - `'unscheduled'`     : int        未纳入计划的股数(整手零头 + ADV 缺口)
          - `'n_slices'`        : int
          - `'notes'`           : list[str]  设计取舍/占位默认值/零头/缩量的如实记录
        附加字段(接线与审计用): `'side'`, `'lot'`, `'adv'`, `'cap_pct'`, `'cap_pct_used'`,
        `'cap_applied'`, `'volume_profile_len'`。

    Raises
    ------
    ValueError
        非法 `algo` / 非法 `n_slices` / `lot` / `side` / `cap_pct`;
        `algo='vwap'` 缺 `volume_profile` 或长度与 `n_slices` 不符;
        `algo='twap'` 却传了 `volume_profile`; `volume_profile` 本身非法(负值/全 0/NaN 等)。
    """
    qty = _require_int(total_qty, "total_qty")
    n = _require_int(n_slices, "n_slices", allow_zero=False)
    l = _require_lot(lot)
    s = _require_side(side)
    if algo not in ("twap", "vwap"):
        raise ValueError(
            f"algo 只接受 'twap' / 'vwap', 收到 {algo!r}; "
            "本模块不做静默回退(静默回退会掩盖调用方的拼写错误)")
    if algo == "twap" and volume_profile is not None:
        raise ValueError(
            "algo='twap' 不接受 volume_profile: 若不打算按量分配, 请不要传量分布; "
            "需要按量分配请显式传 algo='vwap'(避免'以为按量分配了'的误解)")
    if algo == "vwap" and volume_profile is None:
        raise ValueError(
            "algo='vwap' 必须显式提供 volume_profile(本模块不从行情猜日内成交量曲线)")

    notes: list[str] = []
    tradable, odd_lot = lot_split(qty, l)
    if odd_lot > 0:
        if s == "buy":
            notes.append(
                "零头: total_qty={} 不是 lot={} 整数倍, 有 {} 股零头无法按整手**买入**, "
                "已计入 unscheduled(不静默丢弃)".format(qty, l, odd_lot))
        else:
            notes.append(
                "零头: total_qty={} 不是 lot={} 整数倍, 有 {} 股零头未纳入拆单; "
                "A 股零股须**一次性卖出**, 本模块不把它伪装成'分档执行' —— 请对零头单独"
                "下一笔零股委托, 或显式传 lot=1(承担非整手拆分)".format(qty, l, odd_lot))

    profile_len: int | None = None
    if algo == "twap":
        slices = schedule_twap(qty, n, lot=l, side=s)
        notes.append(
            "TWAP: 均匀拆 {} 档, 余数按'前重后轻'逐手摊到最前面的若干档"
            "(保证各档最多差 1 手 = {} 股; 理由: 避免末档离群 + A 股后段未完成风险更高)".format(n, l))
    else:
        prof = _coerce_profile(volume_profile)
        profile_len = len(prof)
        if profile_len != n:
            raise ValueError(
                "volume_profile 长度 {} 与 n_slices={} 不一致: VWAP 的档数由量分布决定, "
                "请让两者对齐(例如 n_slices=len(volume_profile)); 本模块不做重采样/截断".format(
                    profile_len, n))
        slices = schedule_vwap(qty, prof, lot=l, side=s)
        nz = int(np.count_nonzero(np.asarray(prof, dtype=np.float64) > 0))
        notes.append(
            "VWAP: {} 档量占比已内部归一化; 取整用最大余数法(largest remainder), "
            "保证 sum(slices) 严格等于整手可执行量; 其中 {} 档占比为 0 不分单".format(
                profile_len, profile_len - nz))

    if n > tradable // l:
        notes.append(
            "档数({}) 多于可分手数({} 手): 每档最小 1 手, 故尾部 {} 档为 0(不成交); "
            "不做 lot 以下的小数拆分(买入无法成交 50 股), 时间栅格长度仍保持 {} 档".format(
                n, tradable // l, n - int(tradable // l), n))

    # ---- 参与率上限(等比缩量, 不重排算法形状) ----
    cap_used: float | None = None
    cap_applied = False
    if adv is not None:
        adv_f = _require_positive_float(adv, "adv")
        if cap_pct is None:
            cap_used = float(PARTICIPATION_CAP_PCT_PLACEHOLDER)
            notes.append(
                "参与率上限: cap_pct 未显式传入, 使用**占位默认值 {:.2f}**"
                "(无内部依据, 仅行业常见量级; 生产接线必须显式传 cap_pct)".format(cap_used))
        else:
            cap_used = _require_cap_pct(cap_pct)
        cap_lots = int(math.floor(round(cap_used * adv_f / l, 9)))
        cap_per_slice = cap_lots * l
        daily_cap = cap_per_slice * n
        cur_total = int(sum(slices))
        if daily_cap < cur_total:
            target_lots = daily_cap // l
            lots_now = [int(x) // l for x in slices]
            new_lots = (_largest_remainder_lots(lots_now, target_lots)
                        if target_lots > 0 else [0] * len(lots_now))
            slices = [u * l for u in new_lots]
            cap_applied = True
            notes.append(
                "参与率上限触发: 单档上限 {} 股(cap_pct={:.6g} × adv={:.6g} 股, 向下取整到 {} 股整手)"
                " × {} 档 = 窗口上限 {} 股 < 原计划 {} 股; 按原各档手数等比缩量到 {} 股"
                "(最大余数法, 保持算法形状), 缩不下的 {} 股进 unscheduled".format(
                    cap_per_slice, cap_used, adv_f, l, n, daily_cap, cur_total,
                    int(sum(slices)), qty - int(sum(slices))))
        else:
            notes.append(
                "参与率上限未触发: 单档上限 {} 股 × {} 档 = 窗口上限 {} 股 >= 计划量 {} 股, "
                "计划量未减".format(cap_per_slice, n, daily_cap, cur_total))
    elif cap_pct is not None:
        notes.append(
            "cap_pct={!r} 已给出但 adv=None, 参与率上限**未生效**(没有 ADV 就算不出单档上限); "
            "要启用请同时传 adv(单位: 股)".format(cap_pct))

    total_scheduled = int(sum(slices))
    return {
        "algo": algo,
        "slices": [int(x) for x in slices],
        "total_scheduled": total_scheduled,
        "unscheduled": int(qty - total_scheduled),
        "n_slices": int(n),
        "notes": notes,
        # ---- 以下为附加字段(便于 simulate_execution 与接线审计) ----
        "side": s,
        "lot": int(l),
        "adv": (float(adv) if adv is not None else None),
        "cap_pct": cap_used,
        "cap_pct_used": cap_used,
        "cap_applied": bool(cap_applied),
        "volume_profile_len": profile_len,
    }


# ==========================================================================
# 5) 滑点仿真: 拆单 vs 一次性下单
# ==========================================================================
def naive_slippage_bps(order_size: Any, avg_daily_volume: Any,
                       volatility: Any) -> float:
    """"一次性下单"基准的滑点(bps), 直接复用 `slippage_model.almglen_chriss_slippage`.

    Parameters
    ----------
    order_size : float
        订单金额(元) = 股数 × 价格。
    avg_daily_volume : float
        **日均成交额(元)**(与 `slippage_model` 口径一致, 不是股数)。
    volatility : float
        日频波动率(小数)。

    Returns
    -------
    float
        滑点(bps, 1bp = 万分之一)。公式完全来自 `slippage_model`, 本模块不新造公式。
    """
    amt = _require_positive_float(order_size, "order_size")
    adv = _require_positive_float(avg_daily_volume, "avg_daily_volume")
    vol = _require_positive_float(volatility, "volatility")
    return float(round(almglen_chriss_slippage(amt, adv, vol) * 1e4, 2))


def _coerce_prices(prices: Any) -> list[float]:
    """把各档参考价规整为长度>0 的有限正数序列, 否则 ValueError."""
    try:
        arr = np.asarray(prices, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"prices 必须是一维正数序列: {exc}") from exc
    if arr.ndim != 1 or arr.size == 0:
        raise ValueError(f"prices 必须是非空一维序列(各档参考价), 收到 shape={arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("prices 含 NaN/Inf")
    if np.any(arr <= 0):
        raise ValueError("prices 必须为正(参考价)")
    return [float(x) for x in arr]


def simulate_execution(plan: dict, prices: Any, *, avg_daily_volume: Any,
                       volatility: Any, participation_per_slice: Any = None,
                       window_horizon_days: Any = None,
                       urgency_kappa: Any = URGENCY_KAPPA_PLACEHOLDER) -> dict:
    """仿真一个拆单方案的滑点, 并与"一次性下单"基准对比(**含可为负的 saving_bps**).

    Parameters
    ----------
    plan : dict
        `plan_execution()` 的返回值(至少要有 `'slices'`; 有 `'side'` / `'cap_pct'` 更准)。
    prices : Sequence[float]
        该标的**各档参考价**(通常取各档的到达价/时段 VWAP 价)。长度可以
        **等于**或**短于**档数; 短于时**用最后一档价格外推**(即假设参考价在剩余
        窗口内不再变化 —— 这是一个假设, 会原样写进 `assumptions`); 长于档数时
        只取前 n 档, 并在 `assumptions` 里写明"多余价格被忽略"。
        空序列/含非正数/含 NaN 一律 ValueError。
    avg_daily_volume : float
        **日均成交额(元)**, 与 `slippage_model` 口径一致(不是股数)。
    volatility : float
        日频波动率(小数), > 0。
    participation_per_slice : float | None
        单档目标参与率(占 ADV 金额比例, (0, 1]), 用于在未给 `window_horizon_days` 时
        反推**整个拆单窗口**的总时长:
        `窗口时长(交易日) = 总委托金额 / (ADV × participation_per_slice)`。
        优先级: 显式传入 > `plan['cap_pct']`(若计划里记了参与率上限) > **占位默认值
        `PARTICIPATION_CAP_PCT_PLACEHOLDER`(0.10, 无内部依据)**。用到占位值时会在
        `assumptions` 里显式记录, 生产接线必须显式传入。
    window_horizon_days : float | None
        **整个拆单窗口**的总执行时长(交易日): 单档 horizon = `window_horizon_days / 档数`。
        显式给出时优先于上面的参与率反推(便于做"时间风险主导"的压力场景)。
        None = 按参与率反推。> 252 天会被钳到 252(与 `execution_risk_rate` 的内部
        clip 一致, 会在 `assumptions` 里写明被钳)。
    urgency_kappa : float
        执行紧迫度 κ, 默认 **占位/沿用值 1.0**(取自 `slippage_model` 自身的默认值),
        生产接线应显式传入。

    成本口径(先定义清楚, 避免误读)
    -----------------------------
    - **单档**: `order_amount_i = qty_i × price_i`, 走
      `decompose_slippage(order_amount_i, avg_daily_volume, volatility,
      execution_horizon_days=单档 horizon, urgency_kappa=κ)`; 该档滑点取返回的
      `total_bps`(市场冲击 + 执行风险)。
    - **拆单总滑点** `slippage_bps_sliced`: 各档滑点按**成交量加权**平均
      (不是按档数简单平均 —— 空档不成交, 简单平均会低估成本)。
    - **基准** `slippage_bps_naive`: 同一批股数**一次性下单**, 执行时长 0 天
      (纯市场冲击, 无时间风险), 参考价取 `prices[0]`(到达价)。
    - `saving_bps = slippage_bps_naive - slippage_bps_sliced`, **不做 max(0, ·)**:
      拆单用"分散冲击"换"时间风险", 当执行风险分量主导时它就是**负数**。
    - **刻意分离**: `slice_vwap_price` / `naive_price` 是含**各档参考价自身路径
      (市场漂移)**的成交价, 而 `saving_bps` 只含成本模型口径的滑点差。因此
      `(naive_price - slice_vwap_price)` 与 `saving_bps` **不必然同号**, 不要互相校验。
    - 模型边界: 复用的 AC 模型是"线性永久冲击 + 临时冲击"且 `max_slippage=0.02`
      (200bps)封顶, 故 naive 基准在大单下会顶到 200bps, `saving_bps` 的上界被该封顶钳制。

    Returns
    -------
    dict
        - `'slice_vwap_price'`     : float 各档成交量加权的成交价(按 side 方向施加滑点)
        - `'naive_price'`          : float 一次性下单(到达价 prices[0])的成交价
        - `'slippage_bps_sliced'`  : float 拆单的成交量加权滑点(bps)
        - `'slippage_bps_naive'`   : float 一次性下单滑点(bps)
        - `'saving_bps'`           : float `naive - sliced`, **可为负**
        - `'per_slice'`            : list[dict] 每档明细(qty / order_amount / participation /
                                     horizon_days / market_impact_bps / execution_risk_bps /
                                     total_bps / exec_price / idle)
        - `'assumptions'`          : list[str] 全部假设与口径说明(价格外推、占位默认值、
                                     horizon 来源、基准定义、价格与滑点分离等)
        附加字段: `'naive_ac_crosscheck_bps'`(同一基准改用 `almglen_chriss_slippage()`
        复算, 与 `slippage_bps_naive` 应一致, 供接线方自校验复用关系)、
        `'total_qty'`, `'n_slices'`, `'side'`, `'window_horizon_days'`,
        `'per_slice_horizon_days'`, `'participation_used'`, `'participation_source'`。

    Raises
    ------
    ValueError
        `plan` 不是含 `'slices'` 的 dict、计划总量为 0、`prices` 非法、
        `avg_daily_volume <= 0`、`volatility <= 0`、`participation_per_slice` 不在 (0, 1]、
        `window_horizon_days < 0`、`urgency_kappa <= 0`。
    """
    if not isinstance(plan, dict) or "slices" not in plan:
        raise ValueError("plan 必须是 plan_execution() 的返回 dict(至少含 'slices')")
    try:
        raw_slices = list(plan["slices"])
    except TypeError as exc:
        raise ValueError(f"plan['slices'] 必须是可迭代的股数序列: {exc}") from exc
    slices = [_require_int(x, "plan['slices'][i]") for x in raw_slices]
    if not slices:
        raise ValueError("plan['slices'] 不能为空")

    adv = _require_positive_float(avg_daily_volume, "avg_daily_volume")
    vol = _require_positive_float(volatility, "volatility")
    kappa = _require_positive_float(urgency_kappa, "urgency_kappa")
    px = _coerce_prices(prices)

    assumptions: list[str] = []
    side = plan.get("side", "buy")
    if "side" not in plan:
        assumptions.append(
            "plan 未携带 'side', 按 'buy' 处理(成交价方向按买入施加滑点); "
            "注意 saving_bps 与 side 无关(只比滑点幅度), 受影响的是两个价格字段")
    side = _require_side(side)
    sign = 1.0 if side == "buy" else -1.0

    n = len(slices)
    total_qty = int(sum(slices))
    if total_qty <= 0:
        raise ValueError(
            "计划的可执行总量为 0(全部是零头/被 ADV 上限砍光), 没有可仿真的成交; "
            "请先解决 unscheduled 部分再仿真")

    if len(px) < n:
        assumptions.append(
            "价格外推: prices 长度 {} < 档数 {}, 第 {} 档起用最后一档价格 {:.6f} 外推"
            "(假设: 参考价在窗口剩余部分不再变化, 即忽略价格漂移)".format(
                len(px), n, len(px), px[-1]))
    elif len(px) > n:
        assumptions.append(
            "价格截断: prices 长度 {} > 档数 {}, 仅使用前 {} 个价格, 其余被忽略".format(
                len(px), n, n))

    # ---- 窗口总时长 / 单档 horizon 的来源 ----
    total_amount = float(sum(q * (px[i] if i < len(px) else px[-1]) for i, q in enumerate(slices)))
    if window_horizon_days is not None:
        wh = float(window_horizon_days)
        if not math.isfinite(wh) or wh < 0:
            raise ValueError(f"window_horizon_days 必须 >= 0, 收到 {window_horizon_days!r}")
        horizon_total = wh
        part_used = None
        part_source = "显式传入 window_horizon_days={:.6g} 天(整个窗口)".format(wh)
    else:
        if participation_per_slice is not None:
            part_used = _require_cap_pct(participation_per_slice)
            part_source = "显式传入 participation_per_slice={:.6g}".format(part_used)
        elif isinstance(plan.get("cap_pct"), (int, float)) and float(plan["cap_pct"]) > 0:
            part_used = _require_cap_pct(plan["cap_pct"])
            part_source = "沿用 plan['cap_pct']={:.6g}".format(part_used)
        else:
            part_used = float(PARTICIPATION_CAP_PCT_PLACEHOLDER)
            part_source = ("**占位默认值** participation_per_slice={:.2f}"
                           "(无内部依据; 生产接线必须显式传入)").format(part_used)
        horizon_total = total_amount / (adv * part_used)
        if horizon_total > 252.0:
            assumptions.append(
                "窗口时长按参与率反推得 {:.4g} 天 > 252 天, 已钳到 252 天"
                "(与 slippage_model.execution_risk_rate 的内部 clip 一致)".format(horizon_total))
            horizon_total = 252.0
        part_source = ("窗口总时长 = 总委托金额 {:.6g} 元 / (ADV {:.6g} 元 × 单档参与率 {:.6g}) "
                       "= {:.6g} 天; 参与率来源: {}").format(
            total_amount, adv, part_used, horizon_total, part_source)
    per_slice_horizon = horizon_total / n

    # ---- 逐档滑点(完全走 slippage_model.decompose_slippage) ----
    per_slice: list[dict] = []
    weighted_price_num = 0.0
    weighted_qty = 0
    weighted_slip_bps = 0.0
    for i, q in enumerate(slices):
        ref = px[i] if i < len(px) else px[-1]
        if q <= 0:
            per_slice.append({
                "i": i, "qty": 0, "ref_price": ref, "order_amount": 0.0,
                "participation": 0.0, "horizon_days": 0.0,
                "market_impact_bps": 0.0, "execution_risk_bps": 0.0, "total_bps": 0.0,
                "exec_price": ref, "idle": True,
            })
            continue
        amt = q * ref
        dec = decompose_slippage(
            amt, adv, vol,
            execution_horizon_days=per_slice_horizon, urgency_kappa=kappa)
        exec_price = ref * (1.0 + sign * dec["total_rate"])
        per_slice.append({
            "i": i, "qty": int(q), "ref_price": ref, "order_amount": float(amt),
            "participation": float(amt / adv), "horizon_days": float(per_slice_horizon),
            "market_impact_bps": float(dec["market_impact_bps"]),
            "execution_risk_bps": float(dec["execution_risk_bps"]),
            "total_bps": float(dec["total_bps"]),
            "exec_price": float(exec_price), "idle": False,
        })
        weighted_price_num += q * exec_price
        weighted_qty += q
        weighted_slip_bps += q * float(dec["total_bps"])

    slice_vwap_price = weighted_price_num / weighted_qty
    slippage_bps_sliced = round(weighted_slip_bps / weighted_qty, 2)

    # ---- 基准: 同一批股数一次性下单(执行时长 0, 到达价) ----
    naive_amount = total_qty * px[0]
    naive_dec = decompose_slippage(
        naive_amount, adv, vol, execution_horizon_days=0.0, urgency_kappa=kappa)
    slippage_bps_naive = float(naive_dec["total_bps"])
    naive_price = px[0] * (1.0 + sign * naive_dec["total_rate"])
    saving_bps = round(slippage_bps_naive - slippage_bps_sliced, 2)

    assumptions.extend([
        "基准(naive): 同一批股数 {} 股一次性下单, 用首档参考价(到达价){:.6f} 成交, "
        "执行时长 0 天 → 滑点 = 纯市场冲击 {:.2f} bps(无时间风险)".format(
            total_qty, px[0], slippage_bps_naive),
        "拆单口径: 单档 horizon = 窗口总时长 {:.6g} 天 / {} 档 = {:.6g} 天; "
        "滑点 = 市场冲击 + 执行风险(全部由 slippage_model.decompose_slippage 给出, "
        "本模块不新造成本公式)".format(horizon_total, n, per_slice_horizon),
        part_source if window_horizon_days is not None else "窗口时长来源: {}".format(part_source),
        "总滑点口径: slippage_bps_sliced 是各档滑点率按**成交量加权**平均(空档不计入); "
        "saving_bps = slippage_bps_naive - slippage_bps_sliced, **不做 max(0, ·)**: "
        "执行风险分量主导时它就是负值(拆单更贵)",
        "成本 vs 价格分离: saving_bps 只含成本模型口径的滑点差; slice_vwap_price/naive_price "
        "额外包含各档参考价自身的路径(市场漂移), 故两者不必然同号, 不要互相校验",
        "方向: side={!r} → 买入成交价 = 参考价 × (1 + 滑点率), 卖出 = 参考价 × (1 - 滑点率); "
        "但 saving_bps 只比较滑点**幅度**, 与 side 无关".format(side),
        "模型边界: 复用的 AC 模型为线性永久冲击 + 临时冲击, 且单边滑点封顶 0.02(200 bps); "
        "大单下 naive 会顶到 200 bps, 故 saving_bps 的上界被该封顶钳制",
    ])

    return {
        "slice_vwap_price": float(round(slice_vwap_price, 6)),
        "naive_price": float(round(naive_price, 6)),
        "slippage_bps_sliced": float(slippage_bps_sliced),
        "slippage_bps_naive": slippage_bps_naive,
        "saving_bps": float(saving_bps),
        "per_slice": per_slice,
        "assumptions": assumptions,
        # ---- 附加字段 ----
        "naive_ac_crosscheck_bps": naive_slippage_bps(naive_amount, adv, vol),
        "total_qty": total_qty,
        "n_slices": n,
        "side": side,
        "window_horizon_days": float(horizon_total),
        "per_slice_horizon_days": float(per_slice_horizon),
        "participation_used": (float(part_used) if part_used is not None else None),
        "participation_source": part_source,
    }


# ==========================================================================
# 6) CLI: 人工核对用的自检打印(纯 print, 不读写文件/不联网)
# ==========================================================================
def _demo_volume_profile(n_slices: int) -> list[float]:
    """生成一个**示意用**的 U 型日内量占比(开盘/尾盘双高), 供 CLI 自检打印。

    用公式而不是硬编码数字: `w_k = 1 + 2 × u_k²`, 其中 `u_k` 是第 k 档在
    [-1, 1] 上的归一化位置 —— 这是**示意形状**, 不是从任何行情统计出来的参数;
    生产接线必须换成真实统计的日内量分布(或直接不传 volume_profile)。
    """
    if n_slices <= 1:
        return [1.0]
    half = (n_slices - 1) / 2.0
    return [1.0 + 2.0 * ((k - half) / half) ** 2 for k in range(n_slices)]


def _print_plan_table(title: str, plan: dict, sim: dict) -> None:
    """把一个 plan + sim 打成人工可核对的表格(纯 stdout)."""
    print(f"== {title} ==")
    print(f"algo={plan['algo']}  n_slices={plan['n_slices']}  side={plan['side']}  lot={plan['lot']}")
    print(f"计划总量={plan['total_scheduled']} 股   未纳入计划(unscheduled)={plan['unscheduled']} 股")
    print("+------+----------+------------+----------+------------+----------+")
    print("| 档 i | 股数     | 档内占比   | 参考价   | 该档滑点   | 成交价   |")
    print("+------+----------+------------+----------+------------+----------+")
    tot = max(plan["total_scheduled"], 1)
    for row in sim["per_slice"]:
        pct = 100.0 * row["qty"] / tot
        tag = " (空档)" if row["idle"] else ""
        print("| {:>4} | {:>8} | {:>9.2f}% | {:>8.4f} | {:>8.2f}bp | {:>8.4f} |{}".format(
            row["i"], row["qty"], pct, row["ref_price"], row["total_bps"],
            row["exec_price"], tag))
    print("+------+----------+------------+----------+------------+----------+")
    print("滑点对比: 一次性下单 {:.2f} bps  vs  拆单 {:.2f} bps  →  saving_bps = {:+.2f}".format(
        sim["slippage_bps_naive"], sim["slippage_bps_sliced"], sim["saving_bps"]))
    print("成交价  : 一次性下单 {:.4f}  vs  拆单成交量加权 {:.4f}".format(
        sim["naive_price"], sim["slice_vwap_price"]))
    if sim["saving_bps"] < 0:
        print("注意: saving_bps 为**负** —— 该参数下拆单比一次性下单更贵(执行风险分量主导), "
              "本模块不粉饰该结果.")
    for nt in plan["notes"]:
        print(f"  note: {nt}")
    print()


def _main(argv: list[str] | None = None) -> int:
    """CLI 入口: `--selftest` 打印一个 TWAP 与一个 VWAP 的拆单表 + 滑点对比。

    设计取舍:
      - **只 print**: 不读写数据文件、不访问网络、不调用任何下单通道, 因此可以
        在离线环境随手跑, 用于人工核对拆单表与滑点对比是否符合直觉。
      - `--selftest` 会**同时**跑 TWAP 与 VWAP(便于并排比较), 且 VWAP 用
        `_demo_volume_profile()` 生成的 U 型示意量分布(不是行情统计值);
        只跑单个算法用 `--algo twap|vwap`。
      - CLI 里出现的默认参数(档数 10、参与率上限 0.10、样例波动率/价格/ADV)
        全是**占位/示意值**, 打印时会同时提示"生产接线必须由调用方显式传入真实参数"。
      - 参与率上限的单位换算在 CLI 里显式做: `adv`(元) 与 `adv_shares`(股) 分开传,
        因为 `participation_cap_slices()` 吃的是**股数**而 `simulate_execution()`
        吃的是**成交额(元)** —— 这个口径差异是最容易接错的地方, 故在 CLI 里暴露出来。

    返回进程退出码: 0 = 正常; 2 = 没给动作(只打印用法)。参数非法时由 argparse
    抛 SystemExit(2)。
    """
    parser = argparse.ArgumentParser(
        prog="exec_algo",
        description="TWAP/VWAP 离线拆单规划 + Almgren-Chriss 滑点仿真(不接实盘下单通道)",
    )
    parser.add_argument("--selftest", action="store_true",
                        help="打印一个 TWAP 与一个 VWAP 拆单表 + 滑点对比(人工核对用)")
    parser.add_argument("--algo", choices=("twap", "vwap"), default=None,
                        help="不跑 selftest 时只打印指定算法的计划")
    parser.add_argument("--qty", type=int, default=120000,
                        help="母单股数(默认 120000, 仅自检示意值)")
    parser.add_argument("--slices", type=int, default=DEFAULT_N_SLICES_PLACEHOLDER,
                        help="档数(默认 10 = 占位示意值, 生产必须显式传入)")
    parser.add_argument("--lot", type=int, default=100, help="最小交易单位(股)")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--price", type=float, default=10.0,
                        help="首档参考价(**自检示意值**, 无依据; 生产接线自行传入)")
    parser.add_argument("--adv", type=float, default=5e6,
                        help="日均成交额(元), 与 slippage_model 口径一致(不是股数); "
                             "默认 5e6 为**自检示意值**")
    parser.add_argument("--vol", type=float, default=0.03,
                        help="日频波动率(小数); 默认 0.03 为**自检示意值**, 无依据")
    parser.add_argument("--cap-pct", type=float, default=None,
                        help="单档参与率上限(不传则在给 adv 时使用占位默认值 0.10)")
    parser.add_argument("--adv-shares", type=float, default=None,
                        help="日均成交量(股), 用于参与率上限对比; 不传则由 --adv/--price 换算")
    args = parser.parse_args(argv)

    if not args.selftest and args.algo is None:
        parser.print_help()
        return 2

    n = int(args.slices)
    adv_yuan = float(args.adv)
    adv_shares = (float(args.adv_shares) if args.adv_shares is not None
                  else adv_yuan / float(args.price))

    algos = ["twap", "vwap"] if args.selftest else [args.algo]
    for algo in algos:
        plan = plan_execution(
            args.qty, algo=algo, n_slices=n,
            volume_profile=(_demo_volume_profile(n) if algo == "vwap" else None),
            adv=adv_shares, cap_pct=args.cap_pct, lot=args.lot, side=args.side,
        )
        sim = simulate_execution(
            plan, [float(args.price)] * n,
            avg_daily_volume=adv_yuan, volatility=float(args.vol),
            participation_per_slice=0.10,
        )
        title = "TWAP 拆单表" if algo == "twap" else "VWAP 拆单表(U 型量分布, 示意)"
        _print_plan_table(title, plan, sim)

    print("提示: 以上为**人工核对用**的示意输入; 参与率上限 0.10、档数 10、示例量分布与")
    print("      示例波动率都是占位/示意值, 生产接线必须由调用方显式传入真实参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

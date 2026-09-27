# -*- coding: utf-8 -*-
"""两方案对比: **截尾 q%** vs **降 D10 权重**, 在可得口径上做同一批窗口。

## 口径可得性(先说清, 不假装)

用户要"120 天窗口(历史验证口径) + 5-7 天窗口(当前实现口径)"两条都跑。
**120 天这条跑不了**: 融合分最新只到 **2026-09-11**, 而 h5i 止于 **2026-09-24**
=> 120 交易日前向需要到 2027-03 的数据。本脚本**显式断言并报告**这一点,
然后在**可得的 1/3/5/7 天**口径上做对比 —— 并说明这与历史 120 天结论的可比性边界。

## 两方案(按 `selector.trim_top` 的真实语义)

· **截尾 q%**: 把融合分最高的 q% 置为最低档(即"跳过这些极端头部"),
  新的 top10 顺延到次高分的一批 —— 这正是生产 `FUSION_TRIM_Q` 的行为;
· **降 D10 权重**: 把 D10 档的分整体乘一个系数(0.5 / 0.0)。
  注意 `D10 权重=0` 与"截尾 10%"**并不等价**: 前者按档均匀降, 后者只针对最尖。

对比口径: 两方案各自选出的 **top10 的前向收益**(与"实际持仓=等权10只"同量级)。
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402
import h5i_bar_store as S  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_trim_vs_d10.txt")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fusion_scores.json")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st = S.open_store()
ALL = st.trading_days()
_px: dict[str, dict] = {}


def closes(day: str) -> dict:
    if day not in _px:
        df = st.bars_on_day(day)
        _px[day] = {str(r.symbol): float(r.close) for r in df.itertuples(index=False)
                    if r.close is not None}
    return _px[day]


cache = json.load(open(CACHE, encoding="utf-8"))
DAYS = sorted(d for d in cache if (cache[d] or {}).get("scores"))
LAST = ALL[-1]

# ---------------------------------------------------------------- 口径断言
p("=" * 84)
p("口径可得性(先断言, 再算)")
p("=" * 84)
p(f"  融合分最新决策日: {DAYS[-1]}")
p(f"  h5i 末交易日     : {LAST}")
try:
    i = ALL.index(DAYS[-1])
    avail = len(ALL) - 1 - i
except ValueError:
    avail = -1
p(f"  从最新决策日到 h5i 末端可用前向交易日: **{avail}** 天")
missing120 = 120 - avail
p(f"  120 交易日前向**不可得** —— 还差 {missing120} 个交易日(约需到 2027-03)。")
p("  => **历史 120 天口径本次无法复现**, 下面只在可得的 1/3/5/7 天上对比。")
p("     这个边界很重要: 历史的 `+10.76%`(截尾5%) 与本次短期限的数**不可直接比大小**,")
p("     只能比**方向**(截尾是否改善)。")
p()
HORIZONS = [h for h in (1, 3, 5, 7) if h <= avail]
p(f"  实际使用期限: {HORIZONS}")
p()
assert HORIZONS, "连 1 天前向都没有 —— 不要在无数据时出结论"


def fwd(day: str, h: int):
    i = ALL.index(day)
    return ALL[i + h] if i + h < len(ALL) else None


def rets(day: str, d1: str) -> dict:
    m0, m1 = closes(day), closes(d1)
    out = {}
    for s, a in m0.items():
        b = m1.get(s)
        if a and b and a > 0:
            r = b / a - 1.0
            if -0.9 < r < 5.0:
                out[s] = r
    return out


# ---------------------------------------------------------------- 三种选择器
def pick_raw(sc: dict, k: int = 10):
    return [s for s, _ in sorted(sc.items(), key=lambda kv: -kv[1])[:k]]


def pick_trim(sc: dict, q: float, k: int = 10):
    """与 selector.trim_top 同语义: 高分 q 分位以上置最低档。"""
    if q <= 0:
        return pick_raw(sc, k)
    vals = np.array(list(sc.values()), dtype=float)
    fin = vals[np.isfinite(vals)]
    if fin.size < 20:
        return pick_raw(sc, k)
    cut = float(np.quantile(fin, 1.0 - q))
    adj = {s: (float("-inf") if v > cut else v) for s, v in sc.items()}
    return [s for s, _ in sorted(adj.items(), key=lambda kv: -kv[1])[:k]]


def pick_d10_reduced(sc: dict, coef: float, k: int = 10):
    """把 D10 档的分乘 coef(档内相对次序保留)。"""
    ks = sorted(sc.items(), key=lambda kv: kv[1])
    n = len(ks)
    dec = {}
    for idx, (s, _) in enumerate(ks):
        dec[s] = min(10, idx * 10 // n + 1)
    adj = {s: (v * coef if dec[s] == 10 else v) for s, v in sc.items()}
    return [s for s, _ in sorted(adj.items(), key=lambda kv: -kv[1])[:k]]


SCHEMES = [
    ("原始 top10(=实际持仓口径)", lambda sc: pick_raw(sc)),
    ("截尾 3%", lambda sc: pick_trim(sc, 0.03)),
    ("截尾 5%", lambda sc: pick_trim(sc, 0.05)),
    ("截尾 8%", lambda sc: pick_trim(sc, 0.08)),
    ("截尾 10%", lambda sc: pick_trim(sc, 0.10)),
    ("D10 权重 x0.5", lambda sc: pick_d10_reduced(sc, 0.5)),
    ("D10 权重 x0.0", lambda sc: pick_d10_reduced(sc, 0.0)),
]

# ---------------------------------------------------------------- 主表
p("=" * 84)
p("主表: 各方案选出的 top10 前向收益(均值 %, 14 个决策日)")
p("=" * 84)
rows = {nm: {h: [] for h in HORIZONS} for nm, _ in SCHEMES}
overlap_raw = {nm: [] for nm, _ in SCHEMES}
for day in DAYS:
    sc = cache[day]["scores"]
    if len(sc) < 500:
        continue
    base = set(pick_raw(sc))
    for nm, fn in SCHEMES:
        sel = fn(sc)
        overlap_raw[nm].append(len(set(sel) & base))
        for h in HORIZONS:
            d1 = fwd(day, h)
            if not d1:
                continue
            rr = rets(day, d1)
            v = [rr[s] for s in sel if s in rr]
            if v:
                rows[nm][h].append(sum(v) / len(v) * 100)

p(f"  {'方案':28}" + "".join(f"{'h'+str(h):>10}" for h in HORIZONS) + f"{'与原始重叠':>11}")
for nm, _ in SCHEMES:
    cells = []
    for h in HORIZONS:
        vals = rows[nm][h]
        cells.append(f"{statistics.mean(vals):>+9.3f}%" if vals else f"{'-':>10}")
    ov = statistics.mean(overlap_raw[nm]) if overlap_raw[nm] else float("nan")
    p(f"  {nm:28}" + "".join(cells) + f"{ov:>10.1f}/10")
p()

p("  差值(相对原始 top10), 正值 = 改善:")
p(f"  {'方案':28}" + "".join(f"{'h'+str(h):>10}" for h in HORIZONS))
for nm, _ in SCHEMES:
    if nm.startswith("原始"):
        continue
    cells = []
    for h in HORIZONS:
        if rows[nm][h] and rows["原始 top10(=实际持仓口径)"][h]:
            d = statistics.mean(rows[nm][h]) - statistics.mean(rows["原始 top10(=实际持仓口径)"][h])
            cells.append(f"{d:>+9.3f}pp")
        else:
            cells.append(f"{'-':>10}")
    p(f"  {nm:28}" + "".join(cells))
p()

p("  正窗口占比:")
p(f"  {'方案':28}" + "".join(f"{'h'+str(h):>10}" for h in HORIZONS))
for nm, _ in SCHEMES:
    cells = []
    for h in HORIZONS:
        vals = rows[nm][h]
        cells.append(f"{sum(1 for v in vals if v>0):>4}/{len(vals):<5}" if vals else f"{'-':>10}")
    p(f"  {nm:28}" + "".join(cells))
p()

# ---------------------------------------------------------------- 判定
p("=" * 84)
p("判定")
p("=" * 84)
trim5 = {h: (statistics.mean(rows["截尾 5%"][h]) if rows["截尾 5%"][h] else None)
         for h in HORIZONS}
d10z = {h: (statistics.mean(rows["D10 权重 x0.0"][h]) if rows["D10 权重 x0.0"][h] else None)
        for h in HORIZONS}
base = {h: (statistics.mean(rows["原始 top10(=实际持仓口径)"][h])
            if rows["原始 top10(=实际持仓口径)"][h] else None) for h in HORIZONS}
raw_n = {h: len(rows["原始 top10(=实际持仓口径)"][h]) for h in HORIZONS}

p(f"  {'期限':>5}{'原始':>11}{'截尾5%':>11}{'降D10':>11}{'截尾-降D10':>13}{'n':>5}")
for h in HORIZONS:
    if base[h] is None:
        continue
    diff = (trim5[h] - d10z[h]) if (trim5[h] is not None and d10z[h] is not None) else float("nan")
    p(f"  {h:>5}{base[h]:>+10.3f}%{(trim5[h] if trim5[h] is not None else float('nan')):>+10.3f}%"
      f"{(d10z[h] if d10z[h] is not None else float('nan')):>+10.3f}%{diff:>+12.3f}pp{raw_n[h]:>5}")
p()
p("  判据(用户给定): 截尾显著优于降 D10 => 确认「极端尾」是问题;")
p("                  两者接近 => 需要更细致的分档。")
p()
trim_better = [h for h in HORIZONS
               if trim5[h] is not None and d10z[h] is not None and trim5[h] > d10z[h] + 0.05]
near = [h for h in HORIZONS
        if trim5[h] is not None and d10z[h] is not None and abs(trim5[h] - d10z[h]) <= 0.05]
p(f"  截尾明显更优的期限: {trim_better}")
p(f"  两者接近(<=0.05pp)的期限: {near}")
p()
p("  ⚠️ 样本边界: 仅 14 个决策日、且共用一个月的 OOS;")
p("     上述差值**不足以直接改默认值**, 只能作方向性判据。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

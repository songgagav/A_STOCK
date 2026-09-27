# -*- coding: utf-8 -*-
"""同源核对 + 期限结构 + 极端尾 +(beta/动量)中性化。

## 要区分的两件事(历史文档给的关键区别)

`docs/pit-valuation.md` §16② (2026-09-13) 记:

> 十分档 D1..D10 = +0.42 / -0.29 / +1.86 / +4.21 / +3.42 / +4.03 / +5.61 / **+7.49** /
> +5.01 / +5.05 %(**最高十分档并未反转**, D8 最强), 说明反转只发生在
> **池内最尖的 top10(约前 0.5%)**

即历史上: **D10 档没反转, 只有最尖的 top10(≈前 0.5%)反转**, 且那次用的是
**前向 120 交易日**。而我上一批用的是**前向 10 交易日**, 得到 D10 = -0.74%。

=> 必须先排除两个**口径差**, 才能判断"同源":
  1. **期限**: 120 天 vs 10 天(docs 自己提示"信号可能短周期有效、到 120 天已衰减/反转");
  2. **尾的锐度**: D10 档(≈500 只) vs top10(≈25 只, 前 0.5%)。

## 本脚本做三件事

A) **期限结构**: 同一批窗口, 前向 1/3/5/7 交易日, 看 D10 与"最尖 25 只"的收益怎么变;
B) **极端尾**: 单列 前 25 / 前 10 只(≈前 0.5% / 0.2%), 与 D10 档对照;
C) **中性化**: 对 D10 与最尖尾分别剥离 **beta**(对等权市场)与 **动量**(20 日涨跌),
   看残差后的收益 —— 回答"负收益是不是顺周期暴露造成的"。

h5i 数据止于 2026-09-24, 故前向最多 7 个交易日(决策日 <= 09-17)。
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402
import h5i_bar_store as S  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sameness.txt")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fusion_scores.json")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st = S.open_store()
ALL = st.trading_days()
LAST = ALL[-1]
_px: dict[str, dict] = {}


def closes(day: str) -> dict:
    if day not in _px:
        df = st.bars_on_day(day)
        d = {}
        for r in df.itertuples(index=False):
            try:
                d[str(r.symbol)] = float(r.close)
            except Exception:  # noqa: BLE001
                continue
        _px[day] = d
    return _px[day]


cache = json.load(open(CACHE, encoding="utf-8"))
DAYS = sorted(d for d in cache if (cache[d] or {}).get("scores"))
p(f"h5i 末交易日: {LAST}")
p(f"有融合分的决策日: {len(DAYS)}  ({DAYS[0]} .. {DAYS[-1]})")
p()

HORIZONS = (1, 3, 5, 7)


def fwd(day: str, h: int) -> str | None:
    try:
        i = ALL.index(day)
    except ValueError:
        return None
    return ALL[i + h] if i + h < len(ALL) else None


def rets_on(day: str, d1: str) -> dict:
    m0, m1 = closes(day), closes(d1)
    out = {}
    for s, a in m0.items():
        b = m1.get(s)
        if a and b and a > 0:
            r = b / a - 1.0
            if -0.9 < r < 5.0:
                out[s] = r
    return out


def decile_of(scores: dict, n_dec: int = 10) -> dict:
    """返回 {symbol: 档号 1..10}(按分升序分档)。"""
    ks = sorted(scores.items(), key=lambda kv: kv[1])
    n = len(ks)
    out = {}
    for k, (s, _) in enumerate(ks):
        out[s] = min(n_dec, k * n_dec // n + 1)
    return out


# ============================================================ A/B 期限结构 + 极端尾
p("=" * 84)
p("A/B 期限结构 + 极端尾(每档/每组按当日全市场分排序)")
p("=" * 84)
res = {h: {"D10": [], "D9": [], "D8": [], "top25": [], "top10": [], "all": []}
       for h in HORIZONS}
used = {h: 0 for h in HORIZONS}
for day in DAYS:
    sc = cache[day]["scores"]
    if len(sc) < 500:
        continue
    dec = decile_of(sc)
    ranked = [s for s, _ in sorted(sc.items(), key=lambda kv: -kv[1])]
    for h in HORIZONS:
        d1 = fwd(day, h)
        if not d1:
            continue
        rr = rets_on(day, d1)
        if not rr:
            continue
        used[h] += 1

        def mean_of(syms):
            v = [rr[s] for s in syms if s in rr]
            return (sum(v) / len(v) * 100) if v else None

        for tag, key in (("D8", 8), ("D9", 9), ("D10", 10)):
            syms = [s for s in sc if dec.get(s) == key]
            m = mean_of(syms)
            if m is not None:
                res[h][tag].append(m)
        for tag, k in (("top25", 25), ("top10", 10)):
            m = mean_of(ranked[:k])
            if m is not None:
                res[h][tag].append(m)
        m = mean_of(list(rr.keys()))
        if m is not None:
            res[h]["all"].append(m)

p(f"  {'期限':>5}{'天数':>5}" + "".join(f"{t:>11}" for t in
                                        ("all", "D8", "D9", "D10", "top25", "top10")))
for h in HORIZONS:
    r = res[h]
    if not r["D10"]:
        continue
    row = [f"{statistics.mean(r[t]):>+10.3f}%" if r[t] else f"{'-':>11}"
           for t in ("all", "D8", "D9", "D10", "top25", "top10")]
    p(f"  {h:>5}{used[h]:>5}" + "".join(row))
p()
p("  读法:")
p("   · 若负收益**随期限加深** => 与历史上「120 天反转」同向, 只是期限不同;")
p("   · 若 `top25` 比 `D10` 更负 => 与历史「反转只在最尖 top10」一致(尾的锐度问题);")
p("   · 若 `all`(全市场)同时为负 => 该期是**普跌**, 需先扣掉市场。")
p()

# ============================================================ C 中性化
p("=" * 84)
p("C 中性化: beta 与动量(在决策日截面内剥离, 再看残差排序的头部)")
p("=" * 84)


def mom20(day: str) -> dict:
    """20 交易日动量(用 h5i 回溯)。"""
    try:
        i = ALL.index(day)
    except ValueError:
        return {}
    if i < 21:
        return {}
    m0, m1 = closes(ALL[i - 20]), closes(day)
    out = {}
    for s, a in m0.items():
        b = m1.get(s)
        if a and b and a > 0:
            out[s] = b / a - 1.0
    return out


def neutralize(day: str, d1: str, exposure: str):
    """在决策日截面上: 用 `exposure` 对前向收益做横截面回归, 取残差。
    返回 (残差排序后的 top25 均值, top10 均值, D10 档均值)。

    暴露量:
      · "beta": 用**当日横截面**的收益协动近似不可得(只看一天), 故改用一个
        可算的代理 —— **前 20 日个股收益对同期等权市场收益的回归斜率**(见 beta20)。
      · "mom": 前 20 日累计收益。
    """
    sc = cache[day]["scores"]
    rr = rets_on(day, d1)
    if exposure == "mom":
        ex = mom20(day)
    else:
        ex = beta20(day)
    common = [s for s in sc if s in rr and s in ex and np.isfinite(ex[s])]
    if len(common) < 200:
        return None
    y = np.array([rr[s] for s in common])
    x = np.array([ex[s] for s in common])
    # 横截面 OLS: y = a + b x + e
    X = np.column_stack([np.ones_like(x), x])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    except Exception:  # noqa: BLE001
        return None
    resid = y - X @ coef
    order = np.argsort(-np.array([sc[s] for s in common]))      # 按融合分降序
    top = [common[i] for i in order]
    r25 = resid[order[:25]].mean() * 100
    r10 = resid[order[:10]].mean() * 100
    dec = decile_of({s: sc[s] for s in common})
    d10 = [k for k, s in enumerate(common) if dec.get(s) == 10]
    d10m = resid[d10].mean() * 100 if d10 else float("nan")
    return r25, r10, d10m


_BETA: dict[str, dict] = {}


def beta20(day: str) -> dict:
    """个股对等权市场的 20 日 beta(用日收益回归)。"""
    if day in _BETA:
        return _BETA[day]
    try:
        i = ALL.index(day)
    except ValueError:
        return {}
    if i < 21:
        return {}
    wdays = ALL[i - 20:i + 1]
    maps = [closes(d) for d in wdays]
    # 等权市场日收益
    mkt = []
    for k in range(1, len(maps)):
        a, b = maps[k - 1], maps[k]
        rs = [b[s] / a[s] - 1.0 for s in set(a) & set(b) if a[s] > 0]
        mkt.append(sum(rs) / len(rs) if rs else 0.0)
    mkt = np.array(mkt)
    var = mkt.var()
    out = {}
    if var <= 0:
        _BETA[day] = out
        return out
    for s in maps[-1]:
        rs = []
        for k in range(1, len(maps)):
            a, b = maps[k - 1].get(s), maps[k].get(s)
            rs.append((b / a - 1.0) if (a and b and a > 0) else np.nan)
        rr = np.array(rs)
        if np.isnan(rr).any():
            continue
        out[s] = float(np.cov(rr, mkt)[0, 1] / var)
    _BETA[day] = out
    return out


for exposure, label in (("beta", "beta 中性化"), ("mom", "动量中性化"), ("both", "beta+动量 中性化")):
    acc = []
    for day in DAYS:
        d1 = fwd(day, 5)
        if not d1:
            continue
        if exposure == "both":
            a1 = neutralize(day, d1, "beta")
            a2 = neutralize(day, d1, "mom")
            if a1 and a2:
                acc.append((a1, a2))
        else:
            a = neutralize(day, d1, exposure)
            if a:
                acc.append(a)
    if not acc:
        p(f"  {label}: 无法计算")
        continue
    if exposure == "both":
        p(f"  {label} (分别做, 看两种剥离各自的效果):")
        for idx, nm in ((0, "beta"), (1, "mom")):
            r25 = statistics.mean(a[idx][0] for a in acc)
            r10 = statistics.mean(a[idx][1] for a in acc)
            d10 = statistics.mean(a[idx][2] for a in acc)
            p(f"    {nm:5}: top25={r25:+.4f}%  top10={r10:+.4f}%  D10={d10:+.4f}%  (n={len(acc)})")
    else:
        r25 = statistics.mean(a[0] for a in acc)
        r10 = statistics.mean(a[1] for a in acc)
        d10 = statistics.mean(a[2] for a in acc)
        p(f"  {label}: top25={r25:+.4f}%  top10={r10:+.4f}%  D10={d10:+.4f}%  (n={len(acc)})")
p()
p("  读法: 若中性化后仍为负 => 负收益**不是** beta/动量暴露造成的(残差仍差);")
p("        若中性化后转正 => 顺周期暴露是主因, 可通过中性化修复。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

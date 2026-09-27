# -*- coding: utf-8 -*-
"""截尾比例: **期限**依赖 还是 **协议**依赖? —— 固定窗口与池, 只变前向期限。

## 为什么必须做 (2026-09-27)

出现两个**看似矛盾**的结果:

| 来源 | 窗口集 | 池 | 前向 | 最优截尾 |
|---|---|---|---|---|
| `scripts/trim_sensitivity.py`(实测) | 11 非重叠窗口 | 池内 | **120 天** | **5%** |
| `_tools/trim_vs_d10.py` | 14 决策日 | 全市场 | **1/3/5/7 天** | **3%** |

两者至少差**三个**维度 ⇒ 直接比较**不能**得出"随期限变化", 那是混淆归因(本仓 ⑤/⑦ 同族)。

本脚本固定**窗口集**(与 `trim_sensitivity.py` 同一批 `pit/xsec/<day>.parquet`)
与**池**(融合分非缺失的全体, 与它同源), **只**换前向期限, 用
`_tools/_fusion_scores.json` 的缓存分(与 `cross_section_scores` 同源)。

## 判据

· 最优 q **随 horizon 移动** => 截尾比例**期限依赖**, 需按持有期标定;
· 最优 q **各 horizon 一致** => 与期限无关; 那么 3% vs 5% 的差异必来自别处
  (窗口集 / 池 / 因子权重动态重算), **不能在期限上归因**。
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import h5i_bar_store as S  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XSEC = os.path.join(BASE, "data", "pit", "xsec")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fusion_scores.json")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_trim_by_horizon.txt")
TOPK = 10
Q_LIST = [0.00, 0.03, 0.05, 0.08, 0.10, 0.15]

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st_ = S.open_store()
ALL = st_.trading_days()
_px: dict[str, dict] = {}


def closes(day: str) -> dict:
    if day not in _px:
        df = st_.bars_on_day(day)
        _px[day] = {str(r.symbol): float(r.close) for r in df.itertuples(index=False)
                    if r.close is not None}
    return _px[day]


cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}


def _iso(d: str) -> str:
    d = str(d).strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d


win_days = [_iso(f.replace(".parquet", "")) for f in os.listdir(XSEC)
            if f.endswith(".parquet")]
win_days = sorted(d for d in win_days if d in ALL)
p(f"pit/xsec 窗口(在 h5i 日历内): {len(win_days)} 个  ({win_days[0]} .. {win_days[-1]})")
assert win_days, "pit/xsec 无可用窗口"

# 缺分的窗口就地补算(与 `trim_sensitivity.py` 调用的是**同一个** cross_section_scores,
# 故口径一致)。这样"窗口集"与"池"都与它相同, 只剩 horizon 一个变量。
miss = [d for d in win_days if not (cache.get(d) or {}).get("scores")]
if miss:
    p(f"  需补算融合分: {len(miss)} 个窗口  {miss[:3]}{' ...' if len(miss) > 3 else ''}")
    from factor_fusion import cross_section_scores  # noqa: E402

    for d in miss:
        try:
            r = cross_section_scores(d)
        except Exception as e:  # noqa: BLE001
            r = {"n_pool": 0, "n_scored": 0, "scores": {}, "meta": {"error": str(e)}}
        cache[d] = {"n_pool": r.get("n_pool"), "n_scored": r.get("n_scored"),
                    "coverage": r.get("coverage"), "scores": r.get("scores") or {},
                    "meta": r.get("meta")}
        print(f"  [scored] {d} n_scored={r.get('n_scored')}")
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    p(f"  ✓ 补算完成, 缓存已更新({CACHE})")
    p()

have = [d for d in win_days if (cache.get(d) or {}).get("scores")]
p(f"  可用融合分的窗口: {len(have)}/{len(win_days)}")
p(f"  h5i 末交易日: {ALL[-1]}")
p()
assert have, (
    f"xsec 有 {len(win_days)} 个窗口, 但一个都没有融合分。"
    f" 缓存键样例={list(cache)[:3]}")

usable = [d for d in win_days if (cache.get(d) or {}).get("scores") and d in ALL]
if not usable:
    # 不安静退出: 报出两个集合的差, 便于定位是"没分"还是"不在 h5i 日历"
    not_in_cal = [d for d in win_days if (cache.get(d) or {}).get("scores") and d not in ALL]
    raise AssertionError(
        f"有分的窗口 {len(have)} 个, 但都不在 h5i 交易日历里。"
        f" 不在日历的: {not_in_cal[:5]}; h5i 范围 {ALL[0]}..{ALL[-1]}")

max_h = min(len(ALL) - 1 - ALL.index(d) for d in usable)
HS = [h for h in (1, 3, 5, 7, 10, 20, 60, 120) if h <= max_h]
p(f"窗口: {usable}")
p(f"可用的前向交易日上限(受**最早**窗口限制): **{max_h}**  => 可扫 horizon = {HS}")
p("  [2026-09-27 自查] 我第一版在这里写了「120 天扫不了」—— **那句话是错的**:")
p("    · 受限的是**最新**窗口(2026-09-11, 之后没有 120 天数据);")
p("    · 而这批**历史**窗口里, 2026-03-05 + 120 交易日 = 2026-08-27, 在 h5i 覆盖内;")
p("    · 故本表 h=120 是**真的 120 天**, 不是截断口径。")
p("    教训: 报「能扫到多少」要看**每个窗口各自**的余量, 不能拿一个数概括。")
p()
if not HS:
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    print("written:", OUT)
    raise SystemExit(0)


def topk_after_trim(fz: pd.Series, q: float, k: int = TOPK):
    if q <= 0:
        return fz.nlargest(k).index
    cut = fz.quantile(1.0 - q)
    return fz[fz <= cut].nlargest(k).index


per = {h: {q: [] for q in Q_LIST} for h in HS}
used = {h: 0 for h in HS}
for day in usable:
    zmap = cache[day]["scores"]
    xs = pd.read_parquet(os.path.join(XSEC, f"{day}.parquet")).copy()
    if "sym6" not in xs.columns:
        xs = xs.assign(sym6=xs["canon"].astype(str).str.split(".").str[0].str.zfill(6))
    xs["sym6"] = xs["sym6"].astype(str).str.zfill(6)
    xs["fz"] = xs["sym6"].map(zmap)
    m = xs.dropna(subset=["fz"])
    if len(m) < 100:
        continue
    for h in HS:
        i = ALL.index(day)
        if i + h > len(ALL) - 1:
            continue
        m0, m1 = closes(day), closes(ALL[i + h])
        rr = []
        for s in m["sym6"]:
            a, b = m0.get(s), m1.get(s)
            v = (b / a - 1.0) if (a and b and a > 0) else np.nan
            rr.append(v if (v == v and -0.9 < v < 5.0) else np.nan)
        mm = m.assign(ret=rr).dropna(subset=["ret"])
        if len(mm) < 100:
            continue
        used[h] += 1
        for q in Q_LIST:
            sel = topk_after_trim(mm["fz"], q)
            if len(sel):
                per[h][q].append(float(mm.loc[sel, "ret"].mean()) * 100)

p("=" * 88)
p(f"截尾比例 × 前向期限(窗口集={len(usable)} 个, 池=融合分非缺失全体, top{TOPK})")
p("=" * 88)
p(f"  {'horizon':>8}{'n':>5}" + "".join(f"{'q'+format(q, '.2f'):>10}" for q in Q_LIST)
  + f"{'最优q':>9}")
best_by_h = {}
for h in HS:
    row, best_q, best_v = [], None, -1e9
    for q in Q_LIST:
        v = per[h][q]
        mu = st.mean(v) if v else float("nan")
        row.append(mu)
        if v and mu > best_v:
            best_v, best_q = mu, q
    best_by_h[h] = (best_q, best_v)
    p(f"  {h:>8}{used[h]:>5}" + "".join(f"{x:>+9.2f}%" for x in row)
      + f"{(format(best_q, '.2f') if best_q is not None else '-'):>9}")
p()
p("  各 horizon 的最优截尾比例:")
for h in HS:
    q, v = best_by_h[h]
    if q is not None:
        p(f"    h={h:<4} 最优 q={q:.2f}  (均值 {v:+.2f}%, n={used[h]})")
p()
qs = [best_by_h[h][0] for h in HS if best_by_h[h][0] is not None]
p(f"  最优 q 序列: {qs}")
if qs and len(set(qs)) == 1:
    p("  => **与期限无关**(各 horizon 一致) —— 那么 3% vs 5% 的差异**不能**归因到期限,")
    p("     必来自窗口集 / 池口径 / 因子权重的动态重算。")
else:
    p("  => 最优 q 随 horizon 变化 ⇒ **期限依赖**, 需按持有期标定。")
p()
p(f"  ⚠️ 样本边界: 仅 {len(usable)} 个窗口; h={HS[-1]} 的可用窗口 {used[HS[-1]]} 个。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

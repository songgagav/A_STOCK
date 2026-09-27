# -*- coding: utf-8 -*-
"""每日选股的**重叠度** + 因子日度稳定性 —— 判"每日换池"假设。

## 要回答

用户: 若每日选出 10 只与前一天重叠度低(0~3/10) => **每日换池是主导**;
      若重叠度高(7~10/10) => 持有期应自然更长, 那短持有期就得另找原因。

## 两个口径都算(它们不是一回事, 必须分开)

| 口径 | 来源 | 含义 |
|---|---|---|
| **目标池** | `portfolio_live.make_targets_of` | 生产实际会去持有的 10 只(走**回退梯子**) |
| **融合分 top10** | `factor_fusion.cross_section_scores` | 纯因子口径的前 10 名 |

**为什么要分开**: 上一轮已确认"池来自 `daily/<D-1>/selection.json`"(滞后 1 天),
故池的重叠度未必等于因子的重叠度。若两者差很大, 本身就是线索。

## 因子日度稳定性

对相邻两日, 取**共同标的**上的融合分, 算 **Spearman 秩相关**与
**top10 的集合重叠**。秩相关高但 top10 重叠低 ⇒ 排序整体稳定而**头部抖动** ——
那是"头部噪声", 会让每日换池与高换手自然发生。
"""
from __future__ import annotations

import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_overlap.txt")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fusion_scores.json")

import numpy as np  # noqa: E402
import h5i_bar_store as S  # noqa: E402
import portfolio_live as PL  # noqa: E402

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st_ = S.open_store()
ALL = st_.trading_days()
tf = PL.make_targets_of(live_pool=False)

# 可服务决策日
served = []
for d in ALL:
    try:
        if tf(d):
            served.append(d)
    except Exception:  # noqa: BLE001
        pass
p(f"目标池可服务的决策日: {len(served)}  ({served[0]} .. {served[-1]})")
assert served, "无可用决策日"
p()

# 目标池的成员
pool: dict[str, list] = {}
for d in served:
    try:
        pool[d] = [str(t.get("canon") or t.get("symbol") or "").split(".")[0]
                   for t in (tf(d) or [])]
    except Exception:  # noqa: BLE001
        pool[d] = []
pool = {k: v for k, v in pool.items() if v}

p("=" * 86)
p("1) 目标池的日度重叠(与**前一交易日**的 10 只比)")
p("=" * 86)
p(f"  {'日期':12}{'n':>4}{'与前日重叠':>11}{'新增':>6}")
ov = []
ks = sorted(pool)
for i, d in enumerate(ks):
    if i == 0:
        p(f"  {d:12}{len(pool[d]):>4}{'-':>11}{'-':>6}")
        continue
    prev = set(pool[ks[i - 1]])
    cur = set(pool[d])
    n = len(prev & cur)
    ov.append(n)
    p(f"  {d:12}{len(pool[d]):>4}{n:>8}/10{len(cur - prev):>6}")
p()
if ov:
    p(f"  重叠均值 {st.mean(ov):.2f}/10   中位 {st.median(ov)}/10   范围 [{min(ov)}, {max(ov)}]")
    p(f"  分布: " + ", ".join(f"{k}只×{ov.count(k)}" for k in sorted(set(ov))))
    p()
    if st.mean(ov) <= 3:
        p("  => **重叠低(<=3/10) => 每日换池是主导** —— 与用户假设一致。")
    elif st.mean(ov) >= 7:
        p("  => **重叠高(>=7/10) => 持有期本应自然更长**, 短持有期需另找原因(查卖出逻辑)。")
    else:
        p(f"  => **中等重叠({st.mean(ov):.1f}/10)** —— 既非每日全换, 也非稳定持有。")
p()

p("=" * 86)
p("2) 因子口径: 融合分 top10 的日度重叠 + 秩相关")
p("=" * 86)
cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
have = [d for d in ks if (cache.get(d) or {}).get("scores")]
p(f"  有融合分的决策日: {len(have)} / {len(ks)}")
p()
if len(have) >= 2:
    p(f"  {'日期':12}{'top10 与前日重叠':>18}{'共同标的秩相关':>16}{'分位重叠':>10}")
    ovs, rhos = [], []
    for i in range(1, len(have)):
        d0, d1 = have[i - 1], have[i]
        s0 = cache[d0]["scores"]
        s1 = cache[d1]["scores"]
        t0 = set(sorted(s0, key=lambda k: -s0[k])[:10])
        t1 = set(sorted(s1, key=lambda k: -s1[k])[:10])
        n = len(t0 & t1)
        ovs.append(n)
        common = sorted(set(s0) & set(s1))
        rho = float("nan")
        if len(common) > 100:
            a = np.array([s0[c] for c in common])
            b = np.array([s1[c] for c in common])
            ra = np.argsort(np.argsort(a)).astype(float)
            rb = np.argsort(np.argsort(b)).astype(float)
            rho = float(np.corrcoef(ra, rb)[0, 1])
            rhos.append(rho)
        p(f"  {d1:12}{n:>15}/10{rho:>16.4f}{len(common):>10}")
    p()
    p(f"  top10 重叠: 均值 {st.mean(ovs):.2f}/10  中位 {st.median(ovs)}/10  "
      f"范围 [{min(ovs)}, {max(ovs)}]")
    if rhos:
        p(f"  秩相关(共同标的): 均值 {st.mean(rhos):.4f}  中位 {st.median(rhos):.4f}  "
          f"范围 [{min(rhos):.4f}, {max(rhos):.4f}]")
    p()
    p("  读法(**关键**):")
    p("   · 秩相关**高**而 top10 重叠**低** => 排序整体稳定, 但**头部抖动**")
    p("     => 每日换池是「头部噪声」的自然结果, 不是信号整体不稳定;")
    p("   · 秩相关**低** => 因子日度本身不稳, 那才是更深的问题。")
p()

p("=" * 86)
p("3) 结论")
p("=" * 86)
if ov:
    p(f"  目标池重叠均值 {st.mean(ov):.2f}/10")
if len(have) >= 2 and ovs:
    p(f"  因子 top10 重叠均值 {st.mean(ovs):.2f}/10")
    p()
    if rhos and st.mean(rhos) > 0.9 and st.mean(ovs) <= 3:
        p("  => **秩相关很高但 top10 几乎全换** —— 头部 10 名在日度上近乎重排。")
        p("     这解释了: ①为何换手高 ②为何「每日换池」看起来是主导")
        p("     ③为何 `min_hold` 想拦也拦不住(拦的是卖出, 不是重排)。")

p("=" * 86)
p("4) **双峰归因**: 目标池的来源(DRL plan / selection 回退)")
p("=" * 86)
p("  上一节发现目标池重叠是**双峰**的(0只×7 与 10只×8), 几乎无中间值。")
p("  假设: 来源切换造成 —— 同一来源内几乎不换, 一换来源就整池换。")
p("  检验: 用 `data/drl/<C>/target_plan.json` 与 `data/daily/<C>/selection.json`")
p("        反查每天池的来源, 再按来源给重叠分组。")
p()

DRL = os.path.join(BASE, "data", "drl")
DAILY = os.path.join(BASE, "data", "daily")


def find_source(day: str, members: set):
    """反查该日池来自哪个文件; 返回 (kind, cand_day)。"""
    for cand in sorted((x for x in os.listdir(DAILY)
                        if x.isdigit() and len(x) == 8), reverse=True):
        sp = os.path.join(DAILY, cand, "selection.json")
        if not os.path.exists(sp):
            continue
        try:
            sel = json.load(open(sp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        bare = {str((t.get("canon") or t.get("symbol") or "")).split(".")[0]
                for t in (sel.get("top_n") or [])}
        if bare and bare == members:
            return "selection", cand
    for cand in sorted((x for x in os.listdir(DRL)
                        if x.isdigit() and len(x) == 8), reverse=True):
        tp = os.path.join(DRL, cand, "target_plan.json")
        if not os.path.exists(tp):
            continue
        try:
            j = json.load(open(tp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        bare = {str((t.get("canon") or t.get("symbol") or "")).split(".")[0]
                for t in (j.get("picks") or j.get("top_n") or [])}
        if bare and bare == members:
            return "drl_plan", cand
    return "?", ""


src_of = {}
for d in ks:
    src_of[d] = find_source(d, set(pool[d]))

p(f"  {'日期':12}{'来源':11}{'来源日':10}{'滞后':>5}{'与前日重叠':>11}{'前日来源':>11}")
for i, d in enumerate(ks):
    kind, cd = src_of[d]
    lag = ""
    if cd:
        try:
            lag = str(int(d.replace("-", "")) - int(cd))
        except Exception:  # noqa: BLE001
            lag = "?"
    if i == 0:
        p(f"  {d:12}{kind:11}{cd:10}{lag:>5}{'-':>11}{'-':>11}")
        continue
    prev = set(pool[ks[i - 1]])
    n = len(prev & set(pool[d]))
    p(f"  {d:12}{kind:11}{cd:10}{lag:>5}{str(n)+'/10':>11}{src_of[ks[i-1]][0]:>11}")
p()

# 按来源分组统计
from collections import defaultdict
by_kind = defaultdict(list)
for i, d in enumerate(ks):
    if i == 0:
        continue
    by_kind[src_of[d][0]].append(len(set(pool[ks[i - 1]]) & set(pool[d])))
p("  按**当日来源**分组的重叠:")
for k, v in sorted(by_kind.items()):
    p(f"    {k:11} n={len(v):>2}  均值 {st.mean(v):.2f}/10  分布 {sorted(v)}")
p()
same = [len(set(pool[ks[i - 1]]) & set(pool[d]))
        for i, d in enumerate(ks) if i > 0 and src_of[d][0] == src_of[ks[i - 1]][0]]
diff = [len(set(pool[ks[i - 1]]) & set(pool[d]))
        for i, d in enumerate(ks) if i > 0 and src_of[d][0] != src_of[ks[i - 1]][0]]
p("  **决定性对照**:")
if same:
    p(f"    来源**未变**时: n={len(same):>2}  重叠均值 {st.mean(same):.2f}/10  分布 {sorted(same)}")
if diff:
    p(f"    来源**改变**时: n={len(diff):>2}  重叠均值 {st.mean(diff):.2f}/10  分布 {sorted(diff)}")
p()
if same and diff:
    p(f"  => 差 {st.mean(same) - st.mean(diff):+.2f} 只/10。")
    if st.mean(same) - st.mean(diff) > 3:
        p("     **双峰由来源切换解释**: 同一来源内几乎不换池, 一换来源就整池换。")
        p("     ⇒ 「每日换池」不是因子抖动, 而是**回退梯子在换档**。")
p()

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

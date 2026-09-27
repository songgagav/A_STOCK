# -*- coding: utf-8 -*-
"""十分档分析: 融合因子分档 -> 持有期收益; 并对比「因子 Top10」vs「实际持仓 Top10」。

## 要回答的两个互斥假设

· **假设一 头部反转**: D10(最高分档)持有期收益为负 => 因子在头部失效(真信号问题);
· **假设二 口径分叉**: D10 为正, 但实际持仓(10 只)为负
  => 问题出在"因子分 -> 目标池"这一段, 不在因子本身。

故必须**同时**算两者, 并列出差异清单。判据(用户给定):
  D10 为负 => 确认头部反转;  D10 为正 => 转向假设二。

## 持有期口径

用户要求"每档持有期收益", 窗口统一取 **10 个交易日**(与前瞻窗口一致的量级),
并在 14 个连续窗口上各算一次 -> 得到分档收益的稳定性。

## 缓存

`cross_section_scores` 每日较慢, 结果落 `_tools/_fusion_scores.json` 缓存,
重跑不重算(除非加 `--refresh`)。
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np  # noqa: E402
import h5i_bar_store as S  # noqa: E402
import portfolio_live as PL  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_deciles.txt")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fusion_scores.json")
REFRESH = "--refresh" in sys.argv

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st = S.open_store()
ALL_DAYS = st.trading_days()
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


# ---------------------------------------------------------------- 目标池(实际持仓)
tf = PL.make_targets_of(live_pool=False)
served = []
for d in ALL_DAYS:
    try:
        if tf(d):
            served.append(d)
    except Exception:  # noqa: BLE001
        pass

# 14 个连续 10 天窗口的起点
wins = []
for i in range(len(served) - 10 + 1):
    w = served[i:i + 10]
    if w == [d for d in ALL_DAYS if w[0] <= d <= w[-1]][:10]:
        wins.append(w)
wins = [w for w in wins if w[-1] in ALL_DAYS]
p(f"目标池可服务交易日: {len(served)}  ({served[0]} .. {served[-1]})")
p(f"连续 10 天窗口数: {len(wins)}")
p()

# ---------------------------------------------------------------- 融合分(带缓存)
from factor_fusion import cross_section_scores  # noqa: E402

cache = {}
if os.path.exists(CACHE) and not REFRESH:
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}

need = sorted({w[0] for w in wins})
p(f"需要融合分的决策日: {len(need)} 天")
for d in need:
    if d in cache and cache[d].get("scores"):
        continue
    try:
        r = cross_section_scores(d)
    except Exception as e:  # noqa: BLE001
        r = {"as_of": d, "n_pool": 0, "n_scored": 0, "scores": {},
             "meta": {"error": f"{type(e).__name__}: {e}"}}
    cache[d] = {"n_pool": r.get("n_pool"), "n_scored": r.get("n_scored"),
                "coverage": r.get("coverage"), "scores": r.get("scores") or {},
                "meta": r.get("meta")}
    print(f"  [scored] {d}  n_pool={r.get('n_pool')} n_scored={r.get('n_scored')}")
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)

for d in need:
    c = cache.get(d) or {}
    p(f"  {d}: n_pool={c.get('n_pool')} n_scored={c.get('n_scored')} "
      f"coverage={c.get('coverage')}")
p()
assert any((cache.get(d) or {}).get("scores") for d in need), (
    "没有任何一天取到融合分 —— 取数失败, 不要在空数据上出结论")

# ---------------------------------------------------------------- ① 十分档
p("=" * 84)
p("① 十分档持有期(10 交易日)收益 —— 每个窗口独立分档")
p("=" * 84)


def decile_ret(day: str, d1: str, n_dec: int = 10):
    sc = (cache.get(day) or {}).get("scores") or {}
    m0, m1 = closes(day), closes(d1)
    rows = []
    for s, v in sc.items():
        a, b = m0.get(s), m1.get(s)
        if a and b and a > 0:
            r = b / a - 1.0
            if -0.9 < r < 5.0:
                rows.append((float(v), r))
    if len(rows) < n_dec * 5:
        return None
    rows.sort(key=lambda x: x[0])                 # 分低 -> 高
    n = len(rows)
    out = []
    for k in range(n_dec):
        lo, hi = k * n // n_dec, (k + 1) * n // n_dec
        seg = rows[lo:hi]
        out.append(sum(r for _, r in seg) / len(seg) * 100 if seg else float("nan"))
    return out, n


dec_rows = []
for w in wins:
    r = decile_ret(w[0], w[-1])
    if r:
        dec_rows.append((w[0], w[-1], r[0], r[1]))

p(f"  {'窗口':24}{'n':>6}" + "".join(f"{'D'+str(k+1):>9}" for k in range(10)))
for d0, d1, ds, n in dec_rows:
    p(f"  {d0+'..'+d1:24}{n:>6}" + "".join(f"{v:>+8.2f}%" for v in ds))
p()
if dec_rows:
    p(f"  {'均值':24}{'':>6}" + "".join(
        f"{statistics.mean([r[2][k] for r in dec_rows]):>+8.2f}%" for k in range(10)))
    p(f"  {'中位':24}{'':>6}" + "".join(
        f"{statistics.median([r[2][k] for r in dec_rows]):>+8.2f}%" for k in range(10)))
    p(f"  {'正窗口':24}{'':>6}" + "".join(
        f"{sum(1 for r in dec_rows if r[2][k] > 0):>6}/{len(dec_rows):<2}" for k in range(10)))
    p()
    p("  === 重点 D8 / D9 / D10(最高分档) ===")
    for k in (7, 8, 9):
        vals = [r[2][k] for r in dec_rows]
        p(f"    D{k+1}: 均值={statistics.mean(vals):+.4f}%  "
          f"中位={statistics.median(vals):+.4f}%  "
          f"正={sum(1 for v in vals if v>0)}/{len(vals)}")
    d10 = [r[2][9] for r in dec_rows]
    d1_ = [r[2][0] for r in dec_rows]
    p()
    p(f"  D10 - D1 均值差 = {statistics.mean(d10) - statistics.mean(d1_):+.4f}pp "
      f"(高减低; 正 = 因子方向有效)")
    p("  判据: **D10 为负 => 确认头部反转**; D10 为正 => 转假设二(口径分叉)。")
p()

# ---------------------------------------------------------------- ② Top10 对比
p("=" * 84)
p("② 「因子 Top10」vs「实际持仓(目标池)」差异")
p("=" * 84)
p(f"  {'决策日':12}{'因子Top10(前5)':46}{'实际池(前5)':34}{'重叠':>6}")
overlaps = []
for w in wins[:6]:
    day = w[0]
    sc = (cache.get(day) or {}).get("scores") or {}
    if not sc:
        continue
    top_f = [s for s, _ in sorted(sc.items(), key=lambda kv: -kv[1])[:10]]
    try:
        pool = [str(t.get("canon") or t.get("symbol")) for t in (tf(day) or [])]
    except Exception:  # noqa: BLE001
        pool = []
    pool_bare = [str(t.get("canon") or t.get("symbol") or "").split(".")[0]
                 for t in (tf(day) or [])]
    ov = len(set(top_f) & set(pool_bare))
    overlaps.append((day, top_f, pool_bare, ov))
    p(f"  {day:12}{','.join(top_f[:5]):46}{','.join(pool_bare[:5]):34}{ov:>5}/10")
if overlaps:
    p()
    p(f"  Top10 重叠均值 = {statistics.mean([o[3] for o in overlaps]):.1f}/10")
    p("  若重叠低 => **口径分叉**(实际池不是因子 Top10); 若接近 10 => 同源。")
    p()
    p("  差异清单(首个决策日的完整对比):")
    day, top_f, pool_bare, _ = overlaps[0]
    p(f"    因子 Top10 : {top_f}")
    p(f"    实际池     : {pool_bare}")
    p(f"    只在因子侧 : {sorted(set(top_f) - set(pool_bare))}")
    p(f"    只在池侧   : {sorted(set(pool_bare) - set(top_f))}")
    p()
    p("  ⚠️ **重叠 0/10 需要先排除一个机械解释**: 回退梯子取的是 `C < D` 的**最早可用计划**,")
    p("     故 `targets_of(D)` 可能返回**陈旧**计划(如 D=08-25 却用 09-07 的 plan)。")
    p("     若如此, 上面的对比就不是「两套口径」, 而是「拿 D 的分去比 C 的池」 —— 无意义。")
    p("     下面显式检查池的**来源日期**。")
p()

# ---------------------------------------------------------------- ②b 池来源新鲜度
p("=" * 84)
p("②b 目标池来源新鲜度(池究竟是哪一天选出来的?)")
p("=" * 84)
import glob  # noqa: E402

_DAILY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "daily")
p(f"  {'决策日':12}{'池(前3)':34}{'在哪天的 plan/selection 里找到':38}{'滞后':>6}")
fresh = []
for w in wins:
    day = w[0]
    try:
        pool = [str(t.get("canon") or t.get("symbol") or "").split(".")[0]
                for t in (tf(day) or [])]
    except Exception:  # noqa: BLE001
        pool = []
    if not pool:
        continue
    src = None
    # 在 data/daily/<C>/selection.json 与 data/drl/<C>/target_plan.json 里反查
    for cand in sorted(os.listdir(_DAILY), reverse=True):
        sp = os.path.join(_DAILY, cand, "selection.json")
        if not os.path.exists(sp):
            continue
        try:
            sel = json.load(open(sp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        tops = sel.get("top_n") or []
        bare = {str((t.get("canon") or t.get("symbol") or "")).split(".")[0]
                for t in tops}
        if bare and bare == set(pool):
            src = ("daily/" + cand, cand)
            break
    if src is None:
        for cand in sorted(os.listdir(os.path.join(_DAILY, "..", "drl")), reverse=True):
            tp = os.path.join(_DAILY, "..", "drl", cand, "target_plan.json")
            if not os.path.exists(tp):
                continue
            try:
                tpj = json.load(open(tp, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            picks = tpj.get("picks") or tpj.get("top_n") or []
            bare = {str((t.get("canon") or t.get("symbol") or "")).split(".")[0]
                    for t in picks}
            if bare and bare == set(pool):
                src = ("drl/" + cand, cand)
                break
    tag, cand_d = (src if src else ("<未找到>", ""))
    lag = ""
    if cand_d:
        try:
            lag = str((int(day.replace("-", "")) - int(cand_d)))
        except Exception:  # noqa: BLE001
            lag = "?"
    fresh.append((day, bool(src), lag))
    p(f"  {day:12}{','.join(pool[:3]):34}{tag:38}{lag:>6}")

ok = sum(1 for _, f, _ in fresh if f)
p()
p(f"  能在磁盘上反查到来源的: {ok}/{len(fresh)}")
if fresh:
    lags = [int(l) for _, f, l in fresh if f and str(l).isdigit()]
    if lags:
        p(f"  滞后(决策日 - 来源日, 自然日): {lags}")
        p(f"  滞后 > 0 的天数: {sum(1 for l in lags if l > 0)}/{len(lags)}")
        p("  => **滞后 > 0 表示该窗口实际用的是陈旧计划**, 那么'因子 Top10 vs 实际池'的")
        p("     对比就不同源, **不能据此判口径分叉**; 应只看滞后 = 0 的那些天。")
p()

# ---------------------------------------------------------------- ③ 权重
p("=" * 84)
p("③ 实际权重 vs 等权 0.1")
p("=" * 84)
for w in wins[:3]:
    day = w[0]
    try:
        items = list(tf(day) or [])
    except Exception:  # noqa: BLE001
        items = []
    ws = []
    for t in items:
        v = t.get("target_weight")
        ws.append(float(v) if v is not None else None)
    if not ws:
        continue
    if all(v is None for v in ws):
        p(f"  {day}: 无 target_weight 字段 => 退化为**等权** 0.1(10 只)")
    else:
        vals = [v for v in ws if v is not None]
        p(f"  {day}: n={len(ws)} 权重={[round(v,4) for v in vals]}")
        if vals:
            p(f"        min={min(vals):.4f} max={max(vals):.4f} "
              f"和={sum(vals):.4f} "
              f"是否等权={abs(max(vals)-min(vals))<1e-9}")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

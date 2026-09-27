# -*- coding: utf-8 -*-
"""P0: 深挖「回测 4.9 天 vs 生产 1 天」的口径缺口 —— 取价时点是否等价。

## 已确认的事实(有据)

· **生产**成交时间实测多在 `09:30:06 ~ 09:30:21` —— **开盘瞬间**;
· **回测**(`portfolio_backtest`)用 `price_of(day, canon)` = **当日收盘价**撮合;
· **h5i 不存 OHLC** —— `bars_on_day` 只有 `['symbol','close','change_pct','turnover','amount']`;
· **但厂商引擎有 OHLC** —— `engine_bars_sync.fetch_day()` 返回
  `['symbol','date','open','high','low','close','volume','amount','change_pct','turnover']`
  ⇒ **开盘价可得, 只是在落 h5i 时被丢掉**。

## 故本脚本能真正量化"取价时点差"

A) 生产成交价 vs 同日 **open** vs 同日 **close** ⇒ 生产到底锚在哪;
B) 全市场"开->收"日内漂移 ⇒ 两种口径的系统性偏差量级;
C) 用生产成交价(锚 open)与回测价(锚 close)各算**同一批真实成交**的往返收益差。
"""
from __future__ import annotations

import glob
import json
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_timing_gap.txt")

import engine_bars_sync as E  # noqa: E402
import h5i_bar_store as S  # noqa: E402

lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


st_ = S.open_store()
ALL = st_.trading_days()
_ohlc: dict[str, dict] = {}
_ohlc_fail: set[str] = set()


def ohlc(day: str) -> dict:
    """{bare_code: {open,close,high,low}} —— 用厂商引擎(唯一有 OHLC 的源)。"""
    if day in _ohlc:
        return _ohlc[day]
    if day in _ohlc_fail:
        return {}
    try:
        r = E.fetch_day(day.replace("-", ""))
        df = r[0] if isinstance(r, tuple) else r
        d = {}
        for row in df.itertuples(index=False):
            try:
                d[str(row.symbol)] = {"open": float(row.open), "close": float(row.close),
                                      "high": float(row.high), "low": float(row.low)}
            except Exception:  # noqa: BLE001
                continue
        _ohlc[day] = d
        return d
    except Exception as e:  # noqa: BLE001
        print(f"  [ohlc-fail] {day}: {type(e).__name__}")
        _ohlc_fail.add(day)
        return {}


def h5i_close(day: str, code: str):
    try:
        df = st_.bars_on_day(day)
        for r in df.itertuples(index=False):
            if str(r.symbol) == code:
                v = float(r.close)
                return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None
    return None


def load_trades():
    merged: dict[str, dict] = {}
    cand = [os.path.join(BASE, "data", "state.json"),
            os.path.join(BASE, "data", "_backup_before_dryrun", "state.json")]
    cand += sorted(glob.glob(os.path.join(BASE, "data", "daily", "*", "paper_book.json")))
    for fp in cand:
        if not os.path.exists(fp):
            continue
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for day, items in (d.get("trades_history") or {}).items():
            b = merged.setdefault(day, {})
            for t in items or []:
                k = (t.get("date") or day, t.get("type"), t.get("canon"),
                     t.get("qty"), t.get("time"))
                b.setdefault(k, t)
    return {d: list(v.values()) for d, v in merged.items()}


tr = load_trades()
days = sorted(tr)
p(f"生产成交流水: {len(days)} 个交易日  {days[0]} .. {days[-1]}")
p(f"h5i 列(注意**无 OHLC**): {list(st_.bars_on_day(ALL[-1]).columns)}")
# 厂商引擎只到它最后发布的交易日(09-22); h5i 因回填更靠后(09-24) => 必须容错
_last_ok = None
for d in reversed(ALL):
    rr = ohlc(d)
    if rr:
        _last_ok = d
        break
if _last_ok is None:
    raise AssertionError("厂商引擎在所有 h5i 交易日都取不到 OHLC —— 无法做时点对比")
r0 = E.fetch_day(_last_ok.replace("-", ""))
df0 = r0[0] if isinstance(r0, tuple) else r0
p(f"厂商引擎列(**有 OHLC**): {list(df0.columns)}")
p(f"引擎可用的最后一个交易日: **{_last_ok}**(h5i 末端是 {ALL[-1]} —— 回填补的更靠后)")
p("  => 故时点对比只能在引擎覆盖的日期上做; 生产成交若落在 09-23/24 则无 OHLC 可比。")
p()

# ---------------------------------------------------------------- A 生产锚在哪
p("=" * 90)
p("A) 生产成交价锚在哪: 对 **open** 还是对 **close**?")
p("=" * 90)
p(f"  {'日期':12}{'类型':5}{'代码':11}{'成交价':>9}{'时间':>9}{'open':>9}{'close':>9}"
  f"{'对open%':>9}{'对close%':>10}")
vs_open, vs_close = [], []
shown = 0
for day in days:
    o = ohlc(day)
    if not o:
        continue
    for t in tr[day]:
        code = str(t.get("canon") or "").split(".")[0]
        pr = t.get("price")
        if pr is None or code not in o:
            continue
        op, cl = o[code]["open"], o[code]["close"]
        if not (op > 0 and cl > 0):
            continue
        d_o = (float(pr) / op - 1.0) * 100
        d_c = (float(pr) / cl - 1.0) * 100
        vs_open.append(abs(d_o))
        vs_close.append(abs(d_c))
        if shown < 16:
            p(f"  {day:12}{t.get('type'):5}{code:11}{float(pr):>9.3f}"
              f"{(t.get('time') or ''):>9}{op:>9.3f}{cl:>9.3f}{d_o:>+8.3f}%{d_c:>+9.3f}%")
            shown += 1
p()
if vs_open:
    p(f"  可比对成交: {len(vs_open)} 笔")
    p(f"  |成交价/open  - 1| 均值 = {st.mean(vs_open):.4f}%   中位 = {st.median(vs_open):.4f}%")
    p(f"  |成交价/close - 1| 均值 = {st.mean(vs_close):.4f}%   中位 = {st.median(vs_close):.4f}%")
    p()
    if st.mean(vs_open) < st.mean(vs_close):
        p("  => **生产锚在 open 一侧**(对 open 的偏离更小) —— 与「09:30 成交」一致。")
    else:
        p("  => 更接近 close; 需复查成交时刻的实际价位来源。")
p()

# ---------------------------------------------------------------- B 日内漂移
p("=" * 90)
p("B) 全市场日内漂移(开->收): 两种口径的系统性偏差量级")
p("=" * 90)
p(f"  {'日期':12}{'n':>6}{'开->收 均值':>13}{'中位':>10}")
drifts = []
for day in days:
    o = ohlc(day)
    if not o:
        continue
    rs = []
    for code, v in o.items():
        if v["open"] > 0 and v["close"] > 0:
            r = v["close"] / v["open"] - 1.0
            if -0.2 < r < 0.2:
                rs.append(r)
    if rs:
        drifts.append((day, st.mean(rs) * 100, st.median(rs) * 100))
        p(f"  {day:12}{len(rs):>6}{st.mean(rs)*100:>+12.4f}%{st.median(rs)*100:>+9.4f}%")
if drifts:
    p()
    m = st.mean([d[1] for d in drifts])
    p(f"  跨日平均: 开->收 {m:+.4f}%")
    p("  读法: 若「开->收」系统性为正, 则**按收盘成交的回测**会偏向高估买入成本?")
    p("        不 —— 买单按收盘成交=买在当日更高价 => 回测买入更贵; 卖出同理。")
    p("        方向需按**买卖**分开看, 但同一天同一价, 故**往返**上开->收漂移会被抵消,")
    p("        除非持有期跨越该漂移的不同侧。")
p()

# ---------------------------------------------------------------- C 往返收益差
p("=" * 90)
p("C) 同一标的: 「生产口径(open->open)」vs 「回测口径(close->close)」往返收益")
p("=" * 90)
# 用 FIFO 配平生产的买卖, 再对同一区间算 close->close
buys: dict[str, list] = {}
pairs = []
for day in days:
    for t in sorted(tr[day], key=lambda x: (x.get("type") != "sell", x.get("time") or "")):
        code = str(t.get("canon") or "").split(".")[0]
        qty = int(t.get("qty") or 0)
        if qty <= 0:
            continue
        if t.get("type") == "buy":
            buys.setdefault(code, []).append([day, qty, float(t.get("price") or 0)])
        else:
            need = qty
            lots = buys.get(code) or []
            while need > 0 and lots:
                d0, q0, pr0 = lots[0]
                take = min(need, q0)
                pairs.append((code, d0, day, pr0, float(t.get("price") or 0)))
                need -= take
                q0 -= take
                if q0 <= 0:
                    lots.pop(0)
                else:
                    lots[0][1] = q0
            buys[code] = lots

p(f"  FIFO 配平往返: {len(pairs)} 对")
prod_r, bt_r = [], []
for code, d0, d1, pr0, pr1 in pairs:
    if pr0 <= 0 or pr1 <= 0:
        continue
    prod_r.append((pr1 / pr0 - 1.0) * 100)
    c0, c1 = h5i_close(d0, code), h5i_close(d1, code)
    if c0 and c1 and c0 > 0:
        bt_r.append((c1 / c0 - 1.0) * 100)
if prod_r and bt_r and len(prod_r) == len(bt_r):
    diffs = [a - b for a, b in zip(prod_r, bt_r)]
    p(f"  生产口径(实际成交价) 往返均值: {st.mean(prod_r):+.4f}%  中位 {st.median(prod_r):+.4f}%")
    p(f"  回测口径(收盘->收盘) 往返均值: {st.mean(bt_r):+.4f}%  中位 {st.median(bt_r):+.4f}%")
    p(f"  => **差 {st.mean(prod_r) - st.mean(bt_r):+.4f}pp / 往返**")
    p(f"     逐笔差(生产-回测): 均值 {st.mean(diffs):+.4f}pp  "
      f"中位 {st.median(diffs):+.4f}pp")
    p()
    # 配对显著性: 均值 vs 中位差距较大 => 必须看是否被离群点主导
    n = len(diffs)
    sd = st.stdev(diffs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else 0.0
    t = (st.mean(diffs) / se) if se > 0 else float("nan")
    pos = sum(1 for x in diffs if x > 0)
    p(f"  配对统计(n={n}): 均值={st.mean(diffs):+.4f}pp  sd={sd:.4f}  se={se:.4f}  "
      f"**t={t:+.2f}**")
    p(f"    正号 {pos}/{n}   中位 {st.median(diffs):+.4f}pp")
    big = sorted(diffs, key=abs, reverse=True)[:3]
    p(f"    最大的 3 个 |差|: {[round(x,3) for x in big]}")
    p()
    p("  读法(**关键**):")
    if abs(t) >= 2:
        p(f"    |t|={abs(t):.2f} >= 2 => 在 n={n} 下**显著**; 但仍是**单月、单一批成交**,")
        p("    不能外推为长期偏差。")
    else:
        p(f"    |t|={abs(t):.2f} < 2 => 在 n={n} 下**不显著**; 均值可能被少数大偏离拉动")
        p("    (看均值与中位之差、以及最大 |差|)。**不能据此量化长期偏差。**")
    p(f"    正号占比 {pos}/{n} = {pos/n:.0%} —— 若接近 50% 则更像噪声方向。")
elif prod_r:
    p(f"  生产口径 n={len(prod_r)} 往返均值 {st.mean(prod_r):+.4f}%")
    p(f"  回测口径 n={len(bt_r)}(不齐, 跳过逐笔配对)")
else:
    p("  无法配平")

p()
p("=" * 90)
p("结论")
p("=" * 90)
p("  1. **取价时点确实不同**: 生产锚 **open**(09:30 成交), 回测锚 **close**;")
p("  2. **h5i 不存 OHLC**, 故回测**无法**按开盘价撮合 —— 除非改落库(引擎源有 OHLC);")
p("  3. 故「调整回测成交时点」这条建议**需要先补数据管道**, 不是改一行代码;")
p("  4. 缺口对收益的**净影响**见 C 段(往返差); 若量级大, 才值得改管道。")

open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)

# -*- coding: utf-8 -*-
"""修复 P1-H5IUNIT-0904: h5i daily_bars 2026-09-04 的存量单位损坏.

问题（实测，见 data/audit_units.json 与 data/_fix0904_recon.json）
----------------------------------------------------------------
| 子问题 | 规模 |
|---|---|
| `volume` 写成「手」(比「股」小 100 倍) | **405 只**，全在 `00xxxx` 深市主板 |
| `volume` 反向大 100 倍 | **1 只** (`689009`) |
| `turnover` 写成小数(应为 %) | **5198 条**(当天全部) |
| 缺失标的 | **314 只**(引擎有、h5i 无) |
| 仅 h5i 有 | **26 只** |

**为什么不做"整天替换"**: 那 26 只里 **25 只正是引擎覆盖缺口的真实新股**
(`001232 嘉立创`、`688825 长鑫科技`、`688836 宇树科技` …)，另 1 只 `688835 高凯技术`
经 akshare 确认同样在市。整天替换会**删掉 26 行真实数据** —— 正是用户明确禁止的。
故本脚本的策略是 **补齐而非替换**：用引擎重建当天截面，再把这 26 行**原样并入**。

做法
----
1. 从引擎取 09-04 全市场(权威口径: volume=股 / turnover=%，见 engine_bars_sync 的映射说明);
2. 取出 h5i 当天"仅 h5i 有"的 26 行，**保留其行情值**，只把 `turnover` 一并做 ×100 的单位订正
   （当天同一写入方产生的同一单位错误；不这样做会让同一天内出现两种口径）;
3. 合并 → 断言若干门禁 → 用官方变更 API `plan_replace_range` **原子替换该日区间**;
4. 落盘前后各做一次独立复核。

**不删任何行**（合并集 ⊇ 原集合），且 **不改动 09-04 之外的任何日期**。

用法
  python scripts/fix_h5i_units_0904.py                 # 预演(默认): 建集 + 跑门禁 + 打印计划, 不落盘
  python scripts/fix_h5i_units_0904.py --apply         # 真正落盘(必须先备份 market.db)
  python scripts/fix_h5i_units_0904.py --verify-only   # 只复核当前 09-04 状态
退出码: 0=成功(或预演通过); 1=门禁未过; 2=环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

LAKE = os.environ.get("STOCKDB_ROOT", "").strip() or r"E:\A_stockDB"
PYBAO = os.environ.get("PYBAO_DIR", "").strip() or os.path.join(LAKE, "pybao")
PREFIXES = ("0*", "3*", "6*", "9*")

DAY = "2026-09-04"
DAY_DT = dt.datetime(2026, 9, 4)
NEXT_DT = dt.datetime(2026, 9, 5)
MIN_ENGINE_ROWS = 5000          # 09-04 实测 5486

_H5I_COLS = ["symbol", "date", "open", "high", "low", "close",
             "volume", "amount", "change_pct", "turnover"]


def _us(d: dt.datetime) -> int:
    return int(d.timestamp() * 1_000_000)


def _engine_day(day8: str) -> dict:
    if PYBAO not in sys.path:
        sys.path.insert(0, PYBAO)
    from stock_sdk import rd
    out = {}
    for pfx in PREFIXES:
        for r in rd.vals("日k", pfx, day8):
            out[str(r.get("code")).zfill(6)] = r
    return out


def _h5i_day(db, day: str):
    import pandas as pd
    df = db.sql(
        "SELECT symbol, ts, open, high, low, close, volume, amount, change_pct, turnover "
        f"FROM daily_bars WHERE CAST(ts AS DATE) = CAST('{day}' AS DATE)").to_pandas()
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    return df


def build(db) -> tuple:
    """构造 09-04 的目标数据集。返回 (duck 风格 DataFrame, 统计 dict)。"""
    import pandas as pd

    eng = _engine_day(DAY.replace("-", ""))
    if len(eng) < MIN_ENGINE_ROWS:
        raise SystemExit(f"[门禁] 引擎 09-04 仅 {len(eng)} 只 (< {MIN_ENGINE_ROWS}) —— 拒绝据此改写历史")

    cur = _h5i_day(db, DAY)
    cur_by = {r["symbol"]: r for _, r in cur.iterrows()}
    only_h5i = sorted(set(cur_by) - set(eng))

    rows = []
    for code, e in eng.items():
        rows.append({
            "symbol": code, "date": DAY_DT,
            "open": e.get("open"), "high": e.get("high"),
            "low": e.get("low"), "close": e.get("close"),
            "volume": e.get("volume"), "amount": e.get("amount"),
            "change_pct": e.get("pct_chg"),
            "turnover": e.get("turnover"),
        })
    # 保留"仅 h5i 有"的行: 行情值原样, 只把 turnover 做 ×100 的单位订正
    kept_turn_fixed = 0
    for code in only_h5i:
        r = cur_by[code]
        t = r["turnover"]
        t_new = None
        try:
            tf = float(t)
            if tf == tf:                      # 非 NaN
                t_new = tf * 100.0
                kept_turn_fixed += 1
        except (TypeError, ValueError):
            t_new = None
        rows.append({
            "symbol": code, "date": DAY_DT,
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "volume": r["volume"], "amount": r["amount"],
            "change_pct": r["change_pct"], "turnover": t_new,
        })

    df = pd.DataFrame(rows)[_H5I_COLS]
    for c in ("open", "high", "low", "close", "volume", "amount", "change_pct", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])

    stat = {"engine_n": len(eng), "h5i_before_n": int(len(cur)),
            "only_h5i_n": len(only_h5i), "only_h5i": only_h5i,
            "kept_turnover_fixed": kept_turn_fixed,
            "built_n": int(len(df)), "dup": int(df["symbol"].duplicated().sum())}
    return df, stat


def gates(df, db, stat: dict) -> list:
    """落盘前门禁。返回 [(名称, 是否通过, 说明)]。"""
    import pandas as pd
    g = []
    eng = _engine_day(DAY.replace("-", ""))
    by = {r["symbol"]: r for _, r in df.iterrows()}

    # 1) 不丢行: 构造集必须 ⊇ 原集合
    cur = _h5i_day(db, DAY)
    lost = sorted(set(cur["symbol"]) - set(df["symbol"])) if not cur.empty else []
    g.append(("不丢行(构造集 ⊇ 原集合)", not lost, f"丢失 {len(lost)}: {lost[:5]}"))

    # 2) 无重复
    g.append(("无重复 symbol", stat["dup"] == 0, f"重复 {stat['dup']}"))

    # 3) 覆盖引擎全部代码
    miss = sorted(set(eng) - set(df["symbol"]))
    g.append(("覆盖引擎全部代码", not miss, f"缺 {len(miss)}: {miss[:5]}"))

    # 4) 字段与引擎一致(价格严格; volume/amount 相对 1e-4)
    bad_p, bad_v, bad_a = [], [], []
    for code, e in eng.items():
        r = by.get(code)
        if r is None:
            bad_p.append(code); continue
        for f, ef in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close")):
            try:
                if abs(float(r[f]) - float(e[ef])) > 1e-9:
                    bad_p.append(f"{code}.{f}"); break
            except (TypeError, ValueError):
                bad_p.append(f"{code}.{f}"); break
        try:
            if abs(float(r["volume"]) - float(e["volume"])) > max(abs(float(e["volume"])), 1) * 1e-4:
                bad_v.append(code)
        except (TypeError, ValueError):
            bad_v.append(code)
        try:
            if abs(float(r["amount"]) - float(e["amount"])) > max(abs(float(e["amount"])), 1) * 1e-4:
                bad_a.append(code)
        except (TypeError, ValueError):
            bad_a.append(code)
    g.append(("价格与引擎逐只一致", not bad_p, f"不符 {len(bad_p)}: {bad_p[:5]}"))
    g.append(("volume 与引擎一致(股)", not bad_v, f"不符 {len(bad_v)}: {bad_v[:5]}"))
    g.append(("amount 与引擎一致(元)", not bad_a, f"不符 {len(bad_a)}: {bad_a[:5]}"))

    # 5) turnover 量纲: 与引擎抽样比应为同量纲
    import random
    samp = random.sample(sorted(eng), min(50, len(eng)))
    off = []
    for c in samp:
        e, r = eng[c], by.get(c)
        try:
            if e.get("turnover") and float(r["turnover"]) / float(e["turnover"]) not in (0.0,) \
               and abs(float(r["turnover"]) / float(e["turnover"]) - 1.0) > 0.02:
                off.append(c)
        except (TypeError, ValueError):
            off.append(c)
    g.append(("turnover 与引擎同量纲(%)", not off, f"抽样不符 {len(off)}: {off[:5]}"))
    return g


def verify(db) -> dict:
    """独立复核 09-04 的当前状态(不依赖本脚本的构造过程)。"""
    cur = _h5i_day(db, DAY)
    out = {"h5i_n": int(len(cur))}
    if cur.empty:
        return out
    eng = _engine_day(DAY.replace("-", ""))
    by = {r["symbol"]: r for _, r in cur.iterrows()}
    inter = sorted(set(by) & set(eng))
    out["engine_n"] = len(eng)
    out["intersection"] = len(inter)
    out["only_h5i"] = sorted(set(by) - set(eng))
    out["missing_from_h5i"] = sorted(set(eng) - set(by))

    bad_v, bad_t, bad_p = [], [], []
    for c in inter:
        e, r = eng[c], by[c]
        try:
            if abs(float(r["volume"]) - float(e["volume"])) > max(abs(float(e["volume"])), 1) * 1e-4:
                bad_v.append(c)
        except (TypeError, ValueError):
            bad_v.append(c)
        for f, ef in (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close")):
            try:
                if abs(float(r[f]) - float(e[ef])) > 1e-9:
                    bad_p.append(c); break
            except (TypeError, ValueError):
                bad_p.append(c); break
        try:
            if e.get("turnover") and abs(float(r["turnover"]) / float(e["turnover"]) - 1.0) > 0.02:
                bad_t.append(c)
        except (TypeError, ValueError):
            bad_t.append(c)
    out.update({"volume_mismatch": len(bad_v), "price_mismatch": len(bad_p),
                "turnover_mismatch": len(bad_t),
                "volume_mismatch_sample": bad_v[:8], "turnover_mismatch_sample": bad_t[:8]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认预演）")
    ap.add_argument("--verify-only", action="store_true", help="只复核当前状态")
    args = ap.parse_args()

    import h5i_db
    import h5i_sync as H
    db = h5i_db.Database(H.H5I_PATH)
    try:
        if args.verify_only:
            print(json.dumps(verify(db), ensure_ascii=False, indent=2))
            return 0

        print("=" * 78)
        print(f"修复 h5i daily_bars {DAY} 的存量单位损坏（P1-H5IUNIT-0904）")
        print("=" * 78)
        print("\n  --- 修复前 ---")
        before = verify(db)
        print(json.dumps(before, ensure_ascii=False, indent=2)[:1200])

        df, stat = build(db)
        print("\n  --- 构造集 ---")
        print(json.dumps(stat, ensure_ascii=False, indent=2))

        print("\n  --- 落盘前门禁 ---")
        gs = gates(df, db, stat)
        ok = True
        for name, passed, note in gs:
            print(f"    [{'PASS' if passed else 'FAIL'}] {name}" + ("" if passed else f"  {note}"))
            ok = ok and passed
        if not ok:
            print("\n[FAIL] 门禁未过 —— 拒绝落盘")
            return 1

        table = H._df_to_h5i_table(df)          # 复用生产写入路径建表, 保证 schema 一致
        plan = db.plan_replace_range("daily_bars", _us(DAY_DT), _us(NEXT_DT),
                                     table, f"fix P1-H5IUNIT-0904 {DAY}")
        print("\n  --- 变更计划 ---")
        print("   ", json.dumps(plan.summary, ensure_ascii=False, default=str))

        if not args.apply:
            plan.discard()
            print("\n(--预演: 计划已 discard, 未落盘; 加 --apply 落盘)")
            return 0

        plan.apply()
        print("\n  [已落盘] plan applied")
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    # 落盘后复核(新连接)
    db2 = h5i_db.Database(H.H5I_PATH)
    try:
        print("\n  --- 修复后(独立复核) ---")
        after = verify(db2)
        print(json.dumps(after, ensure_ascii=False, indent=2)[:1400])
        try:
            v = db2.verify("daily_bars")
            print("\n  db.verify('daily_bars') =", json.dumps(v, ensure_ascii=False, default=str)[:300])
        except Exception as e:  # noqa: BLE001
            print("\n  db.verify 不可用:", type(e).__name__, str(e)[:120])
        residual = (after.get("volume_mismatch", 0) + after.get("price_mismatch", 0)
                    + after.get("turnover_mismatch", 0) + len(after.get("missing_from_h5i") or []))
        print(f"\n  残余不一致: {residual}  (仅 h5i 有 {len(after.get('only_h5i') or [])} 只 —— 按设计保留)")
        return 0 if residual == 0 else 1
    finally:
        try:
            db2.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())

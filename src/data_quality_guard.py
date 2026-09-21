# -*- coding: utf-8 -*-
"""日K 入库前的**结构校验**（路线图 #5 的第一刀 —— 入库哨兵）。

背景（为什么先做这个）
----------------------
2026-09-04 事故: 406 只 `volume` 被写成「手」（比「股」小 100 倍）、5172 条 `turnover`
写成小数，**静默**进入 h5i daily_bars，直到 09-21 换源审计才被发现（登记册 P1-H5IUNIT-0904）。
当时的防线只有 **dtype 约束**（Int64->Float64 被 h5i 拒绝过一次）—— 但 dtype 管不了
**值域与关系**: 100 倍错的 volume 依然是合法 float64。

本模块提供一组**与来源无关**的结构检查。所谓"结构"= 任何来源、任何量纲下都不可能合法的行:

  · ohlc_relations   high < max(open,close,low) 或 low > min(open,close,high)
  · positive_prices  open/high/low/close 任一 <= 0
  · nonneg_volume    volume < 0
  · nonneg_amount    amount < 0
  · core_nan         核心列(open/high/low/close/volume/amount)有 NaN

**刻意不查**的东西（各有明确理由）:
  · `change_pct`/`turnover` 的 NaN —— 本仓历史上"镜像未提供 turnover 而留空"是**合法**情形;
  · volume 的**量纲**（手 vs 股）—— 没有参照源时无法绝对判定, 留给**对账类**工具
    （`audit_h5i_vs_engine_units.py` 那种有第二数据源的检查）;
  · 时间戳单调性 —— 上层 `append_daily_bars` 已按 date 过滤且只追加, 属既有契约。

用法
----
  from data_quality_guard import validate_daily_bars
  r = validate_daily_bars(df)        # r = {ok, bad_rows, checks:{...}, sample:[...]}
  python src/data_quality_guard.py   # 对 h5i 最近 N 个交易日做基线扫描
"""
from __future__ import annotations

import pandas as pd

CORE = ("open", "high", "low", "close", "volume", "amount")
_TOL = 1e-9


def validate_daily_bars(df: pd.DataFrame, tol: float = _TOL) -> dict:
    """对 duck 风格 df 做结构校验。返回 {ok, bad_rows, checks, sample}。"""
    out: dict = {"ok": True, "bad_rows": 0, "checks": {}, "sample": []}
    if df is None or len(df) == 0:
        return out
    missing = [c for c in CORE if c not in df.columns]
    if missing:
        return {"ok": False, "bad_rows": len(df), "error": f"缺核心列: {missing}",
                "checks": {}, "sample": []}

    bad = pd.Series(False, index=df.index)

    def _add(name: str, mask: pd.Series, desc: str) -> None:
        n = int(mask.sum())
        out["checks"][name] = n
        if n:
            out["ok"] = False
            bad[:] = bad | mask
            syms = df.loc[mask, "symbol"].astype(str).tolist() if "symbol" in df.columns else \
                [str(i) for i in df.index[mask]]
            out["sample"].append({"check": name, "n": n, "desc": desc,
                                  "symbols": syms[:6]})

    # 1) OHLC 关系: high 必须 >= 所有价格; low 必须 <= 所有价格
    m = (df["high"] < df[["open", "close", "low"]].max(axis=1) - tol) | \
        (df["low"] > df[["open", "close", "high"]].min(axis=1) + tol)
    _add("ohlc_relations", m.fillna(False), "high < max(open,close,low) 或 low > min(open,close,high)")

    # 2) 正价格
    m = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    _add("positive_prices", m.fillna(False), "open/high/low/close 存在 <= 0")

    # 3) 非负量
    m = df["volume"] < 0
    _add("nonneg_volume", m.fillna(False), "volume < 0")

    # 4) 非负额
    m = df["amount"] < 0
    _add("nonneg_amount", m.fillna(False), "amount < 0")

    # 5) 核心 NaN（change_pct/turnover 允许 NaN —— 见模块 docstring）
    # 注: CORE 是元组, pandas 会把元组当**单个列键**(MultiIndex) —— 必须转 list。
    m = df[list(CORE)].isna().any(axis=1)
    _add("core_nan", m, "核心列(open/high/low/close/volume/amount)有 NaN")

    out["bad_rows"] = int(bad.sum())
    return out


def gate_decision(r: dict) -> dict:
    """入库闸门的判定：结构违规 ⇒ **拒绝写入**。

    单独抽出来是为了 CI 可测 —— `h5i_sync.append_daily_bars` 需要 h5i_db(CI 没有),
    而判定逻辑本身不需要。语义:
      · 任何结构违规都 **fail-closed**: 宁可这一天写不进去, 也不让 open=0 之类的
        行静默进入因子计算(实测存量里就有 7 行这样的零填充占位行)。
      · 返回体带 bad_rows / checks / sample, 让上层能打印出**具体是哪些符号**,
        而不是一句"校验失败"。
    """
    if r.get("ok"):
        return {"reject": False, "ok": True}
    if r.get("error"):
        return {"reject": True, "ok": False, "error": r["error"]}
    checks = {k: v for k, v in (r.get("checks") or {}).items() if v}
    sample = [s.get("symbols") for s in (r.get("sample") or [])]
    return {"reject": True, "ok": False,
            "bad_rows": r.get("bad_rows"), "checks": checks, "sample": sample,
            "error": "结构校验未通过: " + ", ".join(
                f"{k}={v}行" for k, v in checks.items())}


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    import h5i_sync as H  # noqa: E402

    db = H._open_h5i()
    if db is None:
        print("h5i 库不可用")
        raise SystemExit(2)
    try:
        days = db.sql("SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars "
                      "ORDER BY d DESC LIMIT 10").to_pandas()["d"].tolist()
        print("=" * 76)
        print("h5i daily_bars 结构基线扫描（最近 %d 个交易日）" % len(days))
        print("=" * 76)
        all_bad = 0
        for d in days:
            iso = str(d)[:10]
            df = db.sql(
                "SELECT symbol, open, high, low, close, volume, amount, change_pct, turnover "
                f"FROM daily_bars WHERE CAST(ts AS DATE) = CAST('{iso}' AS DATE)").to_pandas()
            r = validate_daily_bars(df)
            all_bad += r["bad_rows"]
            flag = "OK " if r["ok"] else "BAD"
            print(f"  [{flag}] {iso}  rows={len(df):5d}  bad={r['bad_rows']:4d}  {r['checks']}")
            for s in r["sample"]:
                print(f"         · {s['check']}: {s['n']} 行  {s['symbols']}")
        print(f"\n  合计坏行: {all_bad}")
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

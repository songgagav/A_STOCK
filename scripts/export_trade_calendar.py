# -*- coding: utf-8 -*-
"""导出交易日历供跨项目消费者（veighna_sim 模拟盘）无依赖读取.

为什么需要这个脚本
  A_stock_rotation `realtime_engine._plan_is_formal` 判定"盘后正式 plan"的下界是
  **前一交易日** 16:00（`_prev_trade_day`，基于 daily_bars 实际数据，跨周末/节假日正确），
  而 veighna_sim 跑在自己的 venv（无 h5i_db / duckdb 依赖），无法调用该函数。

  若消费者改用"前一**日历日**"近似，跨周末会判错。实测反例：
    消费日 D = 2026-09-07(周一)，候选 plan = data/drl/20260905/target_plan.json
      generated_at = 2026-09-05 15:46:50
    真实引擎: 前一交易日 = 2026-09-04(周五) -> 窗口 [09-04 16:00, 09-07 00:00) -> **通过**
    日历日近似: 下界 = 2026-09-06 16:00 -> 15:46 < 下界 -> **误拒**
  两者会喂出不同的池 —— 正是 P0-2 一致性偏差的成因类别。

  因此本脚本把引擎同源的交易日序列落成 JSON，消费者按同一序列取"前一交易日"，
  做到口径同源而非近似复刻。

数据源与 `_prev_trade_day` 的 h5i 回退分支完全一致：
  `h5i_bar_store.H5iBarStore().trading_days()`

用法（必须用持有 h5i_db 的解释器）
  & "$env:APPDATA\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe" \
      scripts\export_trade_calendar.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, "src"))

OUT_FP = os.path.join(_BASE, "data", "trade_calendar.json")


def _trading_days():
    """返回 (交易日列表, 来源说明)。与 _prev_trade_day 同源顺序：duck -> h5i。"""
    days: list[str] = []
    src = ""
    try:
        from db import duck_available  # type: ignore
        if duck_available():
            from db import StockDB  # type: ignore
            db = StockDB()
            try:
                rows = db._conn().execute(
                    "SELECT DISTINCT date FROM daily_bars ORDER BY date").fetchall()
                days = [str(r[0])[:10] for r in rows if r[0] is not None]
                src = "duckdb:daily_bars"
            finally:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    if not days:
        from h5i_bar_store import H5iBarStore  # type: ignore
        s = H5iBarStore()
        try:
            days = [str(x)[:10] for x in s.trading_days()]
            src = "h5i:daily_bars.trading_days"
        finally:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return sorted(set(days)), src


def main() -> int:
    days, src = _trading_days()
    if not days:
        print("[export_trade_calendar] 未取到任何交易日, 放弃写出", file=sys.stderr)
        return 2
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": src,
        "n": len(days),
        "first": days[0],
        "last": days[-1],
        "trading_days": days,
    }
    os.makedirs(os.path.dirname(OUT_FP), exist_ok=True)
    with open(OUT_FP, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[export_trade_calendar] {OUT_FP} n={len(days)} "
          f"{days[0]}~{days[-1]} src={src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

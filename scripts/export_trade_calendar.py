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

    # [2026-09-21 修 —— 本脚本曾经"改坏过生产"]
    # 这份 JSON 同时承载**两套契约**, 而 `days` 是 `trading_calendar.is_trading_day` 的
    # 权威集合 —— 后者必须能判断**今天/未来**: 其实现就是 `return norm in cal`。
    # 而本脚本的 days 来自 h5i(只有"已经有数据的日子"), 于是**任何晚于最后数据日的日期
    # 都会被判成非交易日, 包括"今天"**。
    # 后果极隐蔽: 守护把交易日当节假日 —— 不跑盘前健康检查、08:30 不启动盘中引擎、
    # 收盘窗口只跑 --maint。看起来只是"今天没事做"。
    # 故此处**取并集, 绝不缩小**: 既有内容(通常来自 AKShare 官方日历, 含全年未来日期)
    # 原样保留, 只在其上补充数据同源的交易日。
    existing = {}
    try:
        if os.path.exists(OUT_FP):
            with open(OUT_FP, encoding="utf-8-sig") as f:
                existing = json.load(f) or {}
    except Exception:
        existing = {}
    old_set = {str(x) for x in (existing.get("days") or [])}
    new_set = {d.replace("-", "") for d in days}
    union = sorted(old_set | new_set)
    if len(union) < len(old_set):
        print("[export_trade_calendar] 拒绝写出: 新的 days 会小于既有集合", file=sys.stderr)
        return 3

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": f"{src} | days=union(既有 {len(old_set)} ∪ 本次 {len(new_set)})",
        "n": len(union),
        "first": union[0],
        "last": union[-1],
        # [2026-09-19 修] **双键**, 因为本文件有**两个既有消费方**, 契约不同:
        #   1) `days`: 'YYYYMMDD' 无分隔 —— 项目原有契约。
        #      `src/vnpy_backtest._calendar_from_static_file()` 读 `days` 并自行归一化;
        #      `tests/test_calendar_fallback.py::test_static_calendar_file_is_usable`
        #      断言其存在且覆盖研究区间。
        #   2) `trading_days`: 'YYYY-MM-DD' —— 本会话新增的 veighna_sim 消费方
        #      (`veighna_sim/loop/astock_source.py::trading_days()`)。
        # 历史教训: 本脚本最初**只写 trading_days**, 而目标路径正是 (1) 所读的文件 ——
        # 等于用不兼容的 schema 覆盖了项目契约文件, 使日历回退链失效(该用例由通过变
        # 为空列表)。故此处**必须**同时写两个键; 请勿删掉其中任何一个。
        "days": union,
        "trading_days": days,
    }
    # 保留 `updated`(trading_calendar 用它判缓存年龄); 缺了它, refresh() 每次都会尝试
    # 联网重抓, 无网时还会把缓存标成 stale。
    if existing.get("updated"):
        payload["updated"] = existing["updated"]
    os.makedirs(os.path.dirname(OUT_FP), exist_ok=True)
    with open(OUT_FP, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[export_trade_calendar] {OUT_FP} n={len(union)} "
          f"{union[0]}~{union[-1]} src={src} "
          f"keys=days+trading_days (days=并集: 既有 {len(old_set)} -> {len(union)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

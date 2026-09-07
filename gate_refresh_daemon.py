# -*- coding: utf-8 -*-
"""门控 IC 刷新守护 (后台常驻, 替代外部定时任务).

行为: 每 interval_min 分钟检查一次, 满足全部条件时执行一次盘后刷新:
  - 是交易日 (trading_calendar.is_trading_day)
  - 本地时间在 [16:05, 16:50] 窗口 (收盘后数据可用)
  - 当日尚未刷新 (以 data/gate_refresh_last.json 记录日期去重)

刷新内容 = refresh_gate_ic.refresh(days=121): 融合因子 IC 缓存 / 门控快照 /
带门控净值账本 (ledger 需环境变量 GATE_LIVE_FROM 才会记录).

用法:
  python gate_refresh_daemon.py            # 常驻后台
  python gate_refresh_daemon.py --once     # 立即执行一次 (忽略窗口)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, date

_BASE = os.path.dirname(os.path.abspath(__file__))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

DATA_DIR = os.path.join(_BASE, "data")
MARK = os.path.join(DATA_DIR, "gate_refresh_last.json")
START_HHMM = 16 * 60 + 5
END_HHMM = 16 * 60 + 50
_INTERVAL_MIN = 1


def _mark_done(day: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MARK, "w", encoding="utf-8") as f:
        json.dump({"date": day, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f)


def _already_done(day: str) -> bool:
    if not os.path.exists(MARK):
        return False
    try:
        with open(MARK, encoding="utf-8") as f:
            return json.load(f).get("date") == day
    except Exception:
        return False


def _trading(day: date) -> bool:
    try:
        from trading_calendar import is_trading_day
        return bool(is_trading_day(day))
    except Exception:
        return day.weekday() < 5


def run_refresh() -> bool:
    """执行一次刷新; 返回是否成功."""
    from refresh_gate_ic import refresh
    print(f"[gate_daemon] {datetime.now():%Y-%m-%d %H:%M:%S} 开始刷新", flush=True)
    res = refresh(days=121)
    if res.get("ok"):
        gp = res.get("gate_plan") or {}
        print(f"[gate_daemon] 刷新 OK regime={gp.get('regime')} "
              f"exposure={gp.get('exposure_mult')}", flush=True)
        return True
    print(f"[gate_daemon] 刷新失败: {res.get('error')}", flush=True)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="门控 IC 刷新守护")
    ap.add_argument("--once", action="store_true", help="立即执行一次并退出")
    ap.add_argument("--interval-min", type=int, default=_INTERVAL_MIN)
    args = ap.parse_args()

    if args.once:
        ok = run_refresh()
        _mark_done(date.today().strftime("%Y%m%d"))
        sys.exit(0 if ok else 1)

    print(f"[gate_daemon] 常驻启动 窗口=16:05-16:50 交易日, 间隔 {args.interval_min} 分钟",
          flush=True)
    while True:
        now = datetime.now()
        hhmm = now.hour * 60 + now.minute
        today = date.today()
        if _trading(today) and START_HHMM <= hhmm <= END_HHMM and not _already_done(today.strftime("%Y%m%d")):
            try:
                if run_refresh():
                    _mark_done(today.strftime("%Y%m%d"))
                    print("[gate_daemon] 今日刷新完成", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[gate_daemon] 刷新异常 {type(e).__name__}: {e}", flush=True)
        time.sleep(max(30, int(args.interval_min) * 60))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""估值覆盖率哨兵守护 (后台常驻, 替代外部定时任务).

背景
    补丁退役需要"哨兵每日运行"的连续证据(见 docs/patch-retirement-watch.md)。
    但本机没有 Windows 计划任务, `run_daily` 靠手工启动 ⇒ 哨兵不会自动跑。
    本守护把这一步放进**后台常驻脚本**(ops/start_obs_stack.ps1 拉起), 不需要新建
    定时任务, 也不需要改造 `run_daily` 的调用方式。

行为
    每 interval_min 分钟检查一次, 到点且当日尚未执行时, 调用
    `scheduler_entry._pe_patch_and_sentinel()`——即"哨兵体检 + 仅当报出缺口时才补
    pe_ttm 补丁"(复用已测过的编排, 不在此处重复实现)。

    触发条件 (同时满足):
      - 本地时间 >= `--at` (默认 18:30, 收盘后数据已落库)
      - 当日尚未执行 (以 data/sentinel_last.json 记录日期去重)
      - 可选: 仅交易日 (`--trading-days-only`, 默认**每天**都跑, 作为心跳证据)

    启动时会立即补跑一次(若当日尚未执行), 这样重启后台脚本就能补上漏掉的当天。

    连续失败 3 次后放弃当天(写标记 + 记日志), 避免每 5 分钟重试刷屏。

用法
  python src/sentinel_daemon.py                 # 常驻后台 (由 start_obs_stack.ps1 拉起)
  python src/sentinel_daemon.py --once          # 立即体检一次并退出 (忽略时间/去重)
  python src/sentinel_daemon.py --at 19:00 --trading-days-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

DATA_DIR = os.path.join(_BASE, "data")
MARK = os.path.join(DATA_DIR, "sentinel_last.json")
DEFAULT_AT = "18:30"
_MAX_FAILS = 3


def _hhmm(s: str) -> int:
    """'18:30' -> 1110 (分钟)."""
    h, _, m = str(s).partition(":")
    return int(h) * 60 + int(m or 0)


def _is_trading(day: date) -> bool:
    try:
        from trading_calendar import is_trading_day
        return bool(is_trading_day(day))
    except Exception:  # noqa: BLE001
        return day.weekday() < 5


def _read_mark() -> dict:
    if not os.path.exists(MARK):
        return {}
    try:
        with open(MARK, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_mark(day: str, **extra) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    rec = {"date": day, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    rec.update(extra)
    with open(MARK, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def should_run(now: datetime, mark: dict, at_min: int, *,
               trading_only: bool = False, is_trading: bool = True) -> tuple[bool, str]:
    """是否该执行. 返回 (bool, 原因). 纯函数, 便于测试."""
    day = now.strftime("%Y%m%d")
    if mark.get("date") == day:
        return False, "今日已执行"
    if now.hour * 60 + now.minute < at_min:
        return False, f"未到触发时间({at_min // 60:02d}:{at_min % 60:02d})"
    if trading_only and not is_trading:
        return False, "非交易日"
    return True, "到点且未执行"


def run_once() -> tuple[bool, str]:
    """执行一次"哨兵体检 + 按需补丁". 返回 (是否真正跑完, 摘要).

    注意: 哨兵**报出 CRITICAL 也算跑完**(rc=1 是有效体检结论, 不是执行失败);
    只有子进程起不来 / 异常 / rc<0 才算失败。
    """
    try:
        from scheduler_entry import _pe_patch_and_sentinel
    except Exception as e:  # noqa: BLE001
        return False, f"导入 scheduler_entry 失败: {type(e).__name__}: {e}"
    try:
        res = _pe_patch_and_sentinel() or {}
    except Exception as e:  # noqa: BLE001
        return False, f"体检异常: {type(e).__name__}: {e}"
    sen = res.get("sentinel") or {}
    rc = sen.get("rc")
    if rc is None or int(rc) < 0:
        return False, f"哨兵未产出 (rc={rc}, err={sen.get('error')})"
    bf = res.get("backfill") or {}
    summary = f"哨兵 rc={rc} | {str(sen.get('summary') or '')[:80]}"
    if bf.get("skipped"):
        summary += f" | 补丁: {bf['skipped']}"
    elif bf.get("rc") is not None:
        summary += f" | 补丁 rc={bf['rc']}"
    return True, summary


def main() -> None:
    ap = argparse.ArgumentParser(description="估值覆盖率哨兵守护")
    ap.add_argument("--at", default=DEFAULT_AT, help="每日触发时间 HH:MM (默认 18:30)")
    ap.add_argument("--interval-min", type=int, default=5, help="检查间隔(分钟)")
    ap.add_argument("--trading-days-only", action="store_true",
                    help="只在交易日执行(默认每天都跑, 作为心跳证据)")
    ap.add_argument("--once", action="store_true", help="立即体检一次并退出")
    a = ap.parse_args()
    # 在 main 内切工作目录(不在 import 时切, 保证本模块可被测试安全导入)
    os.chdir(_BASE)

    at_min = _hhmm(a.at)
    today = date.today().strftime("%Y%m%d")

    if a.once:
        ok, msg = run_once()
        print(f"[sentinel_daemon] --once {'OK' if ok else 'FAIL'}: {msg}", flush=True)
        if ok:
            _write_mark(today, note="--once")
        sys.exit(0 if ok else 1)

    print(f"[sentinel_daemon] 常驻启动 触发={a.at} 检查间隔={a.interval_min}分 "
          f"{'仅交易日' if a.trading_days_only else '每日'}", flush=True)

    fails = 0
    while True:
        try:
            now = datetime.now()
            mark = _read_mark()
            ok_to_run, why = should_run(now, mark, at_min,
                                        trading_only=a.trading_days_only,
                                        is_trading=_is_trading(now.date()))
            if ok_to_run:
                print(f"[sentinel_daemon] {now:%Y-%m-%d %H:%M:%S} {why}, 开始体检",
                      flush=True)
                ok, msg = run_once()
                if ok:
                    print(f"[sentinel_daemon] 完成: {msg}", flush=True)
                    _write_mark(now.strftime("%Y%m%d"), summary=msg[:300])
                    fails = 0
                else:
                    fails += 1
                    print(f"[sentinel_daemon] 第 {fails}/{_MAX_FAILS} 次失败: {msg}",
                          flush=True)
                    if fails >= _MAX_FAILS:
                        print("[sentinel_daemon] 放弃当天(避免重试刷屏), 次日再试",
                              flush=True)
                        _write_mark(now.strftime("%Y%m%d"),
                                    error=msg[:300], gave_up=True)
                        fails = 0
        except Exception as e:  # noqa: BLE001
            print(f"[sentinel_daemon] 循环异常(不退出): {type(e).__name__}: {e}",
                  flush=True)
        time.sleep(max(60, int(a.interval_min) * 60))


if __name__ == "__main__":
    main()

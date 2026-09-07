# ============================================================
# scheduler_entry.py -- Windows计划任务调度入口
# 作用: 由"任务计划程序"每交易日15:05触发, 独立进程调用run_daily
#       显式设置sys.path, 保证从任意工作目录启动都能import到模块
# ============================================================

import os
import sys

# 显式把模块目录加入搜索路径(不依赖任务计划的启动目录)
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)   # 工作目录统一到模块根, 确保相对路径输出稳定

import json
import time
from datetime import date, datetime

from run_daily import run_daily

LOG_FILE = os.path.join(_BASE, "logs", "run_daily.log")


def is_trading_day(day: date) -> bool:
    """A股交易日判定(含节假日, 走权威交易日历)."""
    try:
        from trading_calendar import is_trading_day as _tc_day
        return _tc_day(day)
    except Exception:
        return day.weekday() < 5


def log(msg: str):
    """print 同时追加到本地日志(计划任务环境无shell重定向)."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _gate_ic_refresh() -> None:
    """盘后刷新: 融合因子 IC 缓存 + 门控快照 + 带门控净值账本 (2026-09-07).

    仅做盘后数据刷新, 不触碰交易; 失败只记录日志, 不影响当日调度结果.
    """
    try:
        from refresh_gate_ic import refresh
        res = refresh(days=121)
        if res.get("ok"):
            gp = res.get("gate_plan") or {}
            ledger = res.get("ledger") or {}
            log(f"门控IC刷新 OK: regime={gp.get('regime')} "
                f"exposure={gp.get('exposure_mult')} 账本={ledger.get('date') or ledger.get('reason') or 'skip'}")
        else:
            log(f"门控IC刷新失败: {res.get('error')}")
    except Exception as e:  # noqa: BLE001
        log(f"门控IC刷新异常: {type(e).__name__}: {e}")


def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"启动 全A轮动 盘后调度")

    today = date.today()
    if not is_trading_day(today):
        # 周末/节假日: 仅跑维护管道(数据拉取+模型训练), 不做盘前决策/盘中执行.
        log(f"非交易日 {today}, 运行维护管道(mode=maint)")
        report = run_daily(day=today.strftime("%Y-%m-%d"), mode="maint")
        log(f"维护管道完成 状态={report.get('status')}")
        _gate_ic_refresh()
        return

    day = today.strftime("%Y-%m-%d")
    out = os.path.join(_BASE, "data", "daily", day.replace("-", ""))
    os.makedirs(out, exist_ok=True)

    # 幂等: 若当日回执已存在且状态OK, 跳过(避免重复下单)
    summary_path = os.path.join(out, "daily_summary.json")
    if os.path.exists(summary_path):
        try:
            import json
            with open(summary_path, encoding="utf-8") as f:
                if json.load(f).get("status") == "OK":
                    log(f"当日回执已存在且OK: {summary_path}, 跳过本次执行")
                    _gate_ic_refresh()
                    return
        except Exception:
            pass

    t0 = time.time()
    report = run_daily(day=day)
    cost = round(time.time() - t0, 1)

    status = report.get("status", "UNKNOWN")
    log(f"耗时{cost}s 状态={status}")
    if status == "OK":
        log("选股: " + str(report["steps"]["select"]["top"]))
        log("净值: " + str(report["steps"]["paper"].get("equity")))
        log("回执: " + str(report.get("输出文件")))
    else:
        log("执行未成功(可查看回执detail): " + str(status))
        sys.exit(1)
    _gate_ic_refresh()


if __name__ == "__main__":
    main()
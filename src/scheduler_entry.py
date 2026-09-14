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
# 注: os.chdir 放在 main() 里而不是 import 时 —— 计划任务启动时需要统一工作目录,
# 但 import 时切会污染测试进程的 CWD(模块需要可被安全导入)。

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


def _pe_patch_and_sentinel(days: int = 30, timeout: int = 3600) -> dict:
    """盘后: 估值覆盖率哨兵 + pe_ttm 缺口兜底补丁 (2026-09-14).

    背景: h5i `valuation` 主表在 2025-08-04 ~ 2026-09-03 窗口里逐日入库没有产出
    完整行, 由 `valuation_backfill` 的"近似行"补齐(按设计 `pe_ttm` 置空) ⇒ `ep` 因子
    在这些日期整体失效(2026-03-05 主表覆盖率仅 0.4%)。补丁 `data/pit/pe_patch/*.parquet`
    是**过渡兜底**, 根本修复是让近似行也计算 PE(见 docs/pit-valuation.md 第 20 条)。

    本步骤把兜底接进每日调度, 但放在收盘管道**之后**(不阻塞选股):
      1) 先跑哨兵体检(校验主表**原始**覆盖率, 补丁不掩盖上游问题);
      2) 只有哨兵报出缺口时才跑 `backfill_pe_ttm.py --resume` 增量补, 且带 timeout。
    补丁变化会让 `vnpy_backtest._data_version()` 变 ⇒ PIT 选股缓存自动失效, 无需手工清。
    环境变量 `PE_PATCH_AUTO=0` 可关掉自动补(只体检不补)。
    """
    res: dict = {"sentinel": None, "backfill": None}
    py = sys.executable
    try:
        import subprocess
        sc = os.path.join(_BASE, "scripts", "valuation_coverage_sentinel.py")
        r = subprocess.run([py, sc, "--days", "20"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-1:]
        res["sentinel"] = {"rc": r.returncode, "summary": tail[0] if tail else ""}
        log(f"估值覆盖率哨兵 rc={r.returncode}: {tail[0] if tail else '(无输出)'}")
    except Exception as e:  # noqa: BLE001
        res["sentinel"] = {"rc": -1, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        log(f"估值覆盖率哨兵异常: {type(e).__name__}: {str(e)[:160]}")
        return res

    if res["sentinel"].get("rc", -1) == 0:
        res["backfill"] = {"skipped": "覆盖率正常, 无需兜底"}
        return res
    if os.environ.get("PE_PATCH_AUTO", "1") in ("", "0"):
        res["backfill"] = {"skipped": "PE_PATCH_AUTO=0 (只体检不补)"}
        return res
    try:
        import subprocess
        bf = os.path.join(_BASE, "scripts", "backfill_pe_ttm.py")
        r = subprocess.run([py, bf, "--days", str(int(days)), "--resume"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=int(timeout))
        tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-2:]
        res["backfill"] = {"rc": r.returncode, "tail": tail}
        log(f"pe_ttm 兜底补丁 rc={r.returncode}: {' | '.join(tail)[:200]}")
    except Exception as e:  # noqa: BLE001
        res["backfill"] = {"rc": -1, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        log(f"pe_ttm 兜底补丁异常(不阻断): {type(e).__name__}: {str(e)[:160]}")
    return res


def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"启动 全A轮动 盘后调度")
    os.chdir(_BASE)   # 工作目录统一到模块根(计划任务启动时 CWD 不可控)

    today = date.today()
    if not is_trading_day(today):
        # 周末/节假日: 仅跑维护管道(数据拉取+模型训练), 不做盘前决策/盘中执行.
        log(f"非交易日 {today}, 运行维护管道(mode=maint)")
        report = run_daily(day=today.strftime("%Y-%m-%d"), mode="maint")
        log(f"维护管道完成 状态={report.get('status')}")
        _gate_ic_refresh()
        _pe_patch_and_sentinel()
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
    _pe_patch_and_sentinel()


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--pe-patch-only", action="store_true",
                     help="只跑估值覆盖率哨兵 + pe_ttm 兜底补丁(可单独挂计划任务)")
    _ap.add_argument("--days", type=int, default=30, help="pe 补丁回看窗口(天)")
    _ap.add_argument("--timeout", type=int, default=3600, help="补丁子进程超时(秒)")
    _a = _ap.parse_args()
    if _a.pe_patch_only:
        _r = _pe_patch_and_sentinel(days=_a.days, timeout=_a.timeout)
        log(f"pe 兜底结果: {_r}")
        sys.exit(0)
    main()


if __name__ == "__main__":
    main()
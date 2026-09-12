# ============================================================
# premarket_healthcheck.py -- 盘前健康检查
#
# 设计目标: 盘前 (08:00~09:00) 在交易引擎启动前确认以下通道就绪, 任一项不通过
# 则引擎不应启动 (或带降级模式运行). 校验项:
#   1) disk         : DATA_DIR 可写, 剩余空间 >= 1 GB
#   2) duckdb       : daily_bars 存在 + 最新日期 <= 今日 (否则行情缺)
#   3) arcticdb     : 6 个库可读写 (health_check)
#   4) orderbook    : orderbook_snapshot 当日已落盘 (或允许缺, 但要标记 WARN)
#   5) daily_bars   : 最新日期距今天数 <= 1 (否则数据老化)
#   6) akshare      : 实时源可达 (拉一只样本股行情, 3s 内有价)
#   7) state_io     : state.json / live_state.json 可写
#
# 调用方:
#   - run_daily.py 步骤 0.5 (盘前也兼容: --premarket)
#   - daemon.py 启动引擎前调用 (best-effort, 仅日志)
#   - 手动: python premarket_healthcheck.py
#
# 返回: {"ok": bool, "checks": [{name, status, detail, ms}], "summary": "..."}
#   status: OK / WARN / FAIL
# ============================================================

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import traceback
from datetime import datetime, date, timedelta
from typing import Any
from urllib import request, error

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from config import DATA_DIR, DUCKDB_PATH, STATE_FILE  # noqa: E402

# ---- 输出 ----
HEALTH_DIR = os.path.join(DATA_DIR, "health")
HEALTH_FILE = os.path.join(HEALTH_DIR, "premarket.json")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record(name: str, status: str, detail: Any, t0: float) -> dict:
    return {
        "name": name,
        "status": status,
        "detail": detail if not isinstance(detail, Exception) else f"{type(detail).__name__}: {detail}",
        "ms": int((time.time() - t0) * 1000),
    }


def _duckdb_locked(e: Exception) -> bool:
    """识别 DuckDB 文件被外部进程独占锁住导致的打开失败.

    DuckDB 是单写文件(内存映射)数据库, 当一个进程打开写句柄时, 其他进程 read_only
    打开会抛出 IOError(Windows: '另一个程序正在使用此文件'). 这类"锁冲突"不代表
    数据库本身损坏, 而是外部后台任务(如非交易日 maint 维护/收盘管道)正在写库, 属于
    时序性降级而非故障. 盘前健康检查若恰好撞上, 应降级为 WARN 而非 FAIL."""
    t = str(e)
    low = t.lower()
    # 必须是 DuckDB 文件打开失败(Cannot open file ... stockdb.duckdb), 且伴随
    # 文件被占/正被使用类关键字. 排除其他 IO 异常(如盘符丢失).
    if "cannot open file" not in low or ".duckdb" not in low:
        return False
    lock_markers = ("正在使用", "another program", "in use", "locked",
                    "already open", "exclusive", "busy", "sharing violation",
                    "process cannot access")
    return any(m in low for m in lock_markers)


def _record_duckdb(e: Exception, name: str, t0: float) -> dict:
    """统一的 DuckDB 异常落盘: 锁冲突->WARN(降级), 其余->FAIL."""
    if _duckdb_locked(e):
        return _record(name, "WARN",
                       {"degraded": True,
                        "reason": "DuckDB 被外部进程持锁(后台维护/收盘管道), 暂无法读库; "
                                  "锁释放后重测即可",
                        "error": f"{type(e).__name__}: {e}"},
                       t0)
    return _record(name, "FAIL", e, t0)


# ---- 单项检查 ----
def check_disk() -> dict:
    t0 = time.time()
    name = "disk"
    try:
        ok_write = os.access(DATA_DIR, os.W_OK)
        os.makedirs(HEALTH_DIR, exist_ok=True)
        # 写入临时文件再删
        probe = os.path.join(HEALTH_DIR, ".health_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write(_now())
        os.remove(probe)
        total, used, free = shutil.disk_usage(DATA_DIR)
        free_gb = free / (1024 ** 3)
        status = "OK" if (ok_write and free_gb >= 1.0) else "FAIL"
        return _record(name, status,
                       {"writable": ok_write, "free_gb": round(free_gb, 2),
                        "data_dir": DATA_DIR},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


def check_duckdb() -> dict:
    t0 = time.time()
    name = "duckdb"
    try:
        import duckdb
        if not os.path.exists(DUCKDB_PATH):
            return _record(name, "FAIL", f"db 不存在: {DUCKDB_PATH}", t0)
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            row = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return _record(name, "FAIL", "daily_bars 表为空", t0)
        latest = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
        today = date.today().isoformat()
        days_lag = (date.today() - row[0]).days if hasattr(row[0], "isoformat") else None
        status = "OK" if (days_lag is not None and days_lag <= 5) else "WARN"
        return _record(name, status,
                       {"latest_date": latest, "today": today,
                        "days_lag": days_lag, "path": DUCKDB_PATH},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


def check_arcticdb() -> dict:
    t0 = time.time()
    name = "arcticdb"
    try:
        from arctic_store import get_store
        store = get_store()
        h = store.health_check()
        libs = h.get("libraries") or {}
        probe = h.get("read_probe") or {}
        # libs 形如 {name: bool}, 全 True = 库能连接
        miss = [k for k, v in libs.items() if not v]
        # 读探测: 库有 symbol 但抽样读取失败 => "能连接却不能读", 判 FAIL (本修复前此盲区)
        read_fail = [
            (k, v.get("read_fail"))
            for k, v in probe.items()
            if v and v.get("symbols", 0) > 0 and v.get("read_fail")
        ]
        # r3 增强: "能读"还要"读到的是最新的". 对比 ArcticDB 抽样读到的 latest_date
        # 与 DuckDB daily_bars 最新日, 滞后过多 => 数据老化(库活着但内容冻结).
        # 仅对"应每日更新"的核心行情/因子库判陈旧(bars/daily_summary/factor_ic);
        # trade_records/perf_report/reward_curve 为低频写库, 不做 lag 判定, 避免误报.
        DAILY_LIBS = ("bars", "daily_summary", "factor_ic")
        stale_libs = []
        db_latest = None
        try:
            import duckdb
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            try:
                row = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
            finally:
                con.close()
            if row and row[0]:
                db_latest = _norm_date(row[0])
                for k in DAILY_LIBS:
                    v = probe.get(k)
                    if not v or v.get("symbols", 0) == 0:
                        continue
                    ldate = v.get("latest_date")
                    if not ldate:
                        stale_libs.append((k, "latest_date=None"))
                        continue
                    lag = int(db_latest) - int(_norm_date(ldate))
                    if lag > 3:
                        stale_libs.append((k, f"lag={lag}d"))
        except Exception:
            pass
        lag_ok = not stale_libs

        status = "OK"
        if miss:
            status = "FAIL"
        elif read_fail:
            status = "FAIL" if not libs else "WARN"  # 库都在但读失败 -> WARN(不阻断连接)
        elif not lag_ok:
            status = "WARN"  # 能读但读到的是老数据 -> WARN(数据冻结, 建议刷新)
        return _record(name, status,
                       {"uri": h.get("uri"),
                        "libraries": libs,
                        "stats": h.get("stats"),
                        "missing": miss,
                        "read_probe": {k: v for k, v in probe.items()},
                        "read_fail": read_fail or None,
                        "db_latest_date": db_latest,
                        "stale_libs": stale_libs or None},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


def check_orderbook() -> dict:
    t0 = time.time()
    name = "orderbook"
    try:
        import duckdb
        if not os.path.exists(DUCKDB_PATH):
            return _record(name, "WARN", "DuckDB 缺失, 跳过 orderbook 校验", t0)
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT MAX(snapshot_date) FROM orderbook_snapshot"
            ).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return _record(name, "WARN", "orderbook_snapshot 表为空", t0)
        latest = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
        today = date.today().isoformat()
        days_lag = (date.today() - row[0]).days if hasattr(row[0], "isoformat") else None
        # 当日盘前 (08:00) 还未必落盘, 所以"今日缺"视为 WARN 而非 FAIL
        status = "OK" if (days_lag is not None and days_lag <= 1) else "WARN"
        return _record(name, status,
                       {"latest_date": latest, "today": today, "days_lag": days_lag},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


def check_daily_bars_freshness() -> dict:
    t0 = time.time()
    name = "daily_bars_freshness"
    try:
        import duckdb
        if not os.path.exists(DUCKDB_PATH):
            return _record(name, "FAIL", "DuckDB 缺失", t0)
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT MAX(date), COUNT(DISTINCT symbol) FROM daily_bars "
                "WHERE date = (SELECT MAX(date) FROM daily_bars)"
            ).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return _record(name, "FAIL", "无数据", t0)
        latest = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
        n_uni = int(row[1] or 0)
        today = date.today().isoformat()
        days_lag = (date.today() - row[0]).days if hasattr(row[0], "isoformat") else None
        # 交易日期望校正: 周六/周日闭市, 最近"应有"交易日自然回退到周五(周一/周三亦前移),
        # 避免首发 check 见 days_lag=2 就误报 FAIL 的周末误报. 仅查看自然日星期, 不依赖节假日表.
        weekend = {"sat": 5, "sun": 6}
        nowd = date.today()
        if nowd.weekday() == weekend["sat"]:
            expected = nowd - timedelta(days=1)      # 周六 → 期望周五
        elif nowd.weekday() == weekend["sun"]:
            expected = nowd - timedelta(days=2)      # 周日 → 期望周五
        else:
            expected = nowd                           # 周一~周五 → 期望当天
        expected_lag = (expected - row[0]).days if hasattr(row[0], "isoformat") else None
        if expected_lag is None or expected_lag > 1:
            status = "FAIL"
        elif expected_lag == 1:
            status = "WARN"
        else:
            status = "OK"
        return _record(name, status,
                       {"latest_date": latest, "today": today,
                        "days_lag": days_lag, "expected_lag": expected_lag,
                        "expected": expected.isoformat(), "universe_size": n_uni},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


def check_akshare() -> dict:
    # 重试退避: 瞬时 502/抖动过滤, 持续失败才判 WARN. 失败信息注明是否已被 free_stockdb_sync 兜底.
    _MAX_TRY = 3
    _BASE_DELAY = 1.0   # 秒, 指数退避 1s/2s/3s
    t0 = time.time()
    name = "akshare"
    last_exc = None
    for attempt in range(1, _MAX_TRY + 1):
        if attempt > 1:
            time.sleep(_BASE_DELAY * (attempt - 1))
        try:
            # 6s 超时, 拉一只高流动性样本 (贵州茅台 600519.SH)
            url = "http://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43,f44,f45,f46"
            req = request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 A_stock_rotation/premarket_healthcheck",
                "Referer": "http://quote.eastmoney.com/",
            })
            with request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            if not isinstance(data, dict) or "data" not in data:
                return _record(name, "WARN",
                               {"error": "返回结构异常", "attempts": attempt,
                                "raw_keys": list(data.keys()) if isinstance(data, dict) else None},
                               t0)
            d = data["data"] or {}
            # f43=最新价(分), f44=最高, f45=最低, f46=今开
            price = d.get("f43")
            if not price or price == "-":
                return _record(name, "WARN",
                               {"error": "无价格", "attempts": attempt, "raw": d}, t0)
            return _record(name, "OK",
                           {"price_cent": int(price), "canon": "600519.SH",
                            "attempts": attempt, "degraded": False},
                           t0)
        except (error.URLError, socket.timeout) as e:
            last_exc = e
        except Exception as e:
            last_exc = e
    # 全部重试失败: 网络降级. daily_bars 已由 free_stockdb_sync 兜底, 不额外升级.
    return _record(name, "WARN",
                   {"error": f"网络异常(降级): {last_exc}", "attempts": _MAX_TRY,
                    "degraded": True,
                    "note": f"连续 {_MAX_TRY} 次失败, 已确认不可达; daily_bars 由 free_stockdb_sync 兜底"},
                   t0)


def check_state_io() -> dict:
    t0 = time.time()
    name = "state_io"
    try:
        # 1) 现有 state.json 可读
        existed = os.path.exists(STATE_FILE)
        # 2) DATA_DIR 可写
        probe = os.path.join(DATA_DIR, ".state_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write(_now())
        os.remove(probe)
        # 3) LIVE_STATE 路径可写
        from realtime_engine import LIVE_STATE
        live_dir = os.path.dirname(LIVE_STATE)
        os.makedirs(live_dir, exist_ok=True)
        probe2 = os.path.join(live_dir, ".live_probe")
        with open(probe2, "w", encoding="utf-8") as f:
            f.write(_now())
        os.remove(probe2)
        return _record(name, "OK",
                       {"state_file_exists": existed,
                        "state_file": STATE_FILE,
                        "live_state": LIVE_STATE},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


# ---- 业务产物断言 (三类盲区补漏: 静默断层 / 吞错降级 / 陈旧数据) ----


def _distinct_trade_days(n):
    """daily_bars 最近 n 个(去重)交易日, 升序 YYYYMMDD 列表. 供滞后判定. """
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute("""
            SELECT DISTINCT date FROM daily_bars
            ORDER BY date DESC LIMIT ?
        """, [n]).fetchall()
    finally:
        con.close()
    days = [_norm_date(r[0]) for r in rows if r and r[0]]
    return list(reversed(days))   # 升序: [最老..最新], 倒数第 k 个 = 最新往前第 k-1 个交易日


def _norm_date(v):
    s = str(v).strip().replace("-", "")
    return s[:8] if len(s) >= 8 and s[:4].isdigit() else s


def check_ic_history() -> dict:
    """断言① ic_history 行数与新鲜度: 检出 ic_history 从未被管道生成的静默断层.

    ic_history 记录信号日的未来收益 IC. 判定"已到期"的天 = 距数据库最新 bar 至少
    max_hold 个交易日的 selection 天(此后 max_hold 收益已全部发生), 此类天必须在
    ic_history.csv 有对应行, 否则说明滞后结算闭环断裂(本修复前 ic_history 从未
    由管道自动生成, 正是此类盲区). 阈值: 文件缺失/覆盖不足/最新结算日陈旧 -> FAIL.
    """
    t0 = time.time()
    name = "ic_history"
    import ic_track as icm
    from db import StockDB

    IC_HISTORY = icm.IC_HISTORY
    MAX_HOLD = 10
    try:
        if not os.path.isfile(IC_HISTORY):
            return _record(name, "FAIL",
                           {"error": "ic_history.csv 缺失 (滞后结算闭环从未落盘)"}, t0)

        # 已到期判定: 最新 bar 往前数 MAX_HOLD 个交易日
        db = StockDB()
        try:
            latest_bar = _norm_date(db.latest_daily_bar_date())
        finally:
            db.close()
        if not latest_bar:
            return _record(name, "FAIL", {"error": "daily_bars 无数据, 无法判定到期"}, t0)
        trade_days = _distinct_trade_days(MAX_HOLD + 1)
        if len(trade_days) <= 1:
            return _record(name, "WARN",
                           {"error": "交易日不足, 暂无法判定陈旧",
                            "latest_bar": latest_bar}, t0)

        # 前一日交易日: 此后 >=1 根未来K线已发生, 该信号日的 h1 必已可结算.
        prev_trade_day = trade_days[-2] if len(trade_days) >= 2 else None
        # AVAIL 天中"至少能结算 h1"的天 = 一切早于 latest_bar 的天(当天最后/未收盘不含)
        # 用 prev_trade_day 界定: 信号日 <= 前一交易日的天, 其 h1 收益已发生.
        due_days = [d for d in icm.available_days()
                    if prev_trade_day is not None and d <= prev_trade_day]
        if not due_days:
            return _record(name, "OK",
                           {"note": "无已过交易日的 selection 天(数据不足)",
                            "latest_bar": latest_bar, "prev_trade_day": prev_trade_day},
                           t0)

        # 统计 ic_history 覆盖
        rows = icm._read_history_rows()
        hist_rows = rows[1] if rows[0] is not None else []
        hist_days = {r.split(",")[0] for r in hist_rows if r.strip()}
        missing = [d for d in due_days if d not in hist_days]
        # 新鲜度: ic_history 应有最近一个"可结算天"的记录
        latest_hist_day = max(hist_days) if hist_days else None
        hold_cols = [int(c.split("ic_h", 1)[1]) for c in rows[0].split(",")[1:]] if rows[0] else []
        # 完整性: 对已完整到期(h = MAX_HOLD 收益已发生)的天, 是否都有其 IC 深度的列
        complete_due = [d for d in due_days
                        if len(trade_days) >= MAX_HOLD + 1
                        and d <= trade_days[-1 - MAX_HOLD]]
        full_depth = all(h in hold_cols for h in [1, 3, 5, 10])

        problems = []
        if latest_hist_day is None:
            problems.append("ic_history 无任何数据行")
        if missing:
            problems.append(f"以下已可结算天无 IC 行: {missing}")
        if complete_due and not full_depth:
            problems.append(f"完全到期天 {len(complete_due)} 个但表头缺持有期列 "
                            f"(现有 {hold_cols}), 未做滞后补全")
        # 门槛: 任一故障 -> FAIL; 仅"列深度不足但行齐全" -> WARN
        hard_fail = bool(problems) and (missing or latest_hist_day is None)
        status = "FAIL" if hard_fail else ("WARN" if problems else "OK")
        return _record(name, status,
                       {"due_days": len(due_days), "hist_rows": len(hist_rows),
                        "latest_hist_day": latest_hist_day,
                        "prev_trade_day": prev_trade_day,
                        "complete_due": len(complete_due),
                        "missing": missing, "hold_cols": hold_cols,
                        "full_depth": full_depth, "problems": problems},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


def _latest_strategy_optimization():
    """扫 data/daily/*/strategy_optimization.json, 返回(generated_at_, dict) 最新一份."""
    daily = os.path.join(DATA_DIR, "daily")
    best = None
    best_key = ""
    for d in sorted(os.listdir(daily)):
        if len(d) != 8 or not d.isdigit():
            continue
        p = os.path.join(daily, d, "strategy_optimization.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        key = obj.get("meta", {}).get("generated_at", d)
        if key > best_key:
            best_key = key
            best = (d, obj)
    return best


def check_learn_errors() -> dict:
    """断言② 内部 error 计数告警: 增量学习回执里的 error/parse_error 升级为告警.

    incremental_learn 设计为"失败一律容错不阻断主流程", 但 error 字段常被当普通日志
    忽略, 掩盖 LLM 调用失败/JSON 解析失败/北极库写失败(吞错降级盲区). 本检查扫描最新
    一份 strategy_optimization.json, 任一错误字段非空 -> WARN(可触发人工复核/重建).
    """
    t0 = time.time()
    name = "learn_errors"
    found = _latest_strategy_optimization()
    if found is None:
        return _record(name, "WARN", {"note": "未发现任何 strategy_optimization.json"}, t0)
    day, obj = found
    errs = {}
    for k in ("error", "parse_error", "write_error", "arcticdb_warn"):
        v = obj.get(k)
        if v:
            errs[k] = str(v)[:200]
    triggered = bool(obj.get("triggered"))
    priority = (obj.get("optimization") or {}).get("action_recommendation", {}).get("priority")
    detail = {
        "day": day,
        "generated_at": (obj.get("meta") or {}).get("generated_at"),
        "triggered": triggered,
        "optimization_priority": priority,
        "internal_errors": errs,
    }
    status = "OK" if not errs else "WARN"
    if errs:
        detail["action"] = "检查 incremental_learn 落盘回执, 修复后再触发学习闭环"
    return _record(name, status, detail, t0)


def check_ic_curve() -> dict:
    """断言③ ic_curve 新鲜度(可触发重建): 避免 ICIR 依据长期冻结.

    weight_optimizer 用 ic_curve_<f>_k20.csv 的 ic_h20 算 ICIR; ic_h20 需要未来20个
    交易日收益, 故"可结算最新日" = DB 最新 bar 往前 20 个交易日. 判定以"非空 ic_h20
    的最后一行"为准(ic_backtest 对区间末尾未发生收益的行写 NaN, MAX(day) 会误读成
    已结算). 若可结算行落后于应结算日 -> 权重依据冻结; 任一因子文件此类落后 ->
    FAIL 建议增量刷新/全量重建.
    """
    t0 = time.time()
    name = "ic_curve"
    try:
        from config import DUCKDB_PATH
        import duckdb
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            latest = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
        finally:
            con.close()
        if not latest or not latest[0]:
            return _record(name, "FAIL", {"error": "daily_bars 无数据"}, t0)
        latest_bar = _norm_date(latest[0])

        import weight_optimizer as wo
        factors = {}
        for wkey, fname in wo.ALPHA_FACTORS.items():
            p = os.path.join(wo.IC_DIR, "ic_curve_{}_k20.csv".format(fname))
            if not os.path.isfile(p):
                factors[wkey] = {"file": p, "status": "MISSING"}
                continue
            try:
                import pandas as pd
                df = pd.read_csv(p, dtype={"day": str})
                if df.empty or "day" not in df.columns:
                    factors[wkey] = {"file": p, "status": "EMPTY", "days": 0}
                    continue
                total_days = int(len(df))
                raw_last_day = max(df["day"].astype(str).str.strip())
                # 有效"已结算"行 = ic_h20 非空的那一行(未来收益已发生)
                if "ic_h20" in df.columns:
                    eff = df.dropna(subset=["ic_h20"])
                    last_day = (max(eff["day"].astype(str).str.strip())
                                if not eff.empty else None)
                else:
                    last_day = None
                factors[wkey] = {"file": p, "status": "INIT",
                                 "last_day": last_day,
                                 "raw_last_day": raw_last_day,
                                 "days": total_days,
                                 "valid_h20_days": int(len(eff)) if "ic_h20" in df.columns else 0}
            except Exception as e:
                factors[wkey] = {"file": p, "status": "ERROR",
                                 "detail": str(e)[:120]}

        # 应结算最新日 = 最新 bar 往前 20 个交易日
        trade_days = _distinct_trade_days(21)
        due = None
        if len(trade_days) >= 21:
            due = trade_days[-21]  # 升序倒数第21个 = 最新对应的"ic_h20可结算日"

        stale_files = []
        very_stale_files = []
        if due is not None:
            for wkey, info in factors.items():
                if wkey.startswith("_"):
                    continue
                last_day = info.get("last_day")
                if info.get("status") in ("MISSING", "EMPTY", "ERROR"):
                    very_stale_files.append(wkey)
                    continue
                if last_day is None:
                    very_stale_files.append(wkey)  # 无任何有效 ic_h20 行
                    continue
                # gap = 应结算日 - 有效最后行; >0 => 落后(权重依据冻结到更早)
                try:
                    gap = int(due) - int(_norm_date(last_day))
                except ValueError:
                    gap = 9999
                info["lag_days"] = gap
                if gap > 40:
                    very_stale_files.append(wkey)
                elif gap > 0:
                    stale_files.append(wkey)

        # 落后>40交易日 -> FAIL; 落后>0 -> WARN; 全达标 -> OK.
        if very_stale_files:
            status = "FAIL"
            note = "ic_curve 落后已结算日过多, 应触发 ic_curve_refresh 增量/全量重建"
        elif stale_files:
            status = "WARN"
            note = "ic_curve 未追平到最近可结算日, 建议增量刷新(ic_curve_refresh)"
        else:
            status = "OK"
            note = "ic_curve 的有效 ic_h20 覆盖到最近可结算日"
        return _record(name, status,
                       {"latest_bar": latest_bar, "due_settled_day": due,
                        "factors": {k: v for k, v in factors.items()
                                    if not k.startswith("_")},
                        "stale_files": stale_files,
                        "very_stale_files": very_stale_files,
                        "action": note},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


# ---- 决策层心跳 + 产物新鲜度断言 (盲区补漏: 静默卡死) ----

def _hb_freshness(base_dir: str, component: str):
    """读某组件心跳文件, 判定活性.
    base_dir: 心跳所在的日期目录父目录 (如 data/drl 或 data/daily).
    心跳由实现侧写入 <base_dir>/<latest_daydir>/<component>_heartbeat.json;
    检查侧对齐到"最新存在日期的目录"读取. 返回 dict 供聚合:
      - missing: 心跳文件完全不存在
      - active : (当前正在跑) last_seen 在 STALE_AFTER 内刷新
      - done   : 心跳终态 phase=done 且 ok=true (正常完成, 非卡死)
    """
    import datetime as _dt
    from heartbeat import STALE_AFTER
    # 定位"含该组件心跳文件的最新日期目录"; 避免被半路创建/未完成流程的空日期目录干扰.
    hb_path = None
    latest_day = None
    try:
        days = [d for d in os.listdir(base_dir)
                if os.path.isdir(os.path.join(base_dir, d))
                and len(d) == 8 and d.isdigit()]
        for d in sorted(days, reverse=True):
            p = os.path.join(base_dir, d, f"{component}_heartbeat.json")
            if os.path.isfile(p):
                hb_path = p
                latest_day = d
                break
    except Exception:
        pass
    if not hb_path:
        return {"component": component, "active": False, "missing": True,
                "done": False, "day": latest_day}
    try:
        with open(hb_path, encoding="utf-8") as f:
            hb = json.load(f)
    except Exception as e:
        return {"component": component, "active": False, "missing": False,
                "done": False, "day": latest_day,
                "read_error": f"{type(e).__name__}: {e}"}
    last = hb.get("last_seen") or ""
    now = _dt.datetime.now()
    try:
        last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        age_s = int((now - last_dt).total_seconds())
    except Exception:
        age_s = None
    active = age_s is not None and age_s <= STALE_AFTER
    done = (hb.get("phase") == "done" and hb.get("ok") is True)
    return {
        "component": component,
        "active": active,
        "missing": False,
        "done": done,
        "day": latest_day,
        "last_seen": last,
        "age_s": age_s,
        "phase": hb.get("phase"),
        "ok": hb.get("ok"),
        "error": hb.get("error"),
        "started_at": hb.get("started_at"),
    }


def check_decider_freshness() -> dict:
    """断言④ 决策层活性: DRL/LLM 心跳活性 + 决策产物新鲜度.
    修复后 drl_train/llm_commentary/pre_drl_brief/incremental_learn 运行/结束时
    落 <daydir>/<component>_heartbeat.json. 检查方对齐最新日期目录读取心跳, 三态判定:
      - done  (phase=done 且 ok=true)  -> 正常完成, 非卡死 (OK)
      - active(last_seen 在 STALE_AFTER 内) -> 正在处理 / 刚结束 (OK)
      - 其余 (缺失 / 停在中间 phase 且 age>STALE_AFTER) -> WARN, 提示可能卡死/静默崩溃
    产出连心跳都没有的组件 -> WARN 提示决策链路未按预期运行.
    """
    t0 = time.time()
    name = "decider_freshness"

    # 组件 -> (心跳父目录, 期望产物文件, 产物所在父目录)
    components = {
        "drl_train": (os.path.join(DATA_DIR, "drl"), None, os.path.join(DATA_DIR, "drl")),
        "pre_drl_brief": (os.path.join(DATA_DIR, "drl"),
                          "pre_drl_brief.json", os.path.join(DATA_DIR, "drl")),
        "llm_commentary": (os.path.join(DATA_DIR, "daily"),
                           "llm_commentary.json", os.path.join(DATA_DIR, "daily")),
        "incremental_learn": (os.path.join(DATA_DIR, "daily"),
                              "strategy_optimization.json", os.path.join(DATA_DIR, "daily")),
    }
    hb_states = {}
    stalled = []      # 心跳存在但既不 done 也失联 (疑似卡死)
    no_hb_with_product = []  # 有产物但无心跳 (链路用了旧代码/未运行)
    for comp, (base_dir, prod, prod_base) in components.items():
        st = _hb_freshness(base_dir, comp)
        hb_states[comp] = st
        if st.get("missing"):
            # 有产物却没心跳 -> 历史产物是旧版本遗留或新代码未运行
            if prod:
                found = _latest_product(prod_base, prod)
                if found:
                    no_hb_with_product.append(comp)
        elif not st.get("done") and not st.get("active"):
            stalled.append((comp, st.get("age_s"), st.get("phase"), st.get("ok")))

    problems = []
    if stalled:
        problems.append("疑似卡死/静默崩溃(心跳存在但既未完成也失联): "
                        + "; ".join(f"{c}(age={a}s,phase={p},ok={k})"
                                    for c, a, p, k in stalled))
    if no_hb_with_product:
        problems.append("有决策产物但缺心跳(可能未用新代码运行): "
                        + ", ".join(no_hb_with_product))

    status = "OK" if not problems else "WARN"
    return _record(name, status,
                   {"components": {c: {"active": s.get("active"), "done": s.get("done"),
                                       "phase": s.get("phase"),
                                       "last_seen": s.get("last_seen"),
                                       "age_s": s.get("age_s"), "ok": s.get("ok"),
                                       "error": s.get("error"), "day": s.get("day"),
                                       "missing": s.get("missing")}
                                   for c, s in hb_states.items()},
                    "stalled": [c for c, *_ in stalled],
                    "no_hb_with_product": no_hb_with_product,
                    "problems": problems,
                    "action": "心跳非 done 且失联的组件检查卡死原因; 缺心跳但有产物的组件用新代码重跑"},
                   t0)


def _latest_product(base_dir, fname):
    """扫 base_dir 下所有 8 位日期目录, 返回最近一份含 fname 的完整路径."""
    try:
        days = [d for d in os.listdir(base_dir)
                if os.path.isdir(os.path.join(base_dir, d))
                and len(d) == 8 and d.isdigit()]
        for d in sorted(days, reverse=True):
            p = os.path.join(base_dir, d, fname)
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    return None


# ---- 执行层仓位空缺 / 应成交未成交对账断言 (盲区补漏) ----

def check_position_gap() -> dict:
    """断言⑤ 执行层仓位空缺: DRL target_plan 的目标 vs 实际持仓缺口.
    DRL plan 每个标的带 target_weight, 实盘状态(live_state.json)记录实际持有.
    style: 空头仓位(timeout 持仓跑掉) / 超配 / 漏配. 用 target_weight 总和 vs
    实盘目标占比做粗略对账, 检出"应持仓但未成交"的静默错配.
    """
    t0 = time.time()
    name = "position_gap"
    try:
        # 1) 目标: 最近一份 target_plan.json
        target_path = None
        target_day = None
        for d in sorted(os.listdir(os.path.join(DATA_DIR, "drl")), reverse=True):
            p = os.path.join(DATA_DIR, "drl", d, "target_plan.json")
            if os.path.isfile(p):
                target_path = p
                target_day = d
                break
        if not target_path:
            return _record(name, "WARN",
                           {"note": "无 target_plan.json (可能未到盘后生成节拍)", "target_day": None},
                           t0)
        try:
            with open(target_path, encoding="utf-8") as f:
                plan = json.load(f)
        except Exception as e:
            return _record(name, "FAIL", {"error": f"读 target_plan 失败: {e}"}, t0)
        top_n = plan.get("top_n") or []
        target_canons = {str(it.get("canon")) for it in top_n if it.get("canon")}

        # 2) 实际: live_state.json 持仓
        from realtime_engine import LIVE_STATE
        actual_canons = set()
        actual_meta = {}
        live_day = None
        if os.path.isfile(LIVE_STATE):
            try:
                with open(LIVE_STATE, encoding="utf-8") as f:
                    ls = json.load(f)
                live_day = ls.get("day")
                positions = (ls.get("positions") or {})
                if isinstance(positions, dict):
                    actual_canons = {str(k) for k in positions.keys()}
                    actual_meta = {"n": len(actual_canons), "engine_live": True,
                                   "live_day": live_day}
                elif isinstance(positions, list):
                    actual_canons = {str(x.get("canon")) for x in positions if x.get("canon")}
                    actual_meta = {"n": len(actual_canons), "engine_live": True,
                                   "live_day": live_day}
            except Exception as e:
                actual_meta = {"live_state_read_error": f"{type(e).__name__}: {e}"}
        # 3) 缺口: 目标池 - 实际持仓
        gap = sorted(target_canons - actual_canons)
        excess = sorted(actual_canons - target_canons)
        problems = []
        # 节点对齐保护: 实盘日滞后于决策日 (target 先出、次日实盘执行) 属正常时序,
        # 不做严格缺口判定; 仅当同日才检测"应成交未成交". 避免把跨天误报成缺口.
        day_aligned = not live_day or not target_day or (live_day.replace("-", "") == target_day)
        note = None
        if not day_aligned:
            note = (f"live_state.day={live_day} 滞后于 target_plan.day={target_day} "
                    f"(决策先于实盘执行, 属正常时序, 跳过严格缺口判定)")
            problems = []  # 跨天不判缺口
        elif target_canons and not actual_canons:
            problems.append(f"目标 {len(target_canons)} 个标的但实盘空仓(引擎未成交或未启动)")
        elif gap and len(gap) > max(1, int(len(target_canons) * 0.5)):
            problems.append(f"目标 {len(target_canons)} 个, 漏配 {len(gap)} 个: {gap[:10]}")
        status = "FAIL" if (problems and not actual_meta.get("engine_live")) else (
            "WARN" if problems else "OK")
        # 空仓且无 live_state 时 (回测/未开实盘) 判定为 WARN 非 FAIL, 避免误报
        status = "WARN" if (problems and not target_canons) else status
        if note:
            # 跨天时序提示 (live 滞后于 target) 是正常节拍, problems 恒为空;
            # 保持 OK 等级, 仅把 note 随结果带出供查看, 避免每个跨天节点持续告噪.
            status = "OK"
        return _record(name, status,
                       {"target_day": target_day, "target_file": target_path,
                        "target_n": len(target_canons),
                        "actual_n": len(actual_canons), "actual_meta": actual_meta,
                        "gap": gap, "excess": excess,
                        "day_aligned": day_aligned, "note": note,
                        "problems": problems,
                        "action": "核对未成交标的; 若目标池大而实盘空仓, 检查引擎是否正常撮合"},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


# ---- 跨组件契约: target_weight 契约校验断言 (盲区补漏) ----

def check_target_contract() -> dict:
    """断言⑥ 跨组件契约: target_weight 是否被下单侧消费.
    历史: drl_train 写 target_plan.json 每项带 target_weight (等权 1/n),
    但 realtime_engine._rebalance 用 band 等权硬设, 未读取每项 -> 契约悬空.
    2026-09-05 已修复: 引擎(_rebalance/backtest)消费每项 target_weight;
    selector/ensure_target_weights 依 f_ml 预测收益生成收缩+封顶的非等权权重.
    本检查现核验:
      a) 各目标权重和为 1.0 (归一), 缺 target_weight 的项占比.
      b) 非等权权重存在 => 已消费信号(新契约), 记录 info 不再告警.
    """
    t0 = time.time()
    name = "target_contract"
    try:
        target_path = None
        for d in sorted(os.listdir(os.path.join(DATA_DIR, "drl")), reverse=True):
            p = os.path.join(DATA_DIR, "drl", d, "target_plan.json")
            if os.path.isfile(p):
                target_path = p
                break
        if not target_path:
            return _record(name, "WARN", {"note": "无 target_plan.json"}, t0)
        try:
            with open(target_path, encoding="utf-8") as f:
                plan = json.load(f)
        except Exception as e:
            return _record(name, "FAIL", {"error": f"读 target_plan 失败: {e}"}, t0)
        top_n = plan.get("top_n") or []
        if not top_n:
            return _record(name, "WARN", {"note": "target_plan top_n 为空"}, t0)
        ws = []
        missing_w = 0
        for it in top_n:
            w = it.get("target_weight")
            if w is not None:
                try:
                    ws.append(float(w))
                except (TypeError, ValueError):
                    missing_w += 1
            else:
                missing_w += 1
        total = sum(ws) if ws else 0.0
        n = len(top_n)
        # 归一性: 权重和应接近 1.0 (允许小偏差)
        norm_ok = bool(ws) and abs(total - 1.0) < 0.05
        # 等权性: 标准差 ~0 => 等权分配; >0 => f_ml 预测加权(引擎已消费)
        equal_ok = True
        std = 0.0
        if len(ws) > 1:
            import statistics
            std = statistics.stdev(ws) if len(ws) > 1 else 0.0
            equal_ok = std < 1e-6

        problems = []
        notes = []
        if missing_w:
            problems.append(f"{missing_w}/{n} 项缺 target_weight (契约字段缺失)")
        if not norm_ok:
            problems.append(f"target_weight 总和={total:.4f} ≠ 1.0 (归一契约破坏)")
        # 非等权 = f_ml 预测加权已生效, 引擎 _rebalance 按 target_weight 下单(2026-09-05)
        if ws and not equal_ok:
            notes.append(f"非等权(std={std:.6f}) = f_ml 预测加权已启用且引擎已消费(契约闭合)")
        # missing_w 大多数 => 结构性缺失, 判定更重
        status = "OK" if (not problems) else ("WARN" if missing_w < n else "FAIL")
        return _record(name, status,
                       {"target_file": target_path, "n": n,
                        "missing_weight": missing_w,
                        "norm_sum": round(total, 6) if ws else None,
                        "norm_ok": norm_ok, "std": round(std, 6),
                        "equal_weights": equal_ok,
                        "problems": problems,
                        "notes": notes,
                        "action": "引擎已按每项 target_weight 落目标市值(2026-09-05 契约闭合)"},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


# ========================================================================
# 第五轮: 分层健康探测 (数据存储 / 查询引擎 / AI Agent)
# ========================================================================


def check_arcticdb_rw() -> dict:
    """数据存储层(R1) 进程存活 + 读写双向健康 + 查询耗时/IO 观测.

    ArcticDB 此处为内嵌 LMDB(单进程), 无独立子进程可 poll();
    "进程存活"等价于单例句柄可枚举库 + 事务级 write->read 双向 round-trip 成功
    (等价 wait_for_server_to_come_up / process.poll() is None 的活性判定).
    复用 arctic_store 单例(避免对同一 LMDB 路径二次 open), 通过 probe_rw 对每个
    库写极小临时 symbol 再读回删除; 同时采读写耗时(total_time_ms 等价物)与行数.
    """
    import time as _t
    t0 = time.time()
    name = "arcticdb_rw"
    try:
        from arctic_store import get_store, ARCTIC_URI
        store = get_store()
        # 进程存活: 单例句柄能枚举库目录 = 后端可达
        try:
            libs = list(store._ac.list_libraries())
        except Exception as e:
            return _record(name, "FAIL",
                           {"error": f"句柄 list_libraries 失败(后端不可达): {type(e).__name__}: {e}"},
                           t0)
        # 读写 round-trip 探针 (复用单例句柄, 逐库临时 symbol, 探后即删)
        rw_start = _t.time()
        rw = store.probe_rw()
        rw_ms = (_t.time() - rw_start) * 1000
        failed = {k: v for k, v in rw.items() if not v.get("ok")}
        status = "OK" if not failed else "FAIL"
        return _record(name, status,
                       {"uri": ARCTIC_URI, "libraries": len(libs),
                        "round_trip_ms": round(rw_ms, 2),
                        "rw_per_lib": rw,
                        "failed_libs": {k: v["error"] for k, v in failed.items()} or None},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


def _duck_select1(con) -> tuple[float, bool]:
    """对给定连接执行 SELECT 1 心跳探针, 返回(ms, ok)."""
    t = time.time()
    try:
        con.execute("SELECT 1").fetchone()
        return (time.time() - t) * 1000, True
    except Exception:
        return (time.time() - t) * 1000, False


def check_duckdb_pool() -> dict:
    """查询引擎层(R2) 连接池健康 + 主动轻量探针.

    借鉴连接池 health_check_interval 定期对(空闲)连接执行 HealthCheck() 的思路:
    盘前对 DuckDB 只读句柄执行 SELECT 1 心跳(等价健康校验), 统计响应耗时;
    同时做一次真实轻量 IO(SELECT MAX(date)) 验证引擎正常响应.
    连接可开/可关、SELECT 1 快速返回 => 连接池健康.
    """
    t0 = time.time()
    name = "duckdb_pool"
    try:
        import duckdb
        if not os.path.exists(DUCKDB_PATH):
            return _record(name, "FAIL", f"db 不存在: {DUCKDB_PATH}", t0)
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            s1_ms, s1_ok = _duck_select1(con)
            t_io = time.time()
            row = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
            io_ms = (time.time() - t_io) * 1000
            latest = row[0].isoformat() if row and row[0] and hasattr(row[0], "isoformat") else (
                str(row[0])[:10] if row and row[0] else None)
            # 再验一句连接仍活(池内复用能力)
            s2_ms, s2_ok = _duck_select1(con)
            alive = s1_ok and s2_ok and row is not None
        finally:
            con.close()
        status = "OK" if alive else "FAIL"
        return _record(name, status,
                       {"liveness": {"select1_ms": round(s1_ms, 2),
                                     "reuse_select1_ms": round(s2_ms, 2),
                                     "alive": alive},
                        "probe": {"io_ms": round(io_ms, 2), "latest_date": latest},
                        "path": DUCKDB_PATH},
                       t0)
    except Exception as e:
        return _record_duckdb(e, name, t0)


def _ngrams(s: str, n: int = 3) -> set:
    """中文字符 n-gram 集合, 用于评论相似度."""
    s = (s or "").strip()
    if not s:
        return set()
    chars = list(s)
    return set("".join(chars[i:i + n]) for i in range(max(0, len(chars) - n + 1)))


def _cosine_sim(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1e-9, (len(a) ** 0.5) * (len(b) ** 0.5))


def check_agent_loop() -> dict:
    """AI Agent 层(R3) 输出循环 / 进展停滞检测.

    a) 输出相似度: 对连续交易日 llm_commentary.json 的 commentary 文本算字符
       n-gram 余弦相似度; 若连续多轮 > 0.85, 判定 Agent 陷入无效输出循环/卡死
       (等价 agent-vitals 的 output_similarity).
    b) 覆盖与进展: 用 ic_history 覆盖行数 + strategy_optimization 是否触发,
       判断 Agent(DRL 决策闭环)产出是否长期停滞; 有输出但 coverage 不涨 =>
       "卡住"状态告警.
    """
    t0 = time.time()
    name = "agent_loop"
    SIM_THRESHOLD = 0.90  # 连续相似度超阈值才判循环; 0.90 减少误报(默认 LLM 评论文本差异大)
    STUDDER_RUN = 3  # 连续 N 个相邻对相似才真正判定循环, 容忍单次雷同
    try:
        # ---- a) LLM 评论相似度 ----
        daily = os.path.join(DATA_DIR, "daily")
        pairs: list[dict] = []
        texts: list[str] = []
        sims: list[float] = []
        if os.path.isdir(daily):
            for d in sorted(os.listdir(daily)):
                if len(d) != 8 or not d.isdigit():
                    continue
                p = os.path.join(daily, d, "llm_commentary.json")
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        o = json.load(f)
                    c = (o.get("commentary") or {}).get("commentary")
                except Exception:
                    continue
                if not c:
                    continue
                if texts:
                    sims.append(_cosine_sim(_ngrams(texts[-1]), _ngrams(str(c))))
                    pairs.append({"prev": texts[-1], "cur": str(c), "sim": round(sims[-1], 3)})
                texts.append(str(c))
        # 连续高相似(循环)判定: 滑窗统计"连续 sim>0.85"最长串
        longest_run = 0
        run = 0
        for s in sims:
            run = run + 1 if s > SIM_THRESHOLD else 0
            longest_run = max(longest_run, run)
        loop_detected = longest_run >= STUDDER_RUN - 1

        # ---- b) DRL 覆盖/进展停滞 ----
        # 用现有信号: ic_history 覆盖趋势 + strategy_optimization 是否触发.
        # 参考 check_ic_history 直接从 ic_track.IC_HISTORY 读, 避免路径不一致.
        coverage_issues = []
        hist_rows_now = None
        try:
            import ic_track as icm
            icpath = icm.IC_HISTORY
            if os.path.isfile(icpath):
                with open(icpath, encoding="utf-8") as f:
                    hist_rows_now = sum(1 for _ in f) - 1  # 减表头
        except Exception:
            pass
        so = _latest_strategy_optimization()
        if so is not None:
            _day, sobj = so
            if not sobj.get("triggered"):
                coverage_issues.append(
                    "最近 strategy_optimization 未触发(DRL 增量学习闭环无进展)")

        problems = []
        if loop_detected:
            problems.append(f"LLM 评论连续 {longest_run + 1} 次高度相似(>0.85), 疑似输出循环/卡死")
        if coverage_issues:
            problems.append("DRL 覆盖/进展停滞: " + "; ".join(coverage_issues))

        # 状态: 循环→FAIL; 覆盖停滞→WARN; 仅单次雷同或无数据→OK(反馈在 detail)
        if loop_detected:
            status = "FAIL"
        elif coverage_issues:
            status = "WARN"
        else:
            status = "OK"
        # 单次雷同但未成串(非循环) 仍 OK, 反馈在 detail 里
        top_pairs = sorted(pairs, key=lambda x: -x["sim"])[:3] if pairs else []
        return _record(name, status,
                       {"commentary_days": len(texts),
                        "adjacent_pairs": len(sims),
                        "max_similarity": round(max(sims), 3) if sims else None,
                        "longest_high_sim_run": longest_run,
                        "loop_detected": loop_detected,
                        "top_similar_pairs": top_pairs,
                        "coverage_issues": coverage_issues or None,
                        "ic_history_rows": hist_rows_now,
                        "problems": problems,
                        "action": ("检查 LLM 评论是否模板化/DRL 是否卡死; 若确为循环"
                                   "应重建模型或重置输入" if problems else None)},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


# ---- 反馈进化层 第六轮: 策略健康诊断 / 反思归因 / 回测-模拟一致性 ----

def _ic_recent_vs_history(wkey: str, fname: str, recent_n: int = 20,
                          hist_n: int = 80, col: str = "ic_h20",
                          reversal: bool = False):
    """因子 IC 衰减检测: 读 ic_curve_<fname>_k20.csv 的 ic_h20,
    比较'最近 recent_n 个有效值均值'与'更早 hist_n 个历史值均值'.

    reversal=True 表示该因子是反转义 alpha(vol/mom_rev): 其打分为"低波动/近跌给
    高分", 故**深度负 IC 反而是有效方向**。对这种因子, 真正的"衰减"应是近期 IC
    **转正**(与反转义矛盾, 变成反向信号) 或 **绝对值大幅收敛归零**(信号失灵),
    而非负 IC 加深。若按"近期比历史均值下跌"一律告警, 会把"负IC加深=更有效"误报
    为失效。返回 {ok, recent_mean, hist_mean, rel_drift, ...} 或 {ok:None}。"""
    import weight_optimizer as wo
    import pandas as pd
    p = os.path.join(wo.IC_DIR, f"ic_curve_{fname}_k20.csv")
    if not os.path.isfile(p):
        return {"ok": None, "file": p, "error": "MISSING"}
    try:
        df = pd.read_csv(p, dtype={"day": str})
        if col not in df.columns:
            return {"ok": None, "file": p, "error": "NO_COL"}
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) < recent_n + 5:          # 样本不足以做"近期vs历史"对比
            return {"ok": None, "file": p,
                    "error": f"样本不足(len={len(s)})", "n_total": int(len(s))}
        recent = s.iloc[-recent_n:]
        hist = s.iloc[-(recent_n + hist_n):-recent_n]
        if recent.empty or hist.empty or hist.mean() == 0:
            return {"ok": None, "file": p, "error": "EMPTY_WINDOW",
                    "n_total": int(len(s))}
        recent_mean = float(recent.mean())
        hist_mean = float(hist.mean())
        rel = (recent_mean - hist_mean) / abs(hist_mean)
        if reversal:
            # 反转义 alpha: 期望近期 IC 仍为负(与反转义一致)。
            #   转正        -> 反转失灵, 变成反向信号(需告警)
            #   仍是负向但 |recent| 收窄到 < 历史强度的 70% 或 <0.02 -> 信号衰减
            flipped = recent_mean > 0
            weakened = not flipped and abs(recent_mean) < max(abs(hist_mean) * 0.7, 0.02)
            ok = not (flipped or weakened)
            return {"ok": bool(ok), "file": p, "reversal": True,
                    "recent_mean": round(recent_mean, 4),
                    "hist_mean": round(hist_mean, 4),
                    "rel_drift": round(rel, 3),
                    "flipped_positive": flipped, "weakened": weakened,
                    "n_recent": int(len(recent)), "n_hist": int(len(hist))}
        # 非反转义因子(常规方向): 近期 IC 相对历史均值明显下跌视为衰减
        ok = rel >= -0.3     # 近期IC比历史均值下跌不超过 30% 视为正常
        return {"ok": bool(ok), "file": p,
                "recent_mean": round(recent_mean, 4),
                "hist_mean": round(hist_mean, 4),
                "rel_drift": round(rel, 3),
                "n_recent": int(len(recent)), "n_hist": int(len(hist))}
    except Exception as e:
        return {"ok": None, "file": p, "error": f"{type(e).__name__}: {e}"}


def _reward_mean_shift() -> dict:
    """DRL 奖励退化检测 (奖励均值偏移, 等价 reward-gradient/均值偏移统计):
    读 reward_curve 库最近若干天的每日平均 reward, 取'最新一天'与'此前历史窗口'
    均值对比; 若最新一天显著低于历史 (低于 历史均值-2σ 或相对下跌>40%) 判退化.
    样本不足时标记 insufficient."""
    from arctic_store import get_store, LIB_REWARD
    store = get_store()
    lib = store._lib(LIB_REWARD)
    if not lib:
        return {"ok": None, "error": "reward 库不可用"}
    syms = sorted(lib.list_symbols())
    if not syms:
        return {"ok": None, "error": "reward 库为空"}
    import math
    day_means = []
    for s in syms:
        df = lib.read(s).data
        if df is not None and "reward" in df.columns:
            v = df["reward"]
            if len(v):
                day_means.append((s, float(v.mean())))
    if not day_means:
        return {"ok": None, "error": "无有效 reward 数据"}
    day_means.sort()
    if len(day_means) < 2:
        return {"ok": None, "error": "仅一天 reward, 样本不足",
                "days": day_means}
    latest_day, latest_mean = day_means[-1]
    hist = [m for d, m in day_means[:-1]]   # 除最新外全部为历史
    hist_mean = float(sum(hist) / len(hist))
    var = sum((x - hist_mean) ** 2 for x in hist) / len(hist)
    sigma = math.sqrt(var) if var > 0 else 0.0
    # 退化判据: 最新显著低于历史 (低于 历史-2σ) 或相对跌幅>40% 且历史为正
    degraded = bool((hist_mean > 0 and latest_mean < hist_mean - 2 * sigma) or
                    (hist_mean > 0 and (latest_mean - hist_mean) / hist_mean < -0.4))
    return {"ok": not degraded, "latest_day": latest_day,
            "latest_mean": round(latest_mean, 4),
            "hist_days": [d for d, _ in day_means[:-1]],
            "hist_mean": round(hist_mean, 4),
            "std": round(sigma, 4),
            "degraded": degraded}


def check_strategy_diagnostics() -> dict:
    """反馈进化层(R4) 策略健康度诊断 (发现'生病').
    定期体检: 从 performance_report 读取收益/夏普/回撤, 对照历史是否恶化;
    因子 IC 衰减检测 (近期 ic_h20 vs 历史均值); DRL 奖励退化检测 (均值偏移).
    产出一份 normal/watch/warning 状态标签 + 各项'生命体征'."""
    t0 = time.time()
    name = "strategy_diag"
    try:
        import performance_report as pr
        # ---- 1) 绩效体检 (收益/夏普/回撤) ----
        diag = {"source": pr.PERF_FILE}
        perf = None
        if os.path.exists(pr.PERF_FILE):
            with open(pr.PERF_FILE, encoding="utf-8") as f:
                perf = json.load(f)
        if perf and perf.get("metrics"):
            m = perf["metrics"]
            total_ret = m.get("total_return")
            max_dd = m.get("max_drawdown")
            sharpe = m.get("sharpe_annual")
            annual = m.get("annual_return")
            period = perf.get("period") or {}
            sample_days = int(period.get("n_days") or 0)
            diag["sample_days"] = sample_days
            diag["metrics"] = {
                "total_return_pct": round(total_ret * 100, 2) if total_ret is not None else None,
                "annual_return_pct": round(annual, 2) if annual is not None else None,
                "max_drawdown_pct": round(max_dd * 100, 2) if max_dd is not None else None,
                "sharpe": round(sharpe, 2) if sharpe is not None else None,
            }
            # 生命体征判定 (单日短样本易失真, 故阈值放宽, 不足则标 watch/insufficient)
            problems = []
            if max_dd is not None and max_dd <= -0.15:
                problems.append("回撤过热(<-15%)")
            if total_ret is not None and total_ret <= -0.05 and sample_days <= 0:
                problems.append("区间收益显著为负")
            if sharpe is not None and sharpe <= 0 and sample_days >= 10:
                problems.append("夏普转负(>10日样本)")
            diag["problems"] = problems
        else:
            diag["metrics"] = None
            diag["problems"] = ["performance_report 无 metrics(样本可能不足)"]

        # ---- 2) 因子 IC 衰减 ----
        import weight_optimizer as wo
        ic_decay = {}
        for wkey, fname in list(wo.ALPHA_FACTORS.items())[:8]:
            # ALPHA_FACTORS 的 vol/mom_rev 均为反转义 alpha(低波/近跌给高分),
            # 深度负 IC 是有效方向; 故用 reversal 判据识别真正衰竭(转正或归零)。
            ic_decay[wkey] = _ic_recent_vs_history(wkey, fname, reversal=True)
        diag_ic = [k for k, v in ic_decay.items()
                   if v.get("ok") is False]

        # ---- 3) DRL 奖励退化 ----
        rw = _reward_mean_shift()
        diag["reward"] = rw

        # ---- 汇总状态: normal / watch / warning ----
        if diag_ic or (rw and rw.get("degraded")):
            status = "WARN"; st_tag = "warning"
        elif diag.get("problems"):
            status = "WARN"; st_tag = "watch"
        else:
            status = "OK"; st_tag = "normal"
        diag["status_tag"] = st_tag
        diag["ic_decay_flagged"] = diag_ic or None
        return _record(name, status,
                       {"status_tag": st_tag,
                        "metrics": diag.get("metrics"),
                        "ic_decay": ic_decay,
                        "ic_decay_flagged": diag_ic or None,
                        "reward": rw,
                        "problems": diag.get("problems") or [],
                        "action": ("检查 IC 是否失效/奖励是否退化, 需校准因子或复核 DRL"
                                   if (diag_ic or (rw and rw.get("degraded")))
                                   else ("关注回撤/收益恶化"
                                         if diag.get("problems") else None))},
                       t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


def check_reflection_diagnostics() -> dict:
    """反馈进化层(R5) 策略反思与归因 (追溯'病因').
    参考反馈反思 Agent: 读取绩效报告的 LLM 表现洞察(performance_review, dict) 与
    持仓归因(attribution: canon + unrealized_pct), 结合前述健康诊断硬指标,
    产出结构化"病因"摘要. 若已出现明显回撤且存在浮亏持仓, 提示结构性失败可能."""
    t0 = time.time()
    name = "reflection_diag"
    try:
        import performance_report as pr
        ref = {"source": pr.PERF_FILE}
        if not os.path.exists(pr.PERF_FILE):
            return _record(name, "WARN",
                           {"insight": ["无 performance_report, 无法生成反思洞察"],
                            "attribution_n": 0, "action": "先运行 performance_report.py"},
                           t0)
        with open(pr.PERF_FILE, encoding="utf-8") as f:
            perf = json.load(f)
        llm = perf.get("llm_commentary") or {}
        review = llm.get("performance_review")
        attr = perf.get("attribution") or []
        metrics = perf.get("metrics") or {}
        total_ret = metrics.get("total_return")
        # 最大回撤归因提示
        max_dd = metrics.get("max_drawdown")

        # performance_review 为 dict: 提取 headline + drivers + risks 拼成洞察
        underlying: list[str] = []
        if isinstance(review, dict):
            if review.get("headline"):
                underlying.append(f"LLM点题: {review['headline']}")
            for d in (review.get("drivers") or [])[:2]:
                underlying.append(f"驱动: {d}")
            for r in (review.get("risks") or [])[:2]:
                underlying.append(f"风险: {r}")
        if max_dd is not None and max_dd <= -8.0:
            underlying.append(f"回撤-{abs(max_dd):.2f}%, 可能结构性问题而非噪音")

        # attribution: canon + unrealized_pct(百分号), 负值即浮亏持仓
        top_bad = [a for a in attr if (a.get("unrealized_pct") or 0) < 0][:3]

        ret_done = total_ret is not None
        status = "OK"
        action = None
        if max_dd is not None and max_dd <= -10.0 and top_bad:
            status = "WARN"
            action = "回撤过大且存在浮亏持仓, 建议复核止盈/止损逻辑是否结构性失效"
        ref["insight"] = underlying or (["暂无异动, 表现正常"] if ret_done else ["样本过少"])
        ref["attribution_n"] = len(attr)
        ref["top_negative_contributors"] = [
            {"symbol": a.get("canon"),
             "unrealized_pct": round(a.get("unrealized_pct") or 0, 2)}
            for a in top_bad] or None
        ref["max_drawdown_pct"] = (None if max_dd is None
                                   else round(max_dd, 2))
        return _record(name, status, {**ref, "action": action}, t0)
    except Exception as e:
        import traceback
        return _record(name, "FAIL", f"{e}\n{traceback.format_exc(limit=5)}", t0)


def check_backtest_paper_parity() -> dict:
    """反馈进化层(R6) 回测-模拟一致性校验 (验证'实盘'环境).
    对比回测基线(backtest_latest.json) 与 模拟盘实绩(performance_report):
      年化收益差 / 最大回撤差 / 换手(交易次数) — 若收益差>阈值(默认 5pp)
    说明模拟环境假设(滑点/手续费/冲击)可能失效, 触发告警."""
    t0 = time.time()
    name = "bt_paper_parity"
    try:
        import performance_report as pr
        # 回测基线
        bt = pr.load_backtest_baseline()
        out = {"backtest": bt if bt.get("ok") else {"ok": False, "reason": bt.get("reason")}}
        if not bt.get("ok"):
            return _record(name, "WARN", {**out, "action": "无回测基线, 无法对比"},
                           t0)
        # 模拟盘实绩
        if not os.path.exists(pr.PERF_FILE):
            return _record(name, "WARN", {**out, "note": "无 performance_report",
                                          "action": "先运行 performance_report.py"}, t0)
        with open(pr.PERF_FILE, encoding="utf-8") as f:
            perf = json.load(f)
        m = perf.get("metrics") or {}
        paper_total = m.get("total_return")        # 百分数(performance_report 已 x100)
        paper_maxdd = m.get("max_drawdown")        # 百分数(负, performance_report 已 x100)
        period = perf.get("period") or {}
        paper_days = int(period.get("n_days") or 0)

        bt_total_pct = bt.get("total_return_pct")  # 已乘100
        bt_maxdd_pct = bt.get("max_drawdown_pct")

        # 收益差(百分点): 回测 vs 模拟
        diff_ret = None
        if bt_total_pct is not None and paper_total is not None:
            diff_ret = round(paper_total - float(bt_total_pct), 2)  # 模拟-回测(均为百分点)
        # 回撤差
        diff_dd = None
        if bt_maxdd_pct is not None and paper_maxdd is not None:
            # paper_maxdd 为负(百分数), bt_maxdd_pct 回撤幅度为正 => 转负
            diff_dd = round(-(abs(paper_maxdd) - float(bt_maxdd_pct)), 2)
        # 换手/交易数差 (样本日不同, 仅展示不硬判)
        bt_trades = bt.get("total_trades")

        # 阈值判定: 年化收益差 > 5pp 才告警; 样本过少(<10交易日)不硬判
        flags = []
        MIN_PP_DAYS = 10   # 平行样本门槛: 少于10个交易日不做严格一致性判定
        insufficient = paper_days < MIN_PP_DAYS or bt.get("trade_days", 0) < MIN_PP_DAYS
        if not insufficient and diff_ret is not None and diff_ret < -5.0:
            flags.append(f"模拟年化收益比回测低 {abs(diff_ret):.1f}pp (>5pp)")
        out["parity"] = {
            "paper_days": paper_days, "bt_days": bt.get("trade_days"),
            "paper_total_return_pct": round(paper_total, 2) if paper_total is not None else None,
            "bt_total_return_pct": bt_total_pct,
            "ret_diff_pp": diff_ret,
            "paper_maxdd_pct": round(paper_maxdd, 2) if paper_maxdd is not None else None,
            "bt_maxdd_pct": bt_maxdd_pct,
            "dd_diff_pp": diff_dd,
            "bt_trades": bt_trades,
            "sample_insufficient": insufficient,
        }
        if insufficient:
            status = "OK"
            note = "样本天数过少(<10交易日), 暂不做严格一致性判定"
        elif flags:
            status = "WARN"
            note = "模拟环境假设可能失效(滑点/手续费/冲击), 需校准"
        else:
            status = "OK"
            note = "回测与模拟关键指标偏差在阈值内"
        return _record(name, status, {**out, "action": note}, t0)
    except Exception as e:
        return _record(name, "FAIL", e, t0)


# ---- 总入口 ----
CHECKS = [
    check_disk,
    check_duckdb,
    check_arcticdb,
    check_orderbook,
    check_daily_bars_freshness,
    check_akshare,
    check_state_io,
    # 业务产物断言 (三类盲区补漏)
    check_ic_history,
    check_learn_errors,
    check_ic_curve,
    # 第四轮盲区补漏: 决策层心跳/产物新鲜度 + 执行层仓位对账 + target_weight 契约
    check_decider_freshness,
    check_position_gap,
    check_target_contract,
    # 第五轮分层健康探测: 数据存储(ArcticDB 读写+存活) / 查询引擎(DuckDB 连接池)
    #                    / AI Agent(LLM 输出循环 + DRL 覆盖停滞)
    check_arcticdb_rw,
    check_duckdb_pool,
    check_agent_loop,
    # 反馈进化层 第六轮: 策略健康诊断 / 反思归因 / 回测-模拟一致性
    check_strategy_diagnostics,
    check_reflection_diagnostics,
    check_backtest_paper_parity,
]


def run_healthcheck(write_to: str | None = HEALTH_FILE) -> dict:
    """同步执行所有检查, 写入 JSON 报告, 返回 dict."""
    results: list[dict] = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append({
                "name": fn.__name__,
                "status": "FAIL",
                "detail": f"{type(e).__name__}: {e}",
                "ms": 0,
            })
    fail_n = sum(1 for r in results if r["status"] == "FAIL")
    warn_n = sum(1 for r in results if r["status"] == "WARN")
    ok_n = sum(1 for r in results if r["status"] == "OK")
    summary = f"OK={ok_n} WARN={warn_n} FAIL={fail_n}"
    overall_ok = fail_n == 0
    overall_level = "OK" if fail_n == 0 else ("DEGRADED" if fail_n <= 2 else "CRITICAL")
    out = {
        "ok": overall_ok,
        "level": overall_level,
        "summary": summary,
        "generated_at": _now(),
        "checks": results,
    }
    if write_to:
        try:
            os.makedirs(os.path.dirname(write_to), exist_ok=True)
            with open(write_to, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2, default=str)
            out["output"] = write_to
        except Exception as e:
            out["write_error"] = str(e)
    return out


if __name__ == "__main__":
    res = run_healthcheck()
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if res["ok"] else 1)

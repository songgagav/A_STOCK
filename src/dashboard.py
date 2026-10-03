# ============================================================
# dashboard.py -- 全A轮动模拟盘 Web 可视化
# 访问: http://localhost:8000
# 数据: 读取 data/live_state.json (盘中引擎实时写入)
#       以及 data/daily/<日>/paper_book.json + trades.json
# 特性: 纯标准库, 无第三方依赖. 前端每3s轮询 /api/live.
#   GET /            -> 可视化仪表盘(根目录模板 + 静态资源)
#   GET /api/live    -> 最新实时状态 JSON
#   GET /api/history -> 最近 N 日每日汇总 JSON
# 用法:
#   python dashboard.py [--port 8000]
# ============================================================

import os
import json
import sys
import glob
import time
import subprocess
import argparse
import logging
import posixpath
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = os.path.join(_BASE, "data")
LIVE_STATE = os.path.join(DATA_DIR, "live_state.json")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
sys.path.insert(0, _BASE)
from config import INIT_CAPITAL, DUCKDB_PATH

_LOG = logging.getLogger("dashboard")

TEMPLATE_PATH = _REPO_ROOT / "templates" / "dashboard.html"
STATIC_ROOT = _REPO_ROOT / "static"
ALLOWED_STATIC = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "application/javascript; charset=utf-8",
}


# ---------------- DuckDB 串行化锁 ----------------
# DuckDB 在多线程并发 read_only 下偶发崩溃 (GIL 状态冲突).
# 用全局锁确保同一时刻仅一个请求在读 DuckDB, 避免 _select GIL 崩溃.
import threading as _threading
_DUCKDB_LOCK = _threading.Lock()

# 物化视图快照目录 (build_meta/build_factor_views 定期刷新, 独立于 DuckDB 主库)
# 盘后维护窗口 run_daily/update_db 以写模式持锁 stockdb.duckdb 时,
# 直连主库会锁冲突; 优先读这些 parquet 可让市场环境/因子IC 始终保持有数据.
VIEWS_PARQUET = os.path.join(DATA_DIR, "views", "parquet")


def _read_view_parquet(name: str, order_by: str = "", cols: list | None = None) -> list | None:
    """优先从物化 parquet 快照读取视图行(fetchall); 无快照/读失败返回 None.
    cols: 显式选择的列序列 —— 必须与直连 duckdb 分支的 SELECT 列序一致,
    否则 parquet 含额外列(如 v_market_breadth 的 n_flat)时按位置映射会错位."""
    fp = os.path.join(VIEWS_PARQUET, name)
    if not os.path.exists(fp):
        return None
    try:
        import duckdb
        # 内存库不允许 read_only (DuckDB 限制); 内存即临时, 不触碰主库文件, 无写锁冲突
        select = ", ".join(cols) if cols else "*"
        con = duckdb.connect(":memory:")
        try:
            return con.execute(
                f"SELECT {select} FROM read_parquet(?) {order_by}", [fp]
            ).fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception:
        return None


def _rows_to_named(rows: list | None, cols: list[str]) -> list[dict] | None:
    """把查询行映射为字段名，避免调用方依赖 Parquet/SQL 列序。"""
    if rows is None:
        return None
    if any(len(row) != len(cols) for row in rows):
        return None
    return [dict(zip(cols, row)) for row in rows]


def _read_view_parquet_named(
    name: str,
    order_by: str = "",
    cols: list[str] | None = None,
) -> list[dict] | None:
    """读取物化视图并按显式列名返回字典行。

    缺少快照、依赖不可用或列不完整时保持 ``None``，由调用方进入既有
    DuckDB fallback；不把缺列错误伪装成空结果。
    """
    if not cols:
        raise ValueError("named parquet reader requires explicit cols")
    return _rows_to_named(_read_view_parquet(name, order_by, cols), cols)


def _duckdb_query(sql: str, params: list | None = None):
    """线程安全执行 DuckDB 查询, 返回 fetchall() 或 df()."""
    import duckdb
    from config import DUCKDB_PATH
    if not os.path.exists(DUCKDB_PATH):
        raise FileNotFoundError(f"DuckDB 不存在: {DUCKDB_PATH}")
    with _DUCKDB_LOCK:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            if params is not None:
                return con.execute(sql, params).fetchall()
            return con.execute(sql).fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass


def _duckdb_query_df(sql: str, params: list | None = None):
    """线程安全 DuckDB 查询, 返回 pandas DataFrame (或 None)."""
    import duckdb
    from config import DUCKDB_PATH
    if not os.path.exists(DUCKDB_PATH):
        raise FileNotFoundError(f"DuckDB 不存在: {DUCKDB_PATH}")
    with _DUCKDB_LOCK:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            if params is not None:
                return con.execute(sql, params).df()
            return con.execute(sql).df()
        finally:
            try:
                con.close()
            except Exception:
                pass


def _duckdb_query_named(
    sql: str,
    params: list | None = None,
    cols: list[str] | None = None,
) -> list[dict] | None:
    """执行兼容查询并按与 Parquet reader 相同的字段名返回。"""
    if not cols:
        raise ValueError("named DuckDB reader requires explicit cols")
    return _rows_to_named(_duckdb_query(sql, params), cols)


# ---------------- h5i 读取路由 (BAR_STORE=duck|h5i, 默认 h5i) ----------------
# [2026-09-05 迁移 m4] dashboard 首页主端点 read_abnormal / read_market_board 提供
# h5i 等价读取分支, 默认切到 h5i (模块开关 _DASH_H5I=True, 环境变量 BAR_STORE 优先).
# 仅当显式设 BAR_STORE=duck 且文件仍在时走 DuckDB 对照 (duck 分支语义不变).
# 其余读取(表/视图)未迁移的部分保持原路径.
_DASH_H5I = True  # 模块内开关 (默认 True -> h5i); 环境变量 BAR_STORE 优先

_H5I_PATH = os.path.join(_BASE, "data", "h5i", "market.db")


def _h5i_mode() -> bool:
    """返回当前是否启用 h5i 读取分支."""
    v = os.environ.get("BAR_STORE", "").strip().lower()
    if v in ("duck", "h5i"):
        return v == "h5i"
    return _DASH_H5I


def _h5i_store():
    """延迟导入 H5iBarStore (仅 h5i 分支用到, 避免无 h5i_db 环境启动失败)."""
    if not os.path.isdir(_H5I_PATH):
        return None
    try:
        from h5i_bar_store import H5iBarStore  # noqa: 延迟导入
        return H5iBarStore()
    except Exception as exc:  # noqa: BLE001
        # h5i_db 只在 Python 3.10 环境提供；看板必须把它视为数据源降级，
        # 不能让单个接口异常杀掉 HTTP 工作线程。
        _LOG.warning("h5i 数据源不可用: %s", exc)
        return None


def _h5i_val(v):
    """把 pandas/numpy 标量规范为 Python 标量; NaN -> None (与 DuckDB NULL 对齐)."""
    if v is None:
        return None
    if isinstance(v, float):
        import math
        if math.isnan(v):
            return None
        return v
    if isinstance(v, bool):
        return v
    try:
        item = v.item()
    except Exception:
        return v
    if isinstance(item, float):
        import math
        if math.isnan(item):
            return None
    return item


def _h5i_rows(df):
    """把 pandas DataFrame 转为 list[tuple] (行序/列序与 duck fetchall 对齐)."""
    out = []
    for r in df.itertuples(index=False, name=None):
        out.append(tuple(_h5i_val(x) for x in r))
    return out


def _h5i_query(sql: str) -> list | None:
    """h5i 版 fetchall: 在 h5i daily_bars 上跑 SQL (ts 时间列需调用方 CAST)."""
    try:
        store = _h5i_store()
        if store is None:
            return None
        return _h5i_rows(store._db.sql(sql, target_partitions=1).to_pandas())
    except Exception:
        return None


def _h5i_max_bar_date() -> str:
    """h5i daily_bars 最新交易日 'YYYY-MM-DD' (无数据返回 '')."""
    rows = _h5i_query("SELECT MAX(CAST(ts AS DATE)) FROM daily_bars")
    if not rows or rows[0][0] is None:
        return ""
    v = rows[0][0]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)[:10]


def _northbound_latest() -> list | None:
    """北向资金最近一日行 (list[tuple], 与 duck 查询列序一致).

    m4: 数据已迁入 h5i.northbound_money (ts 时间列, CAST(ts AS DATE) 取日期),
    优先读 h5i; DuckDB 仍在时只读兜底; 全部缺失返回 None 且不抛错 (调用方
    渲染为空结构).
    """
    try:
        rows = _h5i_query("""
            SELECT CAST(ts AS DATE) AS trade_date, net_buy_amount, buy_amount,
                   sell_amount, daily_balance, sh_index_change_pct
            FROM northbound_money
            WHERE CAST(ts AS DATE) =
                  (SELECT MAX(CAST(ts AS DATE)) FROM northbound_money)
        """)
        if rows:
            return rows
    except Exception:
        pass
    try:
        if os.path.exists(DUCKDB_PATH):
            return _duckdb_query("""
                SELECT trade_date, net_buy_amount, buy_amount, sell_amount,
                       daily_balance, sh_index_change_pct
                FROM northbound_money
                WHERE trade_date = (SELECT MAX(trade_date) FROM northbound_money)
            """)
    except Exception:
        pass
    return None


# ---------------- 数据读取 ----------------
def read_live():
    if os.path.exists(LIVE_STATE):
        try:
            with open(LIVE_STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def read_live_payload():
    """读取实时状态，并为旧生产快照补齐可选的日序列字段。"""
    payload = read_live() or fallback_state()
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    if not isinstance(payload.get("daily_series"), list):
        payload["daily_series"] = read_daily_series()
    return payload


def source_etag(path: str) -> str | None:
    """用源文件 mtime 与大小生成轻量 ETag；缺失源返回 None。"""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'


def _freeze_late_count(day: str) -> int:
    """读取当日迟到信号归档数量；缺失或损坏时返回 0 并由状态栏呈现。"""
    path = os.path.join(DATA_DIR, "daily", day, f"late_signals_{day}.json")
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        items = payload.get("items") if isinstance(payload, dict) else None
        return len(items) if isinstance(items, list) else 0
    except (OSError, json.JSONDecodeError, TypeError):
        return 0


def _freeze_unexplained_count(day: str) -> int:
    """从冻结账本统计当日未解释差异，不把普通迟到候选误报为差异。"""
    path = os.path.join(DATA_DIR, "signal_freeze_events.jsonl")
    difference_kinds = {"shadow_difference", "freeze_difference", "signal_difference"}
    count = 0
    try:
        with open(path, encoding="utf-8") as f:
            rows = f.read().splitlines()
    except OSError:
        return 0
    for line in rows:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            payload = event if isinstance(event, dict) else {}
        event_day = str(payload.get("date") or payload.get("consume_day") or "")
        if event_day not in {day, f"{day[:4]}-{day[4:6]}-{day[6:]}"}:
            continue
        kind = str(event.get("kind") or "") if isinstance(event, dict) else ""
        category = (payload.get("difference_category")
                    or payload.get("classification")
                    or payload.get("category"))
        if kind in difference_kinds and (payload.get("unexplained") is True
                                         or category in (None, "", "unexplained")):
            count += 1
    return count


def read_signal_freeze_status(day: str | None = None) -> dict:
    """返回看板使用的冻结状态摘要；只读已落盘、已校验的权威快照。"""
    now = datetime.now(_SHANGHAI)
    consume_day = day or now.strftime("%Y%m%d")
    try:
        from signal_snapshot import read_mode_control, read_snapshot

        mode = read_mode_control(_BASE).get("mode", "shadow")
        verified = read_snapshot(DATA_DIR, consume_day)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "date": consume_day,
            "status": "unavailable",
            "reason": f"signal_freeze_unavailable: {type(exc).__name__}: {exc}",
            "mode": "shadow",
            "generated_at": None,
            "generated_at_utc": None,
            "snapshot_hash": None,
            "late_count": _freeze_late_count(consume_day),
            "unexplained_count": _freeze_unexplained_count(consume_day),
            "is_today": consume_day == now.strftime("%Y%m%d"),
            "freshness": "unavailable",
        }

    snapshot = verified.get("snapshot") if verified.get("status") == "ready" else None
    generated_at = snapshot.get("generated_at") if snapshot else None
    generated_at_utc = snapshot.get("generated_at_utc") if snapshot else None
    freshness = "missing"
    age_hours = None
    if snapshot:
        try:
            generated = datetime.fromisoformat(str(generated_at))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=_SHANGHAI)
            age_hours = max(0.0, (now - generated.astimezone(_SHANGHAI)).total_seconds() / 3600)
        except (TypeError, ValueError):
            freshness = "unknown"
        if freshness != "unknown":
            if consume_day == now.strftime("%Y%m%d"):
                freshness = "today"
            else:
                try:
                    delta = datetime.strptime(now.strftime("%Y%m%d"), "%Y%m%d").date() - datetime.strptime(consume_day, "%Y%m%d").date()
                    freshness = "yesterday" if delta.days == 1 else "stale"
                except ValueError:
                    freshness = "unknown"

    return {
        "ok": True,
        "date": consume_day,
        "status": verified.get("status", "invalid"),
        "reason": verified.get("reason"),
        "mode": mode,
        "generated_at": generated_at,
        "generated_at_utc": generated_at_utc,
        "snapshot_hash": snapshot.get("snapshot_hash") if snapshot else None,
        "source_tier": snapshot.get("source_tier") if snapshot else None,
        "source_date": snapshot.get("source_date") if snapshot else None,
        "target_count": len(snapshot.get("targets") or []) if snapshot else 0,
        "late_count": _freeze_late_count(consume_day),
        "unexplained_count": _freeze_unexplained_count(consume_day),
        "is_today": consume_day == now.strftime("%Y%m%d"),
        "freshness": freshness,
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
    }


def read_history(n: int = 10):
    """按日期倒序读 data/daily/*/daily_summary.json 基础字段."""
    out = []
    dirs = sorted(glob.glob(os.path.join(DAILY_DIR, "*")), reverse=True)
    for d in dirs[:n]:
        if not os.path.isdir(d):
            continue
        day = os.path.basename(d)
        # [2026-09-20] 与 read_equity_curve 同口径: 只认 8 位日期目录, 挡掉
        # `day/` 与 `2026-09-03/` 这类非规范目录（否则历史列表里会混进假日期）。
        if not (day.isdigit() and len(day) == 8):
            continue
        sp = os.path.join(d, "daily_summary.json")
        if not os.path.exists(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                s = json.load(f)
            snap = (s.get("steps") or {}).get("paper") or {}
            out.append({
                "day": s.get("day", day),
                "equity": snap.get("equity"),
                "cash": snap.get("cash"),
                "positions": snap.get("open_positions"),
                "trades": snap.get("trades_today"),
                "status": s.get("status"),
            })
        except Exception:
            continue
    return {"days": out, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def read_backtest():
    """读取最近一次连续交易日回放结果(回放引擎写 backtest_latest.json)."""
    p = os.path.join(DATA_DIR, "backtest_latest.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def read_market(n_days: int = 10):
    """读取市场情绪面板: data/market/<day>/market.json 的最近 N 个交易日.
    返回 {days:[{day, sentiment_score, market_width, limit, breadth, turnover, sector}]}."""
    mdir = os.path.join(DATA_DIR, "market")
    out = []
    if os.path.isdir(mdir):
        subs = sorted(os.listdir(mdir))
        for s in subs[-n_days:]:
            p = os.path.join(mdir, s, "market.json")
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    r = json.load(f)
                if r.get("ok"):
                    out.append({
                        "day": r.get("day"),
                        "sentiment": r.get("sentiment_score"),
                        "width": r.get("market_width"),
                        "limit": r.get("limit"),
                        "breadth": r.get("breadth"),
                        "turnover": r.get("turnover"),
                        "sector": r.get("sector"),
                    })
            except Exception:
                continue
    out.sort(key=lambda x: x.get("day", ""))
    return out


def read_degradation():
    """读取策略退化指数 + SPC 告警 + 增量学习最新结果.
    数据源: ArcticDB daily_summary (degradation_{day}) + data/reward_config.json +
            data/daily/<day>/strategy_optimization.json (如存在).
    """
    out = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "index": None,
        "spc": [],
        "alerts": [],
        "incremental_learn": None,
        "reward_config": None,
    }
    try:
        from degradation import run_full_check
        check = run_full_check(days=10)
        out["index"] = check.get("degradation_index") or {}
        # SPC 简化: 用 perf_history 跑默认 SPC
        try:
            from spc import spc_batch, DEFAULT_CFGS, IndicatorCfg
            from arctic_store import get_store
            perf = get_store().read_perf_reports(days=60)
            series_map = {}
            if perf is not None and not perf.empty:
                if "total_return" in perf.columns:
                    series_map["daily_return"] = perf["total_return"].astype(float).tail(40)
                if "max_drawdown" in perf.columns:
                    series_map["max_drawdown"] = perf["max_drawdown"].astype(float).tail(40)
            if series_map:
                out["spc"] = spc_batch(series_map, {k: DEFAULT_CFGS[k] for k in series_map if k in DEFAULT_CFGS})
            # alerts: 从 SPC + 退化指数汇总
            alerts = []
            for s in out["spc"]:
                if s.get("level") in ("P0", "P1", "P2"):
                    alerts.append({
                        "indicator": s.get("indicator"),
                        "level": s.get("level"),
                        "message": s.get("message"),
                        "violations": s.get("violations", [])[:3],
                    })
            out["alerts"] = alerts
        except Exception as e:
            out["spc_error"] = str(e)
        # 增量学习最新结果
        try:
            latest_inc = None
            days_dir = os.path.join(DATA_DIR, "daily")
            if os.path.isdir(days_dir):
                ds = sorted([d for d in os.listdir(days_dir)
                             if os.path.isdir(os.path.join(days_dir, d)) and d.isdigit()],
                            reverse=True)
                for d in ds[:3]:
                    f = os.path.join(days_dir, d, "strategy_optimization.json")
                    if os.path.exists(f):
                        try:
                            with open(f, encoding="utf-8") as fh:
                                latest_inc = json.load(fh)
                            latest_inc["_day_dir"] = d
                            break
                        except Exception:
                            continue
            out["incremental_learn"] = latest_inc
        except Exception as e:
            out["inc_error"] = str(e)
        # reward_config
        try:
            from incremental_learn import get_reward_weights
            out["reward_config"] = get_reward_weights()
        except Exception:
            pass
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------- 概念/行业 映射加载 + 聚合 (基于 data/concept_map.json + industry_map.json, 同花顺同源) ----------
_TAGMAP_CACHE: dict[str, tuple[float, dict]] = {}      # fname -> (mtime_or_load_ts, map)
_SNAPSHOT_CACHE: dict[str, tuple[float, list]] = {}     # 'snapshot' -> (loaded_ts, rows)
_AGG_CACHE: dict[str, tuple[float, dict]] = {}          # cache_key -> (ts, payload)
_CACHE_TTL = 30.0                                        # 聚合结果缓存 30 秒 (前端 3s 轮询直接吃缓存)


def _load_tag_map(fname: str) -> dict:
    """读 data/<fname>.json -> {symbol:{name,tags:[..]}}
    带内存缓存 + 文件 mtime 失效 (避免每请求读盘).
    """
    p = os.path.join(DATA_DIR, fname)
    try:
        mtime = os.path.getmtime(p) if os.path.exists(p) else 0
    except Exception:
        mtime = 0
    if fname in _TAGMAP_CACHE:
        ts, m, m_mtime = _TAGMAP_CACHE[fname][0], _TAGMAP_CACHE[fname][1], _TAGMAP_CACHE[fname][2] if len(_TAGMAP_CACHE[fname]) > 2 else 0
        if ts == mtime:
            return m
    if not os.path.exists(p):
        _TAGMAP_CACHE[fname] = (0, {}, 0)
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        m = j.get("map", {}) or {}
        _TAGMAP_CACHE[fname] = (mtime, m, mtime)
        return m
    except Exception:
        return {}


def _today_snapshot_rows():
    """DuckDB v_universe_snapshot 的 rows -> [{symbol, change_pct, amount, turnover, last_price, ...}, ...]
    注: canon 列存的是纯数字 (无 .SH/.SZ 后缀), 已自动加 .SH/.SZ 规范化.
    通过共享 _duckdb_query (全局锁 + read_only) 访问, 避免 view 在多次连接下偶发返回空集.
    带 30s TTL 缓存 (避免每请求重新聚合 5676 行 + 5559 symbols).
    """
    import time as _t
    now = _t.time()
    cached = _SNAPSHOT_CACHE.get('snapshot')
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    try:
        rows = _duckdb_query(
            "SELECT canon, date, close, change_pct, turnover, amount, "
            "       pass_basic, signal, composite_score FROM v_universe_snapshot "
            "ORDER BY amount DESC NULLS LAST"
        )
        out = []
        for r in rows:
            raw = str(r[0]) if r[0] else ""
            if raw.isdigit() and len(raw) == 6:
                sym = f"{raw}.SH" if raw.startswith("6") or raw.startswith("9") else f"{raw}.SZ"
            else:
                sym = raw
            out.append({
                "symbol": sym,
                "canon_raw": raw,
                "date": r[1],
                "close": r[2],
                "change_pct": r[3],
                "turnover": r[4],
                "amount": r[5],
                "pass_basic": r[6],
                "signal": r[7],
                "composite_score": r[8],
            })
        _SNAPSHOT_CACHE['snapshot'] = (now, out)
        return out
    except Exception:
        return _SNAPSHOT_CACHE.get('snapshot', (0, []))[1]


def read_concept(tag: str | None = None, top_n: int = 50, sort_by: str = "strength"):
    """返回概念聚合 (带 30s TTL 缓存, 按参数组合 key)
    sort_by: strength | change_pct | count | amount | turnover
    tag: 可选筛指定概念名
    """
    import time as _t
    cache_key = f"concept|tag={tag}|top={top_n}|sort={sort_by}"
    now = _t.time()
    cached = _AGG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    out = _compute_concept(tag=tag, top_n=top_n, sort_by=sort_by)
    _AGG_CACHE[cache_key] = (now, out)
    if len(_AGG_CACHE) > 64:
        items = sorted(_AGG_CACHE.items(), key=lambda x: x[1][0])
        for k, _ in items[:len(items)//2]:
            _AGG_CACHE.pop(k, None)
    return out


def _compute_concept(tag: str | None = None, top_n: int = 50, sort_by: str = "strength"):
    """重聚合实现 (内部). 同 read_concept 原签名."""
    # 注: 返回 payload 沿用 read_concept 的字段格式
    tag_map = _load_tag_map("concept_map.json")
    snap = _today_snapshot_rows()
    snap_by_sym = {r["symbol"]: r for r in snap}
    # 反向索引： tag -> [symbol]
    tag2syms: dict[str, list[str]] = {}
    for sym, v in tag_map.items():
        for t in (v.get("tags") or []):
            tag2syms.setdefault(t, []).append(sym)
    # 聚合
    items = []
    for t, syms in tag2syms.items():
        rows = [snap_by_sym[s] for s in syms if s in snap_by_sym]
        if not rows:
            # 没有今日行情数据 -> 仍保留条目, 强度为 None
            items.append({
                "name": t,
                "count": len(syms),
                "today": 0,
                "avg_change_pct": None,
                "median_change_pct": None,
                "total_amount": 0,
                "avg_turnover": None,
                "limit_up_count": 0,
                "strong_up_count": 0,
                "leaders": [],
                "heat_score": 0.0,
            })
            continue
        changes = [r.get("change_pct") or 0 for r in rows]
        amounts = [r.get("amount") or 0 for r in rows]
        turnovers = [r.get("turnover") or 0 for r in rows]
        avg_chg = sum(changes) / len(changes)
        sorted_changes = sorted(changes)
        med_chg = sorted_changes[len(sorted_changes) // 2]
        total_amt = sum(amounts)
        avg_to = sum(turnovers) / len(turnovers)
        # 龙头按涨幅 top3
        leaders = sorted(rows, key=lambda r: -(r.get("change_pct") or 0))[:3]
        # 热度 = 平均涨幅 * log(参与家数+1) + log(总成交+1)/5
        import math
        heat = round(avg_chg * math.log(len(rows) + 1)
                      + math.log(max(total_amt, 1) + 1) / 5, 2)
        items.append({
            "name": t,
            "count": len(syms),
            "today": len(rows),
            "avg_change_pct": round(avg_chg, 2),
            "median_change_pct": round(med_chg, 2),
            "total_amount": round(total_amt, 0),
            "avg_turnover": round(avg_to, 2),
            "limit_up_count": sum(1 for r in rows if (r.get("change_pct") or 0) >= 9.5),
            "strong_up_count": sum(1 for r in rows if (r.get("change_pct") or 0) >= 5.0),
            "leaders": [{"symbol": r["symbol"], "name": r.get("name") or "",
                         "change_pct": r.get("change_pct"),
                         "amount": r.get("amount")} for r in leaders],
            "heat_score": heat,
        })
    # 排序
    if sort_by == "strength":
        items.sort(key=lambda x: -x.get("heat_score", 0))
    elif sort_by == "change_pct":
        items.sort(key=lambda x: -(x.get("avg_change_pct") or -999))
    elif sort_by == "count":
        items.sort(key=lambda x: -x.get("count", 0))
    elif sort_by == "amount":
        items.sort(key=lambda x: -x.get("total_amount", 0))
    elif sort_by == "turnover":
        items.sort(key=lambda x: -(x.get("avg_turnover") or 0))
    if tag:
        items = [it for it in items if tag in it["name"]]
    # 元信息
    fetched_at = ""
    try:
        with open(os.path.join(DATA_DIR, "concept_map.json"), encoding="utf-8") as f:
            fetched_at = json.load(f).get("fetched_at", "")
    except Exception:
        pass
    return {
        "ok": True,
        "kind": "concept",
        "source": "concept_map.json + v_universe_snapshot",
        "fetched_at": fetched_at,
        "total_concepts": len(items),
        "total_symbols": len(tag_map),
        "items": items[:max(1, top_n)],
        "sort_by": sort_by,
        "tag_filter": tag or "",
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def read_industry(tag: str | None = None, top_n: int = 50, sort_by: str = "strength"):
    """行业聚合 (带 30s TTL 缓存)."""
    import time as _t
    cache_key = f"industry|tag={tag}|top={top_n}|sort={sort_by}"
    now = _t.time()
    cached = _AGG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    out = _compute_industry(tag=tag, top_n=top_n, sort_by=sort_by)
    _AGG_CACHE[cache_key] = (now, out)
    return out


def _compute_industry(tag: str | None = None, top_n: int = 50, sort_by: str = "strength"):
    """行业聚合实现 (内部)."""
    tag_map = _load_tag_map("industry_map.json")
    snap = _today_snapshot_rows()
    snap_by_sym = {r["symbol"]: r for r in snap}
    tag2syms: dict[str, list[str]] = {}
    for sym, v in tag_map.items():
        # 行业字段是 "大类-子类-细类", 用三段式全部纳入聚合 (按 "-一级" 归大类)
        for full in (v.get("tags") or []):
            # 拆出第一级（一级-二级-三级）
            parts = [p.strip() for p in full.split("-") if p.strip()]
            if parts:
                l1 = parts[0]
                tag2syms.setdefault(l1, []).append(sym)
            tag2syms.setdefault(full, []).append(sym)
    items = []
    for t, syms in tag2syms.items():
        rows = [snap_by_sym[s] for s in syms if s in snap_by_sym]
        if not rows:
            items.append({
                "name": t, "count": len(syms), "today": 0,
                "avg_change_pct": None, "median_change_pct": None,
                "total_amount": 0, "avg_turnover": None,
                "limit_up_count": 0, "strong_up_count": 0,
                "leaders": [], "heat_score": 0.0,
            })
            continue
        changes = [r.get("change_pct") or 0 for r in rows]
        amounts = [r.get("amount") or 0 for r in rows]
        turnovers = [r.get("turnover") or 0 for r in rows]
        avg_chg = sum(changes) / len(changes)
        sorted_changes = sorted(changes)
        med_chg = sorted_changes[len(sorted_changes) // 2]
        total_amt = sum(amounts)
        avg_to = sum(turnovers) / len(turnovers)
        leaders = sorted(rows, key=lambda r: -(r.get("change_pct") or 0))[:3]
        import math
        heat = round(avg_chg * math.log(len(rows) + 1)
                      + math.log(max(total_amt, 1) + 1) / 5, 2)
        items.append({
            "name": t, "count": len(syms), "today": len(rows),
            "avg_change_pct": round(avg_chg, 2),
            "median_change_pct": round(med_chg, 2),
            "total_amount": round(total_amt, 0),
            "avg_turnover": round(avg_to, 2),
            "limit_up_count": sum(1 for r in rows if (r.get("change_pct") or 0) >= 9.5),
            "strong_up_count": sum(1 for r in rows if (r.get("change_pct") or 0) >= 5.0),
            "leaders": [{"symbol": r["symbol"], "name": r.get("name") or "",
                         "change_pct": r.get("change_pct"),
                         "amount": r.get("amount")} for r in leaders],
            "heat_score": heat,
        })
    if sort_by == "strength":
        items.sort(key=lambda x: -x.get("heat_score", 0))
    elif sort_by == "change_pct":
        items.sort(key=lambda x: -(x.get("avg_change_pct") or -999))
    elif sort_by == "count":
        items.sort(key=lambda x: -x.get("count", 0))
    elif sort_by == "amount":
        items.sort(key=lambda x: -x.get("total_amount", 0))
    elif sort_by == "turnover":
        items.sort(key=lambda x: -(x.get("avg_turnover") or 0))
    if tag:
        items = [it for it in items if tag in it["name"]]
    fetched_at = ""
    try:
        with open(os.path.join(DATA_DIR, "industry_map.json"), encoding="utf-8") as f:
            fetched_at = json.load(f).get("fetched_at", "")
    except Exception:
        pass
    return {
        "ok": True,
        "kind": "industry",
        "source": "industry_map.json + v_universe_snapshot",
        "fetched_at": fetched_at,
        "total_industries": len(items),
        "total_symbols": len(tag_map),
        "items": items[:max(1, top_n)],
        "sort_by": sort_by,
        "tag_filter": tag or "",
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def read_concept_symbol(symbol: str) -> dict:
    """查单只股票的概念/行业归属"""
    cm = _load_tag_map("concept_map.json")
    im = _load_tag_map("industry_map.json")
    return {
        "ok": True,
        "symbol": symbol,
        "concepts": (cm.get(symbol) or {}).get("tags", []),
        "industries": (im.get(symbol) or {}).get("tags", []),
        "name": (cm.get(symbol) or im.get(symbol) or {}).get("name", ""),
    }


def read_concept_name_symbols(name: str) -> dict:
    """按概念名查全部成分股 symbol (精确匹配 + 一级前缀匹配, 带 60s 缓存)"""
    import time as _t
    cache_key = f"concept_sym|{name}"
    now = _t.time()
    cached = _AGG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 60.0:
        return cached[1]
    out = _compute_concept_name_symbols(name)
    _AGG_CACHE[cache_key] = (now, out)
    return out


def _compute_concept_name_symbols(name: str) -> dict:
    cm = _load_tag_map("concept_map.json")
    exact, prefix = [], []
    for sym, v in cm.items():
        tags = v.get("tags") or []
        if name in tags:
            exact.append(sym)
        else:
            for t in tags:
                if name in t and t != name and name not in prefix:
                    prefix.append(t)
    return {"ok": True, "name": name, "symbols": exact, "related_tags": sorted(set(prefix))[:20]}


def read_industry_name_symbols(name: str) -> dict:
    """按行业名查成分股 (带 60s 缓存)."""
    import time as _t
    cache_key = f"industry_sym|{name}"
    now = _t.time()
    cached = _AGG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 60.0:
        return cached[1]
    out = _compute_industry_name_symbols(name)
    _AGG_CACHE[cache_key] = (now, out)
    return out


def _compute_industry_name_symbols(name: str) -> dict:
    im = _load_tag_map("industry_map.json")
    exact, prefix = [], set()
    target_parts = [p.strip() for p in name.split("-") if p.strip()]
    for sym, v in im.items():
        for full in (v.get("tags") or []):
            full_parts = [p.strip() for p in full.split("-") if p.strip()]
            # 完整三段式匹配
            if full == name:
                exact.append(sym); break
            # 一级匹配 ('汽车'): 含此一级的任一三级 tag 都算
            if len(target_parts) == 1 and full_parts and full_parts[0] == name:
                exact.append(sym); break
            # 二级匹配
            if len(target_parts) == 2 and len(full_parts) >= 2 \
               and full_parts[0] == target_parts[0] and full_parts[1] == target_parts[1]:
                exact.append(sym); break
    return {"ok": True, "name": name, "symbols": sorted(set(exact)), "related_tags": sorted(prefix)[:20]}


def read_drl():
    """读取 DRL 训练产物: data/drl/<YYYYMMDD>/train_meta.json + pre_drl_brief.json.
    按日期倒序, 取最近 3 天的训练快照 + 关联的 brief."""
    out = {"days": [], "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    drl_dir = os.path.join(DATA_DIR, "drl")
    if not os.path.isdir(drl_dir):
        return out
    days = sorted(
        [d for d in os.listdir(drl_dir) if os.path.isdir(os.path.join(drl_dir, d))],
        reverse=True,
    )[:3]
    for day_dir in days:
        meta_path = os.path.join(drl_dir, day_dir, "train_meta.json")
        brief_path = os.path.join(drl_dir, day_dir, "pre_drl_brief.json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        brief = {}
        if os.path.exists(brief_path):
            try:
                with open(brief_path, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("ok"):
                    brief = d.get("brief") or {}
                    brief["_meta"] = d.get("meta") or {}
            except Exception:
                brief = {}
        out["days"].append({
            "day": meta.get("day") or day_dir,
            "day_dir": day_dir,
            "meta": meta,
            "brief": brief,
        })
    return out


def _load_perf_llm_extras() -> dict:
    """从 performance_report.json 文件里读 LLM 写入的两个附加字段, 不存在则返回空 dict."""
    p = os.path.join(DATA_DIR, "performance_report.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return {}
    out = {}
    if isinstance(j, dict):
        if j.get("llm_commentary"):
            out["llm_commentary"] = j["llm_commentary"]
        if j.get("llm_commentary_meta"):
            out["llm_commentary_meta"] = j["llm_commentary_meta"]
    return out


def read_perf():
    """读取绩效归因报告: data/performance_report.json.
    若文件缺失或过期(<1小时)则现算(调用 performance_report.compute_report).
    无论走哪条分支, 都会把文件里 LLM 写入的 llm_commentary / llm_commentary_meta
    并入响应, 避免 LLM 点评更新后 dashboard 看不到."""
    p = os.path.join(DATA_DIR, "performance_report.json")
    if os.path.exists(p):
        try:
            age_s = os.path.getmtime(p)
            if (datetime.now().timestamp() - age_s) < 3600:
                with open(p, encoding="utf-8") as f:
                    j = json.load(f)
                # 文件已含 LLM 字段, 直接返回即可
                return j
        except Exception:
            pass
    try:
        import performance_report as pr
        rows = pr.load_daily_equities()
        r = pr.compute_report(rows, bench=True)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 现算结果不含 LLM 字段, 把文件里的 LLM 点评并入 (确保 dashboard 能渲染)
    r.update(_load_perf_llm_extras())
    return r


def read_weights():
    """读取自适应因子权重: data/weights.json (weight_optimizer 写入).
    返回 {weights:{factor:val}, meta:{ic依据, method, updated}}."""
    p = os.path.join(DATA_DIR, "weights.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                w = json.load(f)
            meta = w.get("meta") or {}
            weights = w.get("weights") or {}
            return {
                "ok": True,
                "weights": weights,
                "meta": meta,
                "updated": w.get("updated") or meta.get("updated") or "",
            }
        except Exception:
            pass
    return {"ok": False, "weights": {}, "meta": {}}


def _proc_alive(pid):
    """探测 pid 进程是否存活。

    [2026-09-21] 改为委托 `proc_alive.alive()` —— 本函数原先自己用 OpenProcess 实现,
    既没有 daemon 那条"句柄残留 => 已终止进程被 OpenProcess 成功打开"的退出码校验,
    又**把『打不开』一律当成『已死』**: 实测以交互用户身份检查以 LocalSystem 运行的
    守护/面板时, OpenProcess 因拒绝访问失败, 告警中心于是把三个**确实存活**的进程报成
    `[critical] 进程未存活`。跨身份(面板被外部脚本以别的身份拉起)就会规律性出现假 CRITICAL。
    正确判据: ERROR_ACCESS_DENIED 本身**证明进程存在**; 详见 `src/proc_alive.py`。
    """
    from proc_alive import alive as _alive
    return _alive(pid)


def read_overview():
    """系统总览: 门控 + 进程健康 + 数据水位 + 风控事件流.
    (对照专业仪表盘"系统状态与AI洞察"与"风险风控"面板.)"""
    out = {"ok": True, "ts": _now()}
    # 1) 门控状态 (IC 门控当前档位/暴露/IC)
    gp = os.path.join(DATA_DIR, "factor_gate_state.json")
    try:
        with open(gp, encoding="utf-8") as f:
            g = json.load(f)
        out["gate"] = {k: g.get(k) for k in (
            "regime", "raw_regime", "exposure_mult", "freeze_new_buys",
            "interval_days", "ic_mean", "ic_neg_share", "ic_ir",
            "ic_as_of", "daily_loss", "loss_flag")}
        out["gate"]["reasons"] = g.get("reasons") or []
        out["gate"]["hyst"] = g.get("hyst") or {}
    except Exception as e:
        out["gate"] = {"error": str(e)}
    # 2) 进程健康 (pid 文件 + 引擎心跳)
    log_dir = os.path.join(_BASE, "logs")
    procs = {}
    for name, pidfile in (("daemon", "daemon.pid"),
                          ("engine", "engine.pid"),
                          ("dashboard", "dashboard.pid")):
        p = os.path.join(log_dir, pidfile)
        pid = None
        try:
            if os.path.exists(p):
                pid = int(open(p, encoding="utf-8").read().strip() or 0)
        except Exception:
            pid = None
        procs[name] = {"pid": pid, "alive": _proc_alive(pid) if pid else None}
    out["procs"] = procs
    lv = os.path.join(DATA_DIR, "live_state.json")
    try:
        with open(lv, encoding="utf-8") as f:
            lj = json.load(f)
        out["engine_heartbeat"] = {
            "updated": lj.get("updated"), "day": lj.get("day"),
            "in_session": lj.get("in_session"), "mode": lj.get("mode")}
    except Exception as e:
        out["engine_heartbeat"] = {"error": str(e)}
    try:
        with open(os.path.join(DATA_DIR, "data_update_last.json"),
                  encoding="utf-8") as f:
            out["data_update_mark"] = json.load(f)
    except Exception:
        out["data_update_mark"] = None
    # 3) 数据水位 (巡检报告 check_data_report.json)
    rep = os.path.join(DATA_DIR, "check_data_report.json")
    try:
        with open(rep, encoding="utf-8") as f:
            j = json.load(f)
        stale = [h["table"] for h in j.get("health", []) if not h.get("ok")]
        out["data"] = {
            "report_ts": j.get("run_ts"), "base": j.get("base_trade_day"),
            "stale": stale,
            "checks": {"ok": sum(1 for c in j.get("checks", [])
                                 if c["status"] == "ok"),
                       "total": len(j.get("checks", []))}}
    except Exception as e:
        out["data"] = {"error": str(e)}
    # 4) 风控事件流 (守护/引擎日志尾部关键字)
    import re
    kw = re.compile(
        r"(IC_GATE|门控|IC门控|熔断|CIRCUIT|回撤|风控|止损|变点|factor_health|隔离|freeze|异常退出)",
        re.I)
    events = []
    for fn in ("daemon_tail.log", "live_engine.log"):
        p = os.path.join(log_dir, fn)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-400:]
        except Exception:
            continue
        for ln in reversed(lines):
            if kw.search(ln):
                events.append({"ts": ln[:19], "src": fn.split(".")[0],
                               "line": ln.strip()[:180]})
                if len(events) >= 8:
                    break
    out["risk_events"] = events
    return out


def read_kline(sym: str, days: int = 200):
    """单标的 K 线 + 技术指标 (h5i daily_bars).

    返回正序 bars: {date,o,h,l,c,vol, amount, ma5/10/20/60, dif/dea/macd(12,26,9), rsi(14)}.
    """
    code = str(sym or "").strip().upper().split(".")[0]
    if len(code) != 6 or not code.isdigit():
        return {"ok": False, "error": f"无效代码: {sym}"}
    days = max(30, min(int(days or 200), 300))
    try:
        import pandas as pd
        import factor_fusion as ff
        n = days + 90  # 额外 90 日作指标回看
        df = ff._sql(
            f"SELECT CAST(ts AS DATE) d, open, high, low, close, volume, amount "
            f"FROM daily_bars WHERE symbol='{code}' "
            f"ORDER BY d DESC LIMIT {n}")
    except Exception as e:
        return {"ok": False, "error": f"读取行情失败: {e}"}
    if df is None or df.empty:
        return {"ok": False, "error": f"库中无 {code} 行情"}
    df = df.sort_values("d").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2
    # RSI(14) Wilder
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / 14, adjust=False).mean()
    al = loss.ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + ag / al.replace(0, 1e-12))
    df = df.tail(days)
    out = {"ok": True, "sym": code, "name": code, "bars": []}
    for r in df.itertuples():
        b = {"date": str(r.d), "o": _f(r.open), "h": _f(r.high),
             "l": _f(r.low), "c": _f(r.close), "v": _f(r.volume),
             "amount": _f(r.amount), "ma5": _f(r.ma5), "ma10": _f(r.ma10),
             "ma20": _f(r.ma20), "ma60": _f(r.ma60), "dif": _f(r.dif),
             "dea": _f(r.dea), "macd": _f(r.macd), "rsi": _f(r.rsi)}
        out["bars"].append(b)
    return out


def _f(v):
    """float NaN -> None (供 json)."""
    import math
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        if hasattr(v, "item"):
            x = v.item()
            if isinstance(x, float) and math.isnan(x):
                return None
            return x
        return v
    except Exception:
        return v


def _equity_series():
    """逐日回执 equity 序列 (data/daily/*/daily_summary.json), 按日去重升序.

    兼容两种结构: 顶层 {equity} 或新格式 {summary:{equity}}.
    """
    seq = {}
    for d in sorted(glob.glob(os.path.join(DAILY_DIR, "*"))):
        # [2026-09-20 泛化] 原为硬编码 `if os.path.basename(d) in ("day",)`（只挡字面量
        # "day"）。实测生产里出现过**两类**非规范目录: `day/`（CLI 占位符 --day）
        # 与 `2026-09-03/`（调用点把带横线的 day 当 day_dir 传, 见 P1-LLMHB）。
        # 硬编码只挡得住第一类 ⇒ 改为「必须 8 位数字」这一口径。
        _bn = os.path.basename(d)
        if not (_bn.isdigit() and len(_bn) == 8):
            continue  # 非规范日期目录一律跳过
        sp = os.path.join(d, "daily_summary.json")
        if not os.path.exists(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        day = j.get("day") or os.path.basename(d)
        eq = j.get("equity")
        if eq is None:
            s = j.get("summary")
            if isinstance(s, dict):
                eq = s.get("equity")
        if not day or eq is None:
            continue
        # 兼容 YYYYMMDD / YYYY-MM-DD 目录
        if isinstance(day, str) and len(day) == 8 and day.isdigit():
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        seq[str(day)] = float(eq)
    return [{"day": k, "equity": v} for k, v in sorted(seq.items())]


def read_daily_series(limit: int = 20):
    """返回最近交易日的净值、回撤和日收益序列。"""
    try:
        rows = _equity_series()
    except (OSError, TypeError, ValueError):
        return []
    if not rows:
        return []

    try:
        base = float(rows[0]["equity"])
    except (KeyError, TypeError, ValueError):
        return []
    if base <= 0:
        return []

    enriched = []
    peak = 0.0
    previous_equity = None
    for row in rows:
        try:
            equity = float(row["equity"])
        except (KeyError, TypeError, ValueError):
            continue
        nav = equity / base
        peak = max(peak, nav)
        dd = nav / peak - 1.0 if peak else 0.0
        daily_return = 0.0 if previous_equity is None else equity / previous_equity - 1.0
        enriched.append({
            "day": str(row["day"]),
            "equity": round(equity, 2),
            "nav": round(nav, 4),
            "dd": round(dd, 4),
            "daily_return": round(daily_return, 6),
        })
        previous_equity = equity

    if limit <= 0:
        return []
    return enriched[-limit:]


def read_curve():
    """组合净值/回撤序列 (相对首日净值 + 历史回撤)."""
    rows = _equity_series()
    if len(rows) < 2:
        return {"ok": False, "error": "回执样本不足", "rows": rows}
    base = rows[0]["equity"]
    peak = -1e18
    out = []
    for r in rows:
        nav = r["equity"] / base
        peak = max(peak, nav)
        dd = (nav / peak - 1) * 100 if peak else 0
        out.append({"day": r["day"], "equity": r["equity"],
                    "nav": nav, "dd": dd})
    return {"ok": True, "rows": out}


def read_monthly():
    """月度收益矩阵 (基于逐日回执 equity 月末值)."""
    rows = _equity_series()
    if len(rows) < 2:
        return {"ok": False, "error": "样本不足", "months": [], "years": []}
    bym = {}
    for r in rows:
        m = r["day"][:7]
        bym.setdefault(m, []).append(r["equity"])
    ms = sorted(bym)
    cells = {}
    prev_last = rows[0]["equity"]
    for m in ms:
        last = bym[m][-1]
        cells[m] = (last / prev_last - 1) * 100 if prev_last else None
        prev_last = last
    years = sorted({m[:4] for m in ms})
    return {"ok": True, "years": years, "cells": cells}


_INDUSTRY_MAP = None


def _industry_map():
    """惰性加载同花顺行业映射 (industry_map.json: canon -> {name, tags[三级]})."""
    global _INDUSTRY_MAP
    if _INDUSTRY_MAP is None:
        try:
            p = os.path.join(DATA_DIR, "industry_map.json")
            with open(p, encoding="utf-8") as f:
                _INDUSTRY_MAP = (json.load(f) or {}).get("map") or {}
        except Exception:
            _INDUSTRY_MAP = {}
    return _INDUSTRY_MAP


def read_holdings_profile():
    """持仓的行业 / 市值双维分布.

    行业: industry_map.json (同花顺一级行业, 按持仓市值聚合)
    市值: valuation_snapshot 最新快照 total_mv(float_mv), 分 小盘<100 / 中盘100-500 / 大盘>=500 亿
    """
    lv = os.path.join(DATA_DIR, "live_state.json")
    pos = []
    try:
        with open(lv, encoding="utf-8") as f:
            lj = json.load(f)
        pos = [p for p in (lj.get("positions") or []) if p and p.get("qty", 0) > 0]
    except Exception:
        pass
    if not pos:
        return {"ok": True, "positions": [], "message": "空仓", "industries": [], "caps": []}
    total = sum((p.get("mv") or 0) for p in pos) or 1
    imap = _industry_map()
    # 市值: 最新估值快照
    code6 = [str(p.get("canon", "")).split(".")[0] for p in pos]
    inlist = ",".join(f"'{c}'" for c in code6)
    mv_map = {}
    try:
        import factor_fusion as ff
        df = ff._sql(
            f"SELECT symbol, total_mv, float_mv FROM valuation_snapshot "
            f"WHERE CAST(ts AS DATE) = (SELECT MAX(CAST(ts AS DATE)) FROM valuation_snapshot) "
            f"AND symbol IN ({inlist})")
        for r in df.itertuples():
            mv_map[str(r.symbol)] = {"total_mv": _f(r.total_mv), "float_mv": _f(r.float_mv)}
    except Exception:
        pass

    inds, capb = {}, {"大盘": [], "中盘": [], "小盘": []}
    out_pos = []
    for p in pos:
        canon = str(p.get("canon", ""))
        rec = imap.get(canon) or {}
        tags = rec.get("tags") or []
        ind1 = tags[0].split("-")[0] if tags else None
        mv = p.get("mv") or 0
        cap = (mv_map.get(canon.split(".")[0]) or {}).get("total_mv")
        bucket = None
        if cap is not None:
            bucket = "大盘" if cap >= 500 else ("中盘" if cap >= 100 else "小盘")
        out_pos.append({
            "canon": canon, "name": rec.get("name") or p.get("name") or canon,
            "qty": p.get("qty"), "mv": mv, "weight_pct": mv / total * 100,
            "pnl_amt": p.get("pnl_amt"), "pnl_pct": p.get("pnl_pct"),
            "industry": ind1, "industry_full": tags[0] if tags else None,
            "total_mv": cap, "cap_bucket": bucket})
        if ind1:
            inds.setdefault(ind1, {"mv": 0.0, "codes": []})
            inds[ind1]["mv"] += mv
            inds[ind1]["codes"].append(canon.split(".")[0])
        if bucket:
            capb[bucket].append({"canon": canon, "mv": mv})
    ind_rows = [{"name": k, "mv": v["mv"], "weight_pct": v["mv"] / total * 100,
                 "codes": v["codes"]} for k, v in
                sorted(inds.items(), key=lambda x: -x[1]["mv"])]
    cap_rows = [{"bucket": k, "positions": v,
                 "weight_pct": (sum(x["mv"] for x in v) / total * 100) if v else 0,
                 "count": len(v)} for k, v in
                (("大盘", capb["大盘"]), ("中盘", capb["中盘"]), ("小盘", capb["小盘"]))]
    return {"ok": True, "positions": out_pos, "industries": ind_rows,
            "caps": cap_rows}


def read_riskops():
    """风险-绩效指标 + 运维计数 (回执日收益口径).

    含: VaR95/99(历史分位, 样本短时注明)、单日盈亏、滚动夏普(近20/60日)、
    Sortino、胜率、盈亏比、最大回撤; 错误计数(近24h 守护/引擎日志);
    当日成交统计.
    """
    import numpy as np
    rows = _equity_series()
    out = {"ok": True, "n_days": len(rows), "note": None, "metrics": {}, "ops": {}}
    if len(rows) < 3:
        out["ok"] = False
        out["note"] = "回执样本不足(需≥3个交易日), 指标不可靠"
        return out
    eq = [r["equity"] for r in rows]
    rets = np.array([eq[i + 1] / eq[i] - 1 for i in range(len(eq) - 1)]) * 100
    m = out["metrics"]
    m["daily_pnl"] = float(eq[-1] - eq[-2])
    m["daily_pnl_pct"] = float((eq[-1] / eq[-2] - 1) * 100)
    m["max_dd"] = float(min(r["dd"] for r in read_curve().get("rows", []))) if len(rows) >= 2 else 0.0
    if len(rets) >= 5:
        m["var95"] = float(np.percentile(rets, 5))
        m["var99"] = float(np.percentile(rets, 1))
    else:
        m["var95"] = m["var99"] = None
    sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    m["vol_annual"] = sd * np.sqrt(252) if sd else None
    m["sharpe60"] = (float(np.mean(rets)) / sd * np.sqrt(252)) if sd else None
    # 短样本滚动夏普仅近 20/60 日
    for w in (20, 60):
        rw = rets[-w:]
        sw = float(np.std(rw, ddof=1)) if len(rw) > 2 else 0.0
        m[f"sharpe_w{w}"] = (float(np.mean(rw)) / sw * np.sqrt(252)) if sw else None
    # Sortino (downside)
    down = rets[rets < 0]
    dsd = float(np.std(down, ddof=1)) if len(down) > 1 else 0.0
    m["sortino"] = (float(np.mean(rets)) / dsd * np.sqrt(252)) if dsd else None
    gains = rets[rets > 0]
    losses = rets[rets < 0]
    m["win_rate"] = float(len(gains) / len(rets)) * 100 if len(rets) else None
    m["pl_ratio"] = (float(np.mean(gains)) / abs(float(np.mean(losses)))
                     if len(gains) and len(losses) and np.mean(losses) else None)
    if len(rets) < 20:
        m["_short_sample"] = True
    # ops: 错误计数(24h)
    log_dir = os.path.join(_BASE, "logs")
    kws = ("error", "traceback", "exception", " failed", "失败")
    err24 = 0
    for fn in ("daemon_tail.log", "live_engine.log"):
        p = os.path.join(log_dir, fn)
        try:
            if os.path.exists(p) and (datetime.now().timestamp() - os.path.getmtime(p)) < 86400 * 2:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()[-4000:]
                err24 += sum(1 for ln in lines if any(k in ln.lower() for k in kws))
        except Exception:
            pass
    out["ops"]["err_24h"] = err24
    # 当日成交统计 (state.json trades_history today)
    try:
        with open(os.path.join(DATA_DIR, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        th = st.get("trades_history") or {}
        today = datetime.now().strftime("%Y-%m-%d")
        tx = th.get(today) or th.get(datetime.now().strftime("%Y%m%d")) or []
        out["ops"]["today_trades"] = {"buy": sum(1 for t in tx if t.get("type") == "buy"),
                                      "sell": sum(1 for t in tx if t.get("type") == "sell"),
                                      "fee": round(sum(float(t.get("fee") or 0) for t in tx), 2)}
        # 持仓敞口 = 多头持仓市值/总权益 (现货无融资, 规范'当前杠杆水平'的现货等价口径: 杠杆恒 ×1.0)
        _eq, _cash = st.get("equity"), st.get("cash")
        if isinstance(_eq, (int, float)) and _eq > 0:
            _mv = max(_eq - (_cash or 0), 0.0)
            out["metrics"]["exposure_pct"] = round(_mv / _eq * 100, 2)
            out["metrics"]["leverage"] = 1.0
    except Exception:
        pass
    # 引擎 tick 处理耗时统计 (成交处理延迟, 由 realtime_engine 每 tick 埋点写入)
    try:
        with open(os.path.join(DATA_DIR, "live_state.json"), encoding="utf-8") as f:
            _lj = json.load(f)
        out["ops"]["tick_ms"] = (_lj.get("ops") or {}).get("tick_ms")
    except Exception:
        out["ops"]["tick_ms"] = None
    return out


def _engine_expected_now(now=None) -> bool:
    """此刻盘中引擎**本该**在运行吗？（交易日 08:30~15:03）

    用来把"预期内的退出"从告警里摘掉：收盘后引擎本就不该存在，报 critical 是噪音。
    窗口取自守护的实际调度(daemon.py: 08:30 启动 / 15:03 收盘)，不另造时间。
    """
    from datetime import time as _t
    now = now or datetime.now()
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from trading_calendar import is_trading_day as _itd
        if not _itd(now.date()):
            return False
    except Exception:  # noqa: BLE001
        # 交易日历不可用时**不**据此静音(宁可报警, 不可漏报)
        return True
    return _t(8, 30) <= now.time() < _t(15, 3)


def read_alerts():
    """告警中心: 规则评估 (门控/进程/引擎心跳/数据滞后/回撤/单日亏损/冻结)."""
    out = {"ok": True, "alerts": [], "critical": 0, "warn": 0}
    adds = lambda level, rule, detail: out["alerts"].append(
        {"level": level, "rule": rule, "detail": detail,
         "ts": datetime.now().strftime("%H:%M:%S")})
    # 进程 / 引擎心跳 / 门控 (复用 read_overview 之文件源, 避免递归请求)
    gp = os.path.join(DATA_DIR, "factor_gate_state.json")
    try:
        with open(gp, encoding="utf-8") as f:
            g = json.load(f)
    except Exception:
        g = {}
    regime = g.get("regime")
    if regime in ("risk",):
        adds("critical", "IC门控档位", f"regime={regime} exposure×{g.get('exposure_mult')}")
    elif regime in ("caution",):
        adds("warn", "IC门控档位", f"regime={regime} exposure×{g.get('exposure_mult')}")
    if g.get("freeze_new_buys"):
        adds("critical", "冻结新买入", "今日触发单日亏损防御, 暂停开新仓")
    # 进程/心跳
    log_dir = os.path.join(_BASE, "logs")
    for name, pidfile in (("守护", "daemon.pid"), ("盘中引擎", "engine.pid"), ("仪表台", "dashboard.pid")):
        p = os.path.join(log_dir, pidfile)
        pid = None
        try:
            if os.path.exists(p):
                pid = int(open(p, encoding="utf-8").read().strip() or 0)
        except Exception:
            pid = None
        # [2026-09-21] 三态报告: 探测受限时**不得**声称"未存活"(假 CRITICAL 会让运维去重启
        # 健康进程, 比不报警更糟)。见 src/proc_alive.py。
        try:
            from proc_alive import probe as _probe
            st = _probe(pid) if pid else False
        except Exception:  # noqa: BLE001
            st = None
        if st is False:
            # [2026-09-21] 引擎只在交易日 08:30~15:03 该活着 —— 收盘后它**本就该退出**,
            # 此前一律报 critical, 于是每天夜里面板都挂着一条假 critical(实测 23:50 仍在报)。
            # 与 HALTED/夜间状态机同一条纪律: 预期内的缺席不是故障, 不该喊狼来了。
            # 另两项(守护/仪表台)常驻, 不设窗口。
            if name == "盘中引擎" and not _engine_expected_now():
                continue
            adds("critical", f"{name}进程", f"pid={pid} 确认不存在")
        elif st is None:
            adds("warn", f"{name}进程", f"pid={pid} 无法判定(权限受限, 非故障)")
    # 数据流动（#4 看门狗，替换此前的内联 120s 规则）
    # [2026-09-21] 此处曾自己写死"盘中且 age>120 => warn"。同一条判据出现在两处就会漂移，
    # 故收敛到 flow_watchdog 单一事实源；顺带升级了能力: 能区分**主循环死锁 / 行情源冻住 /
    # 进程不在**（三种运维动作不同），并把 tick 计数器未推进作为死锁硬证据带出来。
    try:
        import flow_watchdog as _fw
        _f = _fw.gather()
        if _f.get("level") == "CRITICAL":
            adds("critical", "数据流动(#4)", str(_f.get("reason")))
        elif _f.get("level") == "WARN":
            adds("warn", "数据流动(#4)", str(_f.get("reason")))
    except Exception as e:  # noqa: BLE001
        adds("warn", "数据流动(#4)", f"看门狗不可用: {type(e).__name__}: {str(e)[:110]}")
    # 数据滞后
    rep = os.path.join(DATA_DIR, "check_data_report.json")
    try:
        with open(rep, encoding="utf-8") as f:
            j = json.load(f)
        stale = [h["table"] for h in j.get("health", []) if not h.get("ok")]
        if stale:
            adds("warn", "数据滞后", "滞后表: " + ", ".join(stale))
    except Exception:
        pass
    # 回撤 / 单日亏损 (从回执现算)
    rk = read_riskops()
    if rk.get("ok"):
        m = rk["metrics"]
        dd = m.get("max_dd") or 0
        if dd <= -8:
            adds("critical", "历史回撤", f"最大回撤 {dd:.2f}% ≤ -8%")
        elif dd <= -5:
            adds("warn", "历史回撤", f"最大回撤 {dd:.2f}% ≤ -5%")
        if (m.get("daily_pnl_pct") or 0) <= -2:
            adds("critical", "单日亏损", f"今日 {m['daily_pnl_pct']:.2f}% ≤ -2%")
    out["critical"] = sum(1 for a in out["alerts"] if a["level"] == "critical")
    out["warn"] = sum(1 for a in out["alerts"] if a["level"] == "warn")
    return out


def read_factor_lab(days: int = 300):
    """因子实验室: 3 因子 IC 时序 (data/ic/ic_curve_*.csv) + 最新截面五分位.

    五分位来自 v_factor_scores_daily 最新交易日各因子得分分箱(截面分布,
    非未来收益); IC 时序为真实历史(2013~, ic_h1/3/5/10/20).
    """
    import glob as _g
    import math
    fames = {"vol": "波动率(反转义)", "mom_20": "动量(反转义)", "reversal": "反转"}
    factors = []
    for f in sorted(_g.glob(os.path.join(DATA_DIR, "ic", "ic_curve_*.csv"))):
        base = os.path.basename(f)
        key = base.replace("ic_curve_", "").replace("_k20.csv", "")
        try:
            import pandas as _pd
            df = _pd.read_csv(f)
        except Exception:
            continue
        df = df.tail(max(60, int(days)))
        rec = {"key": key, "zh": fames.get(key, key), "series": [], "stats": {}}
        for r in df.itertuples():
            d = str(r.day)
            d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
            rec["series"].append({"d": d, "h1": _f(r.ic_h1), "h3": _f(r.ic_h3),
                                  "h5": _f(r.ic_h5), "h10": _f(r.ic_h10), "h20": _f(r.ic_h20)})
        for h in ("h1", "h3", "h5", "h10", "h20"):
            col = df[f"ic_{h}"].dropna()
            if len(col):
                rec["stats"][h] = {"mean": float(col.mean()), "std": float(col.std()),
                                   "win": float((col > 0).mean() * 100),
                                   "last": float(col.iloc[-1])}
        factors.append(rec)
    # 五分位 (最新日截面)
    q = []
    fp = os.path.join(DATA_DIR, "h5i", "views", "v_factor_scores_daily.parquet")
    try:
        import pandas as _pd
        df = _pd.read_parquet(fp)
        mx = df["date"].max()
        df = df[df["date"] == mx].copy()
        for col, zh in (("f_signal", "综合信号"), ("f_trend", "趋势"), ("f_govern", "治理"),
                        ("f_liquidity", "流动性"), ("f_vol", "波动(反转义)"), ("f_mom_rev", "动量(反转义)")):
            if col not in df.columns:
                continue
            s = _pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) < 10:
                continue
            try:
                qq = _pd.qcut(s, 5, labels=[0, 1, 2, 3, 4], duplicates="drop")
            except Exception:
                continue
            bins = []
            for qi in range(5):
                grp = s[qq == qi]
                if len(grp):
                    bins.append({"q": qi, "n": int(len(grp)),
                                 "lo": float(grp.min()), "hi": float(grp.max()),
                                 "mean": float(grp.mean())})
            if bins:
                q.append({"factor": col, "zh": zh, "date": str(mx), "bins": bins})
    except Exception:
        pass
    return {"ok": True, "factors": factors, "quantiles": q}


def _send_push(channel: str, message: str) -> dict:
    """外部推送通道: 钉钉机器人 / SMTP 邮件 (均从环境变量读配置, 未配置返回错误)."""
    import os as _os
    message = (message or "")[:600]
    if channel == "dingtalk":
        url = _os.environ.get("DINGTALK_WEBHOOK", "").strip()
        if not url:
            return {"ok": False, "error": "未配置 DINGTALK_WEBHOOK 环境变量"}
        try:
            import requests
            r = requests.post(url, json={"msgtype": "text",
                                         "text": {"content": message}}, timeout=8)
            return {"ok": r.ok, "status": r.status_code,
                    "body": (r.text or "")[:200]}
        except Exception as e:
            return {"ok": False, "error": f"钉钉推送失败: {e}"}
    if channel == "email":
        host = _os.environ.get("SMTP_HOST", "").strip()
        user = _os.environ.get("SMTP_USER", "").strip()
        pwd = _os.environ.get("SMTP_PASS", "").strip()
        to = _os.environ.get("SMTP_TO", "").strip()
        if not (host and to):
            return {"ok": False, "error": "未配置 SMTP_HOST/SMTP_TO (可选 USER/PASS)"}
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(message, "plain", "utf-8")
            msg["Subject"] = "[A股轮动] 仪表台告警"
            msg["From"] = user or host
            msg["To"] = to
            with smtplib.SMTP(host, int(_os.environ.get("SMTP_PORT", "25") or 25), timeout=10) as s:
                if user:
                    s.login(user, pwd)
                s.sendmail(user or host, [to], msg.as_string())
            return {"ok": True, "status": "sent"}
        except Exception as e:
            return {"ok": False, "error": f"邮件发送失败: {e}"}
    return {"ok": False, "error": f"未知通道: {channel}"}


_SCAN_COMBOS = [
    {"name": "基线", "desc": "现行门控参数", "ov": {}},
    {"name": "全暴露", "desc": "caution/risk 暴露 ×1.0", "ov": {"exp_caution": 1.0, "exp_risk": 1.0}},
    {"name": "保守", "desc": "caution×0.85 / risk×0.60", "ov": {"exp_caution": 0.85, "exp_risk": 0.60}},
    {"name": "严风控", "desc": "risk 暴露 ×0.50", "ov": {"exp_risk": 0.50}},
    {"name": "快调仓", "desc": "risk 档 2 日即调", "ov": {"int_risk_step": 2}},
    {"name": "漂移敏感", "desc": "因子 IC 漂移阈值 0.10", "ov": {"factor_ic_drift": 0.10}},
    {"name": "常态高阈值", "desc": "normal 判定 IC>0.010", "ov": {"ic_normal_mean": 0.010}},
]


def read_bt_scan(force: bool = False) -> dict:
    """门控参数扫描 (轻量: 单次重放 ~0.1s, 全部 ~1s). 结果缓存 data/backtest_scan.json."""
    cache = os.path.join(DATA_DIR, "backtest_scan.json")
    if not force and os.path.exists(cache):
        try:
            age = datetime.now().timestamp() - os.path.getmtime(cache)
            if age < 86400 * 3:
                with open(cache, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    try:
        import backtest_with_gate as bg
    except Exception as e:
        return {"ok": False, "error": f"backtest_with_gate 导入失败: {e}"}
    try:
        ds = bg.load_dataset()
        if not ds.get("ok"):
            return {"ok": False, "error": ds.get("error")}
        rows = []
        for c in _SCAN_COMBOS:
            try:
                r = bg.simulate(ds, c["ov"])
                g = r.get("gated") or {}
                b = r.get("baseline") or {}
                gs = r.get("gate_stats") or {}
                # 注意单位: backtest_with_gate -> strategy_validation.curve_metrics 的
                # max_drawdown 为"比例"(0..1), 故此处 x100 转百分点; 与 perf/BacktestRunner
                # 的百分数字段不同源, 不可套用统一换算。
                rows.append({
                    "name": c["name"], "desc": c["desc"], "ok": r.get("ok"),
                    "gated": {"sharpe": g.get("sharpe"), "cagr": g.get("cagr"),
                              "mdd_pct": (g.get("max_drawdown") or 0) * 100,
                              "pos_day": g.get("pos_day_share"),
                              "top5": g.get("top5_share"),
                              "recovery": g.get("recovery")},
                    "baseline": {"sharpe": b.get("sharpe"), "cagr": b.get("cagr"),
                                 "mdd_pct": (b.get("max_drawdown") or 0) * 100},
                    "gate_stats": {"regime": gs.get("regime_days") or gs.get("regime") or {},
                                   "n_plan": gs.get("n_plan")},
                })
            except Exception as e:
                rows.append({"name": c["name"], "desc": c["desc"], "ok": False,
                             "error": str(e)[:160]})
        out = {"ok": True, "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "range": {"start": str(ds.get("start")), "end": str(ds.get("end"))},
               "rows": rows}
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return out
    except Exception as e:
        return {"ok": False, "error": f"扫描失败: {e}"}


def read_health():
    """读取盘前健康检查结果: data/health/premarket.json."""
    p = os.path.join(DATA_DIR, "health", "premarket.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "premarket.json 不存在, 请先运行 premarket_healthcheck.py"}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        return j
    except Exception as e:
        return {"ok": False, "error": str(e)}


#: 漏洞清单单一事实源 (committed; 由 scripts/render_vulnerability_register.py 渲染成 md)
ACCEPTANCE_FP = os.path.join(_BASE, "ops", "acceptance_status.json")


def read_acceptance():
    """读取 ops/acceptance_status.json, 汇总阻塞项状态。

    为什么面板要读它: 上线前复验的 P0 阻塞项(P0-1/E-1/P0-2/P0-3)此前只存在于文档里,
    盘面上看不到。放进 /api/health 的 checks 后, 打开面板即可见当前阻塞与状态。
    """
    if not os.path.exists(ACCEPTANCE_FP):
        return {"ok": False, "error": "acceptance_status.json 不存在"}
    try:
        with open(ACCEPTANCE_FP, encoding="utf-8") as f:
            j = json.load(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    items = j.get("items", []) or []
    by_status: dict = {}
    for it in items:
        by_status[it.get("status", "?")] = by_status.get(it.get("status", "?"), 0) + 1
    p0_open = [it for it in items
               if it.get("level") == "P0" and it.get("status") in ("open", "partial", "decision")]
    return {"ok": True, "generated_at": j.get("generated_at", ""), "total": len(items),
            "by_status": by_status, "p0_open": [{"id": it["id"], "title": it["title"],
                                                 "status": it["status"]} for it in p0_open]}


def read_health_merged():
    """合并运行期健康 + 盘前健康检查 + 阻塞项, 供 /api/health 单一出口使用。

    背景(2026-09-19 修): /api/health 曾有两份实现 —— 内联分支(为 dashboard_keepalive
    提供 ok/pid)遮蔽了 read_health()(读 premarket.json)。二者载荷形状不同:
      - 前端 renderHealth 期望 {level, summary, generated_at, checks[]};
      - 内联分支返回 {ok, service, pid, ts, deps{duckdb, arcticdb}}。
    结果健康卡片渲染成"OK + 空表", 退役 DuckDB 的打开失败(deps.duckdb_error)
    在 UI 上完全不显示。此处合并为同一载荷, 两个消费方都满足。
    """
    checks: list = []
    # --- 运行期检查: bar store ---
    mode = "h5i" if _h5i_mode() else "duck"
    if mode == "h5i":
        try:
            ok_dir = os.path.isdir(_H5I_PATH)
            latest = _h5i_max_bar_date() if ok_dir else None
            if not ok_dir:
                checks.append({"name": "bar_store(h5i)", "status": "FAIL", "ms": 0,
                               "detail": f"路径不存在: {_H5I_PATH}"})
            elif latest:
                # 与当前日期比较: 超过 5 个自然日视为陈旧(周末+节假日留余量)
                from datetime import date as _d, datetime as _dt
                try:
                    gap = (_d.today() - _dt.strptime(str(latest)[:10], "%Y-%m-%d").date()).days
                except Exception:  # noqa: BLE001
                    gap = None
                st = "OK" if (gap is not None and gap <= 5) else "WARN"
                checks.append({"name": "bar_store(h5i)", "status": st, "ms": 0,
                               "detail": f"最新 bar 日={latest}" +
                                         (f", 距今 {gap} 天" if gap is not None else "")})
            else:
                checks.append({"name": "bar_store(h5i)", "status": "WARN", "ms": 0,
                               "detail": "H5iBarStore 可用但未取到最新 bar 日"})
        except Exception as e:  # noqa: BLE001
            checks.append({"name": "bar_store(h5i)", "status": "FAIL", "ms": 0,
                           "detail": str(e)[:160]})
    else:
        try:
            import duckdb as _d  # noqa: F401
            con = _d.connect(DUCKDB_PATH, read_only=True)
            con.execute("SELECT 1").fetchone()
            con.close()
            checks.append({"name": "bar_store(duckdb)", "status": "OK", "ms": 0,
                           "detail": DUCKDB_PATH})
        except Exception as e:  # noqa: BLE001
            checks.append({"name": "bar_store(duckdb)", "status": "FAIL", "ms": 0,
                           "detail": str(e)[:160]})
    # --- 遗留 DuckDB: 已退役, 不作为失败项(仅提示) ---
    try:
        import duckdb as _d2
        con2 = _d2.connect(DUCKDB_PATH, read_only=True)
        con2.execute("SELECT 1").fetchone()
        con2.close()
        checks.append({"name": "legacy_duckdb(已退役)", "status": "OK", "ms": 0,
                       "detail": "仍可打开; 主源已切 h5i"})
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "legacy_duckdb(已退役)", "status": "WARN", "ms": 0,
                       "detail": "不可打开(符合预期, 主源=h5i): " + str(e)[:110]})
    # --- 阻塞项 ---
    acc = read_acceptance()
    if acc.get("ok"):
        p0 = acc.get("p0_open", [])
        detail = f"共 {acc.get('total')} 项; P0 未闭环 {len(p0)} 项"
        if p0:
            detail += ": " + "; ".join(f"{x['id']}[{x['status']}] {x['title']}" for x in p0)
        checks.append({"name": "acceptance(P0 阻塞项)", "status": "WARN" if p0 else "OK",
                       "ms": 0, "detail": detail[:400]})
    else:
        checks.append({"name": "acceptance(P0 阻塞项)", "status": "WARN", "ms": 0,
                       "detail": str(acc.get("error", ""))[:160]})
    # --- DRL 降级状态（用户决策 D: 必须"每日复盘告警可见"）---
    # 数据源是 drl_degrade 的**轻量**读取器（指针 + 账本），不需要 torch/h5i_db，
    # 故在看板（可能跑在无 h5i_db 的解释器上）也能取值。
    try:
        import drl_degrade as _dd
        _lv = _dd.current_level()
        _lvnum = int(_lv.get("level") or 0)
        _st = "OK" if _lvnum == 0 else ("CRITICAL" if _lvnum >= 3 else "WARN")
        _pr = _dd.probe_runtime()
        checks.append({
            "name": "DRL 降级状态",
            "status": _st, "ms": 0,
            "detail": (f"L{_lvnum} {_lv.get('level_name')}"
                       f"{'（当日未生成新信号）' if _lv.get('blocked_plan') else ''}"
                       f"; 环境齐备={_pr.get('ok')}"
                       f"{'; 缺 ' + '/'.join(_pr.get('missing') or []) if not _pr.get('ok') else ''}"
                       f"; 账本事件={_dd.event_count()}"),
        })
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "DRL 降级状态", "status": "WARN", "ms": 0,
                       "detail": "读取失败: " + str(e)[:140]})
    # --- 运行态降级状态机（路线图 #2；读**已发布快照**, 不跑引擎探针）---
    # 为什么读快照而不是现算: 引擎探针实测 1.71s(查 4 只参考股全历史, 见 engine_bars_sync),
    # 而本面板前端每 3 秒轮询一次 /api/health —— 探针放进请求路径会直接拖垮面板。
    # 快照由守护进程按既有 5 分钟看护节奏发布(dashboard.py 无需 h5i_db / 无需 STOCKDB_ROOT)。
    try:
        import health_state as _hs
        _h = _hs.read_published()
        if not _h.get("available"):
            checks.append({"name": "运行态降级状态机", "status": "WARN", "ms": 0,
                           "detail": "无快照: " + str(_h.get("error", ""))[:150]})
        else:
            _st = {"NORMAL": "OK", "DEGRADED": "WARN",
                   "HALTED": "CRITICAL"}.get(_h.get("state"), "WARN")
            _age = _h.get("age_s")
            _agetxt = (f"{_age/60:.1f} 分钟前" if isinstance(_age, (int, float)) else "年龄未知")
            _rs = _h.get("reasons") or []
            # 必须带上"观测时段": 夜间引擎已停, tick 延迟的滚动窗口**冻结在最后一次 tick**,
            # 如 23:30 报 p50=11.9s 其实描述的是 15:02 那段盘。不写清就会被读成"现在"。
            _obs_ts = (_h.get("observed") or {}).get("live_data_ts")
            checks.append({
                "name": "运行态降级状态机", "status": _st, "ms": 0,
                "detail": (f"{_h.get('state')}; 快照 {_h.get('ts')} ({_agetxt})"
                           + (f"; 观测时段 {_obs_ts}" if _obs_ts else "")
                           + ("【快照陈旧】" if _h.get("stale") else "")
                           + ("; " + "; ".join(_rs) if _rs else "; 各项正常"))[:400],
            })
    except Exception as e:  # noqa: BLE001
        checks.append({"name": "运行态降级状态机", "status": "WARN", "ms": 0,
                       "detail": "读取失败: " + str(e)[:140]})
    # --- 盘前健康检查(premarket.json) ---
    pm = read_health()
    pm_checks = pm.get("checks") if isinstance(pm, dict) else None
    if pm_checks:
        # 说明(不改状态, 只补信息): premarket.json 由 premarket_healthcheck.py 产出,
        # 其判定仍以 DuckDB 为主源。DuckDB 退役后, 一批检查因"legacy_stockdb.duckdb
        # 不存在"连锁 FAIL —— 属**预期结果而非实际故障**, 但状态原样保留(不粉饰),
        # 仅追加来源说明, 以免被误读为系统损坏。根治需更新 premarket_healthcheck.py。
        _RETIRED = ("legacy_stockdb.duckdb", "DuckDB 缺失", "db 不存在")
        for c in pm_checks:
            if isinstance(c, dict):
                det = str(c.get("detail", ""))
                if ("duckdb" in str(c.get("name", "")).lower()
                        or any(sig in det for sig in _RETIRED)):
                    det += "（注: legacy DuckDB 已退役、主源=h5i, 此项待 premarket_healthcheck 适配）"
                checks.append({"name": "premarket:" + str(c.get("name", "?")),
                               "status": str(c.get("status", "WARN")),
                               "ms": c.get("ms", 0), "detail": det})
    else:
        checks.append({"name": "premarket", "status": "WARN", "ms": 0,
                       "detail": str(pm.get("error", "无盘前健康检查结果"))[:160]})

    worst = "OK"
    if any(c["status"] == "FAIL" for c in checks):
        worst = "FAIL"
    elif any(c["status"] in ("CRITICAL", "WARN") for c in checks):
        # 2026-09-21 修: 此前只认 FAIL/WARN, 于是 status=CRITICAL 的项(DRL L3、
        # 以及新增的降级状态机 HALTED)在 summary/level 里**完全隐身** ——
        # 检查项写着 CRITICAL, 摘要却说 OK。CRITICAL 归入 DEGRADED(本字段只有三档词汇)。
        worst = "DEGRADED"
    level = pm.get("level") if isinstance(pm, dict) and pm.get("level") else worst
    return {
        "ok": True,                      # dashboard_keepalive.py 以 HTTP 200 + ok 判定可用
        "service": "dashboard",
        "pid": os.getpid(),
        "ts": _now(),
        "level": level,
        "summary": (f"运行期 {len(checks)} 项; 最差={worst}; "
                    f"bar_store={mode}; " + str(pm.get("summary", "") if isinstance(pm, dict) else ""))[:300],
        "generated_at": _now(),
        "checks": checks,
        "deps": {"bar_store": mode, "h5i_path": _H5I_PATH, "duckdb_path": DUCKDB_PATH},
    }


def read_views():
    """读取因子物化视图构建元数据 + 各视图摘要: data/views/build_meta.json."""
    p = os.path.join(DATA_DIR, "views", "build_meta.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "build_meta.json 不存在, 请先运行 build_factor_views.py"}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        return j
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_target_plan(day: str | None = None):
    """读取 DRL 目标计划: data/drl/<YYYYMMDD>/target_plan.json.
    day: 形如 'YYYY-MM-DD', 默认昨日 (与盘中引擎默认一致)."""
    from datetime import date, timedelta
    if not day:
        d = date.today() - timedelta(days=1)
        day = d.strftime("%Y-%m-%d")
    day_dir = day.replace("-", "")
    p = os.path.join(DATA_DIR, "drl", day_dir, "target_plan.json")
    if not os.path.exists(p):
        # 尝试当日
        d = date.today()
        day2 = d.strftime("%Y-%m-%d")
        p = os.path.join(DATA_DIR, "drl", day2.replace("-", ""), "target_plan.json")
    if not os.path.exists(p):
        return {"ok": False, "error": "target_plan.json 不存在",
                "hint": "需先运行 drl_train.py 生成计划"}
    try:
        with open(p, encoding="utf-8") as f:
            j = json.load(f)
        return {"ok": True, "path": p, "plan": j}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# 8 个新面板的 read 函数
# ============================================================

# DuckDB 表名 → 中文名 + 数据时间范围描述 (用于数据板块展示)
_DB_TABLE_ZH = {
    "daily_bars":            {"zh": "日K线 (主行情)",         "group": "行情",     "desc": "全 A 日线 OHLCV + 涨跌幅 + 换手率"},
    "minute_bars":           {"zh": "分钟K线",                 "group": "行情",     "desc": "1/5/15 分钟 K 线"},
    "bars":                  {"zh": "K线明细",                 "group": "行情",     "desc": "通用 K 线明细表"},
    "valuation":             {"zh": "估值明细",               "group": "估值",     "desc": "PE/PB/PS/总市值历史序列"},
    "valuation_snapshot":    {"zh": "估值快照",               "group": "估值",     "desc": "当日全 A 估值快照"},
    "adj_factors":           {"zh": "复权因子",               "group": "行情",     "desc": "前/后复权因子"},
    "orderbook_snapshot":    {"zh": "委托/盘口快照",          "group": "行情",     "desc": "十档盘口快照"},
    "northbound_money":      {"zh": "北向资金",               "group": "资金",     "desc": "沪深港通北向每日净流入"},
    "margin_daily":          {"zh": "融资融券",               "group": "资金",     "desc": "两融余额每日明细"},
    "money_flow":            {"zh": "资金流向 (标准)",        "group": "资金",     "desc": "主力/中单/小单资金流向"},
    "money_flow_estimate":   {"zh": "资金流向 (估算)",        "group": "资金",     "desc": "内外盘 + 净主动成交估算"},
    "dzjy_daily":            {"zh": "大宗交易",               "group": "资金",     "desc": "每日大宗交易明细"},
    "block_trade":           {"zh": "大笔成交",               "group": "资金",     "desc": "Block Trade 明细"},
    "lhb":                   {"zh": "龙虎榜",                 "group": "异动",     "desc": "龙虎榜每日上榜"},
    "lhb_detail":            {"zh": "龙虎榜明细",             "group": "异动",     "desc": "龙虎榜买卖席位明细"},
    "events":                {"zh": "事件日历",               "group": "事件",     "desc": "复牌/分红/股东大会等事件"},
    "stock_news":            {"zh": "个股新闻",               "group": "资讯",     "desc": "个股新闻"},
    "stock_notices":         {"zh": "个股公告",               "group": "资讯",     "desc": "全 A 公告 (标题/正文)"},
    "announcements":         {"zh": "公告标题",               "group": "资讯",     "desc": "公告标题集合"},
    "news":                  {"zh": "财经新闻",               "group": "资讯",     "desc": "财经新闻聚合"},
    "news_cctv":             {"zh": "央视新闻",               "group": "资讯",     "desc": "央视财经新闻"},
    "earnings_forecasts":    {"zh": "业绩预告",               "group": "财报",     "desc": "全 A 业绩预告"},
    "financials":            {"zh": "财务报表",               "group": "财报",     "desc": "三大报表 + 财务指标"},
    "share_structure":       {"zh": "股本结构",               "group": "财报",     "desc": "总股本/流通股本/限售"},
    "shareholder_changes":   {"zh": "股东变动",               "group": "财报",     "desc": "前十大股东变动"},
    "corporate_actions":     {"zh": "公司行动",               "group": "财报",     "desc": "分红/送转/回购等"},
    "locked_shares":         {"zh": "限售股",                 "group": "财报",     "desc": "限售股明细"},
    "restricted_releases":   {"zh": "解禁",                   "group": "财报",     "desc": "解禁排期"},
    "dividends":             {"zh": "分红送配",               "group": "财报",     "desc": "历年分红送配"},
    "repurchases":           {"zh": "回购",                   "group": "财报",     "desc": "公司回购明细"},
    "economic_events":       {"zh": "宏观事件",               "group": "宏观",     "desc": "宏观日历 (CPI/PMI/利率决议等)"},
    "symbols":               {"zh": "证券主数据",             "group": "主数据",   "desc": "全 A 代码/名称/市场/上市日"},
    "pipeline_status":       {"zh": "管道状态",               "group": "元数据",   "desc": "管道作业状态"},
    "sync_status":           {"zh": "同步状态",               "group": "元数据",   "desc": "各表同步历史"},
}


def validate_dashboard_table_name(name: str) -> str | None:
    """只接受已登记的精确表名，拒绝大小写/路径/SQL 片段绕过。"""
    if not isinstance(name, str) or name not in _DB_TABLE_ZH:
        return None
    return name


def _zh_for(t: str) -> dict:
    return _DB_TABLE_ZH.get(t, {"zh": t, "group": "其他", "desc": ""})


# ---------- 回测历史 ----------
def read_backtest_history():
    """扫描 data/backtest/* 与 data/backtest_latest.json, 返回回测记录列表."""
    rows = []
    p_latest = os.path.join(DATA_DIR, "backtest_latest.json")
    if os.path.exists(p_latest):
        try:
            j = json.load(open(p_latest, encoding="utf-8"))
            j["_source"] = "backtest_latest.json"
            rows.append(j)
        except Exception:
            pass
    bt_dir = os.path.join(DATA_DIR, "backtest")
    if os.path.exists(bt_dir):
        for sub in sorted(os.listdir(bt_dir), reverse=True):
            full = os.path.join(bt_dir, sub)
            if not os.path.isdir(full):
                continue
            for fname in ("summary.json", "result.json"):
                p = os.path.join(full, fname)
                if os.path.exists(p):
                    try:
                        j = json.load(open(p, encoding="utf-8"))
                        j["_source"] = f"backtest/{sub}/{fname}"
                        j["_day"] = sub
                        rows.append(j)
                    except Exception:
                        pass
    # 也读 vnpy 输出
    vnpy_dir = os.path.join(DATA_DIR, "vnpy_backtest")
    if os.path.exists(vnpy_dir):
        for sub in sorted(os.listdir(vnpy_dir), reverse=True)[:20]:
            full = os.path.join(vnpy_dir, sub)
            if not os.path.isdir(full):
                continue
            sp = os.path.join(full, "summary.json")
            if os.path.exists(sp):
                try:
                    j = json.load(open(sp, encoding="utf-8"))
                    j["_source"] = f"vnpy_backtest/{sub}/summary.json"
                    j["_day"] = sub
                    rows.append(j)
                except Exception:
                    pass
    # 也读 performance_report
    perf_p = os.path.join(DATA_DIR, "performance_report.json")
    if os.path.exists(perf_p):
        try:
            perf = json.load(open(perf_p, encoding="utf-8"))
            m = perf.get("metrics") or {}
            bench = perf.get("benchmark") or {}
            rows.append({
                "_source": "performance_report.json",
                "_day": perf.get("period", {}).get("end", ""),
                "tag": "performance_report",
                "period": perf.get("period"),
                "metrics": m,
                "benchmark": bench,
            })
        except Exception:
            pass
    return {"ok": True, "rows": rows, "count": len(rows)}


# ---------- 监控中心 ----------
def read_monitor():
    """聚合: 盘前健康 + 退化 + SPC + DRL plan ready + reward config."""
    out = {"ok": True, "components": {}}
    # 1) 盘前健康
    hp = os.path.join(DATA_DIR, "health", "premarket.json")
    if os.path.exists(hp):
        try:
            out["components"]["health"] = json.load(open(hp, encoding="utf-8"))
        except Exception as e:
            out["components"]["health"] = {"ok": False, "error": str(e)}
    else:
        out["components"]["health"] = {"ok": False, "error": "premarket.json 不存在"}
    # 2) 退化
    dp = os.path.join(DATA_DIR, "drl_degradation.json")
    if not os.path.exists(dp):
        dp = os.path.join(DATA_DIR, "degradation_latest.json")
    if os.path.exists(dp):
        try:
            out["components"]["degradation"] = json.load(open(dp, encoding="utf-8"))
        except Exception as e:
            out["components"]["degradation"] = {"ok": False, "error": str(e)}
    else:
        out["components"]["degradation"] = {"ok": False, "error": "退化报告不存在"}
    # 3) reward_config
    rc = os.path.join(DATA_DIR, "reward_config.json")
    if os.path.exists(rc):
        try:
            out["components"]["reward_config"] = json.load(open(rc, encoding="utf-8"))
        except Exception as e:
            out["components"]["reward_config"] = {"ok": False, "error": str(e)}
    else:
        out["components"]["reward_config"] = {"ok": False, "error": "reward_config.json 不存在 (默认 vnpy=0.6/ic=0.4)"}
    # 4) DRL plan ready
    from datetime import date, timedelta
    d = date.today()
    plan_paths = [
        os.path.join(DATA_DIR, "drl", d.strftime("%Y%m%d"), "target_plan.json"),
        os.path.join(DATA_DIR, "drl", (d - timedelta(days=1)).strftime("%Y%m%d"), "target_plan.json"),
    ]
    plan_found = None
    for pp in plan_paths:
        if os.path.exists(pp):
            try:
                plan_found = {"path": pp, "data": json.load(open(pp, encoding="utf-8"))}
                break
            except Exception:
                pass
    if plan_found:
        out["components"]["drl_plan"] = {"ok": True, **plan_found}
    else:
        out["components"]["drl_plan"] = {"ok": False, "error": "DRL plan 未生成"}
    # 5) daemon 状态
    ds = os.path.join(_BASE, "logs", "daemon_state.json")
    if os.path.exists(ds):
        try:
            out["components"]["daemon"] = json.load(open(ds, encoding="utf-8"))
        except Exception as e:
            out["components"]["daemon"] = {"ok": False, "error": str(e)}
    else:
        out["components"]["daemon"] = {"ok": False, "error": "daemon_state.json 不存在"}
    return out


# ---------- 市场环境 ----------
def read_regime():
    """综合 v_market_breadth + market.json + v_factor_ic_latest."""
    out = {"ok": True, "components": {}}
    # 1) 市场宽度 (优先物化 parquet, 避开盘后维护窗口的 DuckDB 写锁)
    _BREADTH_COLS = [
        "date", "total", "n_up", "n_down", "n_limit_up", "n_limit_dn",
        "total_amount", "avg_change_pct", "breadth_ratio",
    ]
    rows = _read_view_parquet_named(
        "v_market_breadth.parquet", "ORDER BY date DESC LIMIT 30", _BREADTH_COLS)
    src = "parquet"
    if rows is None:
        src = "duckdb"
        try:
            rows = _duckdb_query_named("""
                SELECT date, total, n_up, n_down, n_limit_up, n_limit_dn,
                       total_amount, avg_change_pct, breadth_ratio
                FROM v_market_breadth
                ORDER BY date DESC
                LIMIT 30
            """, cols=_BREADTH_COLS)
        except Exception as e:
            rows = None
            out["components"]["breadth_error"] = str(e)
    if rows:
        out["components"]["breadth"] = [
            {"date": str(r["date"]), "total": r["total"], "n_up": r["n_up"] or 0,
             "n_down": r["n_down"] or 0, "n_limit_up": r["n_limit_up"] or 0,
             "n_limit_dn": r["n_limit_dn"] or 0, "total_amount": r["total_amount"],
             "avg_chg": r["avg_change_pct"], "breadth_ratio": r["breadth_ratio"]}
            for r in rows
        ]
        out["components"]["breadth_source"] = src
    # 2) 市场情绪 (market.json)
    market_dir = os.path.join(DATA_DIR, "market")
    found_market = None
    if os.path.exists(market_dir):
        days = sorted(os.listdir(market_dir), reverse=True)
        for d in days[:3]:
            p = os.path.join(market_dir, d, "market.json")
            if os.path.exists(p):
                try:
                    found_market = {"path": p, "data": json.load(open(p, encoding="utf-8"))}
                    break
                except Exception:
                    pass
    if found_market:
        out["components"]["market"] = found_market
    else:
        out["components"]["market"] = {"ok": False, "error": "market.json 不存在"}
    # 3) 因子 IC (优先物化 parquet, 避开盘后维护窗口的 DuckDB 写锁)
    _IC_COLS = [
        "factor", "ic_value", "mean_20", "std_20", "icir_20", "win_rate_20", "n_days",
    ]
    ic_rows = _read_view_parquet_named(
        "v_factor_ic_latest.parquet", "ORDER BY factor", _IC_COLS)
    ic_src = "parquet" if ic_rows is not None else "duckdb"
    if ic_rows is None:
        try:
            ic_rows = _duckdb_query_named(
                "SELECT factor, ic_value, mean_20, std_20, icir_20, win_rate_20, n_days "
                "FROM v_factor_ic_latest ORDER BY factor",
                cols=_IC_COLS,
            )
        except Exception as e:
            ic_rows = None
            out["components"]["factor_ic_error"] = str(e)
    if ic_rows:
        out["components"]["factor_ic"] = [
            {"factor": r["factor"], "ic_value": r["ic_value"], "mean_20": r["mean_20"],
             "std_20": r["std_20"], "icir_20": r["icir_20"],
             "win_rate_20": r["win_rate_20"], "n_days": r["n_days"]}
            for r in ic_rows
        ]
        out["components"]["factor_ic_source"] = ic_src
    # 4) 阶段判别 (基于 breadth)
    breadth = out["components"].get("breadth") or []
    if breadth:
        latest = breadth[0]
        ratio = latest.get("breadth_ratio") or 0.0
        n_lim_up = latest.get("n_limit_up") or 0
        n_lim_dn = latest.get("n_limit_dn") or 0
        if ratio > 0.5 and n_lim_up > 50:
            phase = "强势上行"
        elif ratio > 0.2:
            phase = "震荡偏多"
        elif ratio > -0.2:
            phase = "震荡整理"
        elif ratio > -0.5:
            phase = "震荡偏空"
        else:
            phase = "弱势下行"
        out["components"]["phase"] = {
            "phase": phase,
            "breadth_ratio": ratio,
            "n_limit_up": n_lim_up,
            "n_limit_dn": n_lim_dn,
            "based_on": latest.get("date"),
        }
    return out


# ---------- 异动监控 ----------
def read_abnormal(limit: int = 50):
    """当日异动: 涨幅 Top / 跌幅 Top / 成交额 Top / 涨停 / 跌停."""
    # [2026-09-05 迁移] BAR_STORE=h5i 时走 h5i 等价实现 (返回结构/数值与 duck 版一致)
    if _h5i_mode():
        return _read_abnormal_h5i(limit)
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": False, "error": "DuckDB 不存在"}
    out = {"ok": True}
    try:
        latest = _duckdb_query("SELECT MAX(date) FROM daily_bars")
        latest_date = (latest[0][0].isoformat() if latest and hasattr(latest[0][0], "isoformat")
                       else (str(latest[0][0])[:10] if latest and latest[0][0] else "")) if latest else ""
        out["latest_date"] = latest_date
        out["top_change"] = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars WHERE date = (SELECT MAX(date) FROM daily_bars)
            ORDER BY change_pct DESC NULLS LAST LIMIT ?
        """, [limit])
        out["top_drop"] = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars WHERE date = (SELECT MAX(date) FROM daily_bars)
            ORDER BY change_pct ASC NULLS LAST LIMIT ?
        """, [limit])
        out["top_amount"] = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars WHERE date = (SELECT MAX(date) FROM daily_bars)
            ORDER BY amount DESC NULLS LAST LIMIT ?
        """, [limit])
        out["limit_up"] = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars) AND change_pct >= 9.5
            ORDER BY change_pct DESC LIMIT ?
        """, [limit])
        out["limit_dn"] = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars) AND change_pct <= -9.5
            ORDER BY change_pct ASC LIMIT ?
        """, [limit])
        # 成交额异动 (vs 20日均量 放大量). 避免 INTERVAL 语法以兼容单线程 server
        out["volume_surge"] = _duckdb_query("""
            WITH latest AS (SELECT MAX(date) AS d FROM daily_bars),
            today AS (
                SELECT symbol, amount FROM daily_bars
                WHERE date = (SELECT d FROM latest)
            ),
            avg20 AS (
                SELECT symbol, AVG(amount) AS amt_avg
                FROM daily_bars b, latest l
                WHERE b.date >= l.d - 20
                  AND b.date < l.d
                GROUP BY symbol
            )
            SELECT t.symbol, t.amount, COALESCE(a.amt_avg, 0) AS amt_avg,
                   t.amount / NULLIF(a.amt_avg, 0) AS ratio
            FROM today t
            LEFT JOIN avg20 a ON a.symbol = t.symbol
            WHERE t.amount > 0 AND a.amt_avg > 0
            ORDER BY ratio DESC NULLS LAST
            LIMIT ?
        """, [limit])
    except Exception as e:
        return {"ok": False, "error": f"DuckDB 异常: {e}"}

    def canonize(rows, n=5):
        return [
            {"canon": str(r[0]) + ('.SH' if str(r[0]).startswith('6') else '.SZ'),
             "close": r[1], "change_pct": r[2], "amount": r[3], "turnover": r[4]}
            for r in rows
        ]

    out["top_change"] = canonize(out["top_change"])
    out["top_drop"] = canonize(out["top_drop"])
    out["top_amount"] = canonize(out["top_amount"])
    out["limit_up"] = canonize(out["limit_up"])
    out["limit_dn"] = canonize(out["limit_dn"])
    out["volume_surge"] = [
        {"canon": str(r[0]) + ('.SH' if str(r[0]).startswith('6') else '.SZ'),
         "amount": r[1], "amt_avg_20d": r[2], "ratio": r[3]}
        for r in out["volume_surge"]
    ]
    return out


def _read_abnormal_h5i(limit: int = 50) -> dict:
    """read_abnormal 的 h5i 等价实现 (BAR_STORE=h5i).

    对 h5i daily_bars (ts 时间列) 用等价的 h5i SQL 查询, 返回字段/结构/数值
    与 read_abnormal(duck 版) 一致; 日期为 'YYYY-MM-DD', 浮点允许 1e-6 级差异.
    """
    store = _h5i_store()
    if store is None:
        return {"ok": False, "error": "h5i 不存在"}
    out = {"ok": True}
    try:
        latest_date = _h5i_max_bar_date()
        if not latest_date:
            return {"ok": False, "error": "h5i daily_bars 无数据"}
        out["latest_date"] = latest_date
        L = int(limit)
        day = f"DATE '{latest_date}'"

        def q(sql):
            rows = _h5i_query(sql)
            return rows if rows is not None else []

        base_sel = ("SELECT symbol, close, change_pct, amount, turnover "
                    "FROM daily_bars WHERE CAST(ts AS DATE) = " + day + " ")
        out["top_change"] = q(base_sel + f"ORDER BY change_pct DESC NULLS LAST LIMIT {L}")
        out["top_drop"] = q(base_sel + f"ORDER BY change_pct ASC NULLS LAST LIMIT {L}")
        out["top_amount"] = q(base_sel + f"ORDER BY amount DESC NULLS LAST LIMIT {L}")
        out["limit_up"] = q(base_sel + f"AND change_pct >= 9.5 ORDER BY change_pct DESC LIMIT {L}")
        out["limit_dn"] = q(base_sel + f"AND change_pct <= -9.5 ORDER BY change_pct ASC LIMIT {L}")
        # 成交额异动 (vs 20日均量 放大量): 等价改写 (date -> CAST(ts AS DATE), 20日自然窗)
        out["volume_surge"] = q(f"""
            WITH today AS (
                SELECT symbol, amount FROM daily_bars
                WHERE CAST(ts AS DATE) = {day}
            ),
            avg20 AS (
                SELECT symbol, AVG(amount) AS amt_avg
                FROM daily_bars
                WHERE CAST(ts AS DATE) >= {day} - 20
                  AND CAST(ts AS DATE) < {day}
                GROUP BY symbol
            )
            SELECT t.symbol, t.amount, COALESCE(a.amt_avg, 0) AS amt_avg,
                   t.amount / NULLIF(a.amt_avg, 0) AS ratio
            FROM today t
            LEFT JOIN avg20 a ON a.symbol = t.symbol
            WHERE t.amount > 0 AND a.amt_avg > 0
            ORDER BY ratio DESC NULLS LAST
            LIMIT {L}
        """)
    except Exception as e:
        return {"ok": False, "error": f"h5i 异常: {e}"}

    def canonize(rows, n=5):
        return [
            {"canon": str(r[0]) + ('.SH' if str(r[0]).startswith('6') else '.SZ'),
             "close": r[1], "change_pct": r[2], "amount": r[3], "turnover": r[4]}
            for r in rows
        ]

    out["top_change"] = canonize(out["top_change"])
    out["top_drop"] = canonize(out["top_drop"])
    out["top_amount"] = canonize(out["top_amount"])
    out["limit_up"] = canonize(out["limit_up"])
    out["limit_dn"] = canonize(out["limit_dn"])
    out["volume_surge"] = [
        {"canon": str(r[0]) + ('.SH' if str(r[0]).startswith('6') else '.SZ'),
         "amount": r[1], "amt_avg_20d": r[2], "ratio": r[3]}
        for r in out["volume_surge"]
    ]
    return out


# ---------- 数据板块 ----------
def read_db_meta():
    """扫描 DuckDB 全部表 + ArcticDB 库, 返回表头/行数/最新/最早日期 + 中文名.
    带 60s TTL 缓存 (避免每15秒扫描 38 个表 + 38 次 DESCRIBE + COUNT)."""
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": True, "retired": True, "duckdb": None, "tables": [],
                "arctic": {}, "notice": "DuckDB 已退役并删除, 数据已全部迁移至 h5i/parquet; 表管理请用 h5i."}
    import time as _t
    cache_key = "db_meta"
    now = _t.time()
    cached = _AGG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < 60.0:
        return cached[1]
    out = _compute_db_meta()
    _AGG_CACHE[cache_key] = (now, out)
    return out


def _compute_db_meta():
    """read_db_meta 的实际实现 (内部, 供缓存层调用)."""
    out = {"ok": True,
           "duckdb": {"path": DUCKDB_PATH, "size_mb": -1,
                      "tables": [], "groups": {}},
           "arcticdb": {}}
    # DuckDB 文件大小
    try:
        out["duckdb"]["size_mb"] = round(os.path.getsize(DUCKDB_PATH) / (1024 * 1024), 2)
    except Exception:
        pass

    if os.path.exists(DUCKDB_PATH):
        try:
            table_rows = _duckdb_query("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'main'
                ORDER BY table_name
            """)
            for (tname,) in table_rows:
                try:
                    cnt_row = _duckdb_query(f'SELECT COUNT(*) FROM "{tname}"')
                    cnt = cnt_row[0][0] if cnt_row else -1
                except Exception:
                    cnt = -1
                cols = []
                latest_date = None
                earliest_date = None
                # 探测日期列
                date_col = None
                try:
                    desc_rows = _duckdb_query(f'DESCRIBE "{tname}"')
                    for col in desc_rows:
                        cols.append({"name": col[0], "type": col[1]})
                    for col in cols:
                        n = col["name"]
                        if n in ("date", "trade_date", "notice_date", "snapshot_date",
                                 "fetch_time", "event_time", "report_date"):
                            date_col = n
                            break
                    if date_col:
                        try:
                            rmax = _duckdb_query(f'SELECT MAX("{date_col}") FROM "{tname}"')
                            if rmax and rmax[0] and rmax[0][0]:
                                v = rmax[0][0]
                                latest_date = v.isoformat() if hasattr(v, "isoformat") else str(v)[:19]
                        except Exception:
                            pass
                        try:
                            rmin = _duckdb_query(f'SELECT MIN("{date_col}") FROM "{tname}"')
                            if rmin and rmin[0] and rmin[0][0]:
                                v = rmin[0][0]
                                earliest_date = v.isoformat() if hasattr(v, "isoformat") else str(v)[:19]
                        except Exception:
                            pass
                except Exception:
                    pass
                zh = _zh_for(tname)
                entry = {
                    "name": tname,
                    "name_zh": zh["zh"],
                    "group": zh["group"],
                    "desc": zh["desc"],
                    "rows": int(cnt) if cnt is not None else -1,
                    "columns": cols,
                    "col_count": len(cols),
                    "date_col": date_col,
                    "earliest_date": earliest_date,
                    "latest_date": latest_date,
                    "date_range": (f"{earliest_date} ~ {latest_date}" if earliest_date and latest_date else None),
                }
                out["duckdb"]["tables"].append(entry)
                out["duckdb"]["groups"].setdefault(zh["group"], 0)
                out["duckdb"]["groups"][zh["group"]] += 1
            # 按行数倒序
            out["duckdb"]["tables"].sort(key=lambda x: -(x.get("rows") or 0))
        except Exception as e:
            out["duckdb"]["error"] = str(e)
    else:
        out["duckdb"]["error"] = "DuckDB 文件不存在"
    # ArcticDB
    try:
        from arctic_store import get_store
        s = get_store()
        h = s.health_check()
        out["arcticdb"] = h
        # 每个库加中文名
        for lib, info in (h.get("libraries") or {}).items():
            zh = _DB_TABLE_ZH.get(lib, {"zh": lib})
            info["name_zh"] = zh["zh"]
    except Exception as e:
        out["arcticdb"] = {"error": str(e)}
    return out


def read_db_table(name: str, limit: int = 50):
    """读单表样本行."""
    name = validate_dashboard_table_name(name)
    if name is None:
        return {"ok": False, "error": "table not allowed", "code": "TABLE_NOT_ALLOWED"}
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": False, "retired": True,
                "error": "DuckDB 已退役删除(数据已迁 h5i); 该管理端点不再可用."}
    try:
        with _DUCKDB_LOCK:
            import duckdb
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            try:
                desc = con.execute(f'SELECT * FROM "{name}" LIMIT 0').description
                cols = [c[0] for c in desc]
                rows = con.execute(
                    f'SELECT * FROM "{name}" LIMIT ?', [int(limit)]
                ).fetchall()
                cnt = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        # 把 datetime 转 iso
        def _conv(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            if isinstance(v, (bytes,)):
                return v.decode("utf-8", errors="replace")
            return v
        rows_clean = [[_conv(v) for v in row] for row in rows]
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": name, "columns": cols, "rows": rows_clean, "total": int(cnt)}


# ---------- 数据同步: 增量 / 手动 ----------
# 同步模式 (单线程 HTTP 兼容): POST 后在请求线程内同步执行 update_db.update_all.
# 结果存在 _LAST_SYNC_RESULT, 前端 GET /api/db_sync/status 读这份.
_LAST_SYNC_RESULT: dict = {}
_SYNC_LOCK = _threading.Lock()


def _ensure_sync_dir():
    os.makedirs(os.path.join(DATA_DIR, "logs", "sync_jobs"), exist_ok=True)


def _now() -> str:
    """统一时间戳格式 (YYYY-MM-DD HH:MM:SS)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 立即定义以防 NameError (放在使用前)
_ = _now


def start_incremental_sync(only: list[str] | None = None,
                            day: str | None = None) -> dict:
    """同步模式启动增量同步 (通过 subprocess.run, 子进程独立持 DuckDB 写锁,
    避免 dashboard 同进程 read_only/write 冲突)."""
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": False, "retired": True, "started_at": _now(),
                "error": "DuckDB 已退役删除; 增量同步已由每日 run_daily(h5i 主写) 承担."}
    _ensure_sync_dir()
    from datetime import datetime as _dt
    d_str = day or date.today().strftime("%Y-%m-%d")
    # 日志 (用 print 即可, dashboard 不一定 import logging)
    print(f"[sync] incremental start day={d_str} only={only}")
    out = {"ok": False, "tables": {}, "started_at": _now()}
    code_lines = [
        "import sys, json, time",
        "sys.path.insert(0, r'" + _BASE.replace('\\', '/') + "')",
        "from datetime import datetime",
        "from update_db import update_all",
        "only = " + repr(only),
        "day = " + repr(d_str),
        "d_obj = datetime.strptime(day, '%Y-%m-%d').date()",
        "results = update_all(day=d_obj, only=only)",
        "print('__RESULT__' + json.dumps(results, ensure_ascii=False, default=str))",
    ]
    code = "\n".join(code_lines)
    # 注: 子进程代码只用 datetime/sys/json, 不引用外部 _now
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                                cwd=_BASE, capture_output=True, text=True,
                                timeout=300, encoding="utf-8")
        out["finished_at"] = _now()
        if proc.stderr:
            out["subprocess_stderr"] = proc.stderr[:500]
        # 解析结果
        text = proc.stdout
        marker = "__RESULT__"
        idx = text.rfind(marker)
        if idx >= 0:
            results = json.loads(text[idx + len(marker):])
            tables = {t: {"ok": v.get("ok"), "rows": v.get("rows"),
                           "error": v.get("error")}
                      for t, v in results.items()}
            out["tables"] = tables
            out["ok"] = all(v.get("ok") for v in results.values())
            ok_cnt = sum(1 for v in results.values() if v.get("ok"))
            fail_cnt = len(results) - ok_cnt
            out["logs"] = [
                {"ts": _now(), "level": "info",
                 "msg": f"增量同步 day={d_str} only={only or 'all'} -> OK={ok_cnt} FAIL={fail_cnt}"},
            ] + [
                {"ts": _now(), "level": "warn" if not v.get("ok") else "info",
                 "msg": f"{t}: ok={v.get('ok')} rows={v.get('rows')} " + (f"err={(v.get('error') or '')[:120]}" if not v.get('ok') else "")}
                for t, v in results.items()
            ]
        else:
            out["error"] = f"子进程无结果输出: stderr={proc.stderr[:300]}"
            out["logs"] = [{"ts": _now(), "level": "error",
                            "msg": f"stderr: {proc.stderr[:500]}"}]
    except subprocess.TimeoutExpired:
        out["error"] = "子进程超时 (300s)"
        out["logs"] = [{"ts": _now(), "level": "error", "msg": "子进程超时"}]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["logs"] = [{"ts": _now(), "level": "error", "msg": out["error"]}]
    return out


# 在 incremental 出错时也带上子进程的 stderr
def _safe_subprocess_run(code: str, cwd: str, timeout: int = 300):
    try:
        # `errors="replace"`: 已显式 utf-8, 但子进程若在中文 Windows 上以 GBK
        # 写 stderr, 严格模式会在读取线程里抛 —— `subprocess.run` **不抛**,
        # 而是静默返回 `stdout=None`, 调用方再 `(x.stdout or "")` 就把"解码失败"
        # 伪装成"子进程什么都没说"。replace 至少保住能解出来的部分。
        return subprocess.run([sys.executable, "-c", code],
                                cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired 没有 stdout/stderr 属性 (是 CalledProcessError 子类)
        class _FakeResult: pass
        r = _FakeResult()
        r.stdout = ""
        r.stderr = f"TimeoutExpired after {timeout}s"
        r.returncode = -1
        return r


def start_manual_sync_table(table: str, day: str | None = None) -> dict:
    """同步模式手动同步单表 (通过 subprocess.run, 子进程独占 DuckDB).

    支持 table 短名/别名 (e.g. "bars" -> "daily_bars" -> sync_daily_bars).
    子进程出错时返回 available_tables 列表, 方便前端排查.
    """
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": False, "retired": True, "started_at": _now(),
                "error": "DuckDB 已退役删除; 手动单表同步已不可用(数据由 h5i 主写链路维护)."}
    out = {"ok": False, "tables": {}, "started_at": _now()}
    # 别名映射: 兼容 dashboard 短名/历史调用习惯
    aliases = {
        "bars": "daily_bars",
        "kline": "daily_bars",
        "valuation": "valuation_snapshot",
        "adj": "adj_factors",
        "northbound": "northbound_money",
        "margin": "margin_daily",
        "dzjy": "dzjy_daily",
        "money_flow": "money_flow_estimate",
        "orderbook": "orderbook_snapshot",
        "block": "block_trade",
        "news": "stock_news",
        "notice": "stock_notices",
        "notices": "stock_notices",
        "announcement": "announcements",
        "forecast": "earnings_forecasts",
        "forecasts": "earnings_forecasts",
        "share_structure": "share_structure",
        "shareholder": "shareholder_changes",
        "corp_action": "corporate_actions",
        "corp_actions": "corporate_actions",
        "locked": "locked_shares",
        "restricted": "restricted_releases",
        "minute": "minute_bars",
    }
    canonical = aliases.get(table, table)
    out["canonical_table"] = canonical
    code_lines = [
        "import sys, json",
        "sys.path.insert(0, r'" + _BASE.replace('\\', '/') + "')",
        "from datetime import datetime, date",
        "import update_db, duckdb",
        "canonical = " + repr(canonical),
        "day = " + repr(day),
        "fn = getattr(update_db, 'sync_' + canonical, None)",
        "if fn is None:",
        "    print('__NO_FN__' + canonical)",
        "    sys.exit(0)",
        "d_obj = datetime.strptime(day, '%Y-%m-%d').date() if day else date.today()",
        "con = duckdb.connect(r'" + DUCKDB_PATH + "', read_only=False)",
        "ok, rows, err = fn(con, d_obj)",
        "con.close()",
        "print('__RESULT__' + json.dumps({canonical: {'ok': ok, 'rows': rows, 'error': err}}, ensure_ascii=False, default=str))",
    ]
    code = "\n".join(code_lines)
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                                cwd=_BASE, capture_output=True, text=True,
                                timeout=300, encoding="utf-8", errors="replace")
        out["finished_at"] = _now()
        if proc.stderr:
            out["subprocess_stderr"] = proc.stderr[:500]
        text = proc.stdout
        if "__NO_FN__" in text:
            # 错误诊断:列出 update_db 里实际可用的 sync_* 函数
            try:
                avail_proc = subprocess.run(
                    [sys.executable, "-c",
                     "import sys; sys.path.insert(0, r'" + _BASE.replace('\\', '/') + "'); "
                     "import update_db; print('|'.join(sorted([n for n in dir(update_db) if n.startswith('sync_')])))"],
                    cwd=_BASE, capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace")
                avail = [n[len("sync_"):] for n in (avail_proc.stdout or "").split("|") if n]
            except Exception:
                avail = []
            out["error"] = f"update_db.sync_{canonical} 不存在"
            out["available_tables"] = avail
            out["logs"] = [{"ts": _now(), "level": "error",
                            "msg": f"{out['error']} (可用的: {', '.join(avail[:8])}...)"}]
        else:
            idx = text.rfind("__RESULT__")
            if idx >= 0:
                tables = json.loads(text[idx + len("__RESULT__"):])
                out["tables"] = tables
                t_info = tables.get(canonical, {})
                out["ok"] = bool(t_info.get("ok"))
                out["logs"] = [
                    {"ts": _now(),
                     "level": "info" if t_info.get("ok") else "warn",
                     "msg": f"{canonical}: ok={t_info.get('ok')} rows={t_info.get('rows')} " + (f"err={(t_info.get('error') or '')[:120]}" if t_info.get('error') else "")},
                ]
            else:
                out["error"] = f"子进程无结果: {proc.stderr[:300]}"
    except subprocess.TimeoutExpired:
        out["error"] = "子进程超时 (300s)"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def get_sync_status(job_id: str | None = None) -> dict:
    """读同步任务状态."""
    with _SYNC_LOCK:
        jobs = {jid: dict(j) for jid, j in _SYNC_JOBS.items()}
    if job_id:
        job = jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job 不存在"}
        return {"ok": True, "job": job}
    if not jobs:
        return {"ok": True, "jobs": [], "latest": None}
    latest_id = max(jobs.keys(),
                    key=lambda k: jobs[k].get("started_at") or "")
    return {"ok": True, "jobs": list(jobs.keys()),
            "latest": jobs[latest_id]}


def list_syncable_tables() -> dict:
    """读 update_db 已注册的所有 sync_ 函数, 返回可手动同步的表清单."""
    import update_db
    funcs = [n for n in dir(update_db) if n.startswith("sync_") and callable(getattr(update_db, n))]
    out = []
    for n in funcs:
        t = n[len("sync_"):]
        zh = _zh_for(t)
        out.append({"table": t, "name_zh": zh["zh"], "group": zh["group"]})
    # 增补 update_all 支持但无 sync_ 的表
    return {"ok": True, "tables": out, "count": len(out)}


# ---------- 市场看板 (Dashboard) ----------
def read_market_board() -> dict:
    """聚合: KPI 指标 + 主流指数 ticker + 市场宽度条 + 涨跌幅分布 + 情绪雷达 + 连板梯队.
    数据源: DuckDB daily_bars + valuation + market.json + daily_summary.
    """
    # [2026-09-05 迁移] BAR_STORE=h5i 时走 h5i 等价实现 (返回结构/数值与 duck 版一致)
    if _h5i_mode():
        return _read_market_board_h5i()
    out = {"ok": True, "components": {}}
    if not os.path.exists(DUCKDB_PATH):
        return {"ok": False, "error": "DuckDB 不存在"}
    try:
        # 1) KPI 总览
        kpi_row = _duckdb_query("""
            SELECT
                MAX(date) AS latest_date,
                COUNT(DISTINCT symbol) AS universe,
                SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS n_up,
                SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS n_down,
                SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS n_flat,
                SUM(CASE WHEN change_pct >= 9.5 THEN 1 ELSE 0 END) AS n_limit_up,
                SUM(CASE WHEN change_pct <= -9.5 THEN 1 ELSE 0 END) AS n_limit_dn,
                SUM(amount) AS total_amount,
                AVG(change_pct) AS avg_chg,
                MEDIAN(change_pct) AS med_chg
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars)
        """)
        kpi = {}
        if kpi_row:
            r = kpi_row[0]
            kpi = {
                "latest_date": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])[:10]) if r[0] else None,
                "universe": int(r[1] or 0),
                "n_up": int(r[2] or 0),
                "n_down": int(r[3] or 0),
                "n_flat": int(r[4] or 0),
                "n_limit_up": int(r[5] or 0),
                "n_limit_dn": int(r[6] or 0),
                "total_amount": float(r[7] or 0),
                "avg_change_pct": float(r[8]) if r[8] is not None else 0.0,
                "median_change_pct": float(r[9]) if r[9] is not None else 0.0,
            }
            kpi["breadth_ratio"] = (kpi["n_up"] - kpi["n_down"]) / max(1, kpi["universe"])
        out["components"]["kpi"] = kpi
        # 2) 主流指数 (A 股核心指数)
        indices = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000300.SH", "沪深300"),
            ("000688.SH", "科创50"),
            ("000016.SH", "上证50"),
            ("399905.SZ", "中证500"),
            ("000852.SH", "中证1000"),
        ]
        # 优先用 valuation (有指数数据) 或 daily_bars (symbol 是 'sh000001' 等格式)
        # daily_bars 用的 symbol 是 6 位无后缀, 我们的代码用 prefix 'sh/sz' 区分
        idx_symbols_6 = ["000001", "399001", "399006", "000300", "000688",
                         "000016", "399905", "000852"]
        idx_rows = _duckdb_query("""
            SELECT symbol, date, close, change_pct
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars)
              AND symbol IN (?, ?, ?, ?, ?, ?, ?, ?)
            ORDER BY symbol
        """, idx_symbols_6)
        # name 映射
        name_map = {s: n for s, n in indices}
        idx_out = []
        for sym, date_, close, chg in idx_rows:
            idx_out.append({
                "symbol": sym,
                "name": name_map.get(sym + ".SH", name_map.get(sym + ".SZ", sym)),
                "close": float(close) if close is not None else None,
                "change_pct": float(chg) if chg is not None else None,
            })
        out["components"]["indices"] = idx_out
        # 3) 涨跌幅分布 (8 段: <-9.5, -9.5~-7, -7~-5, -5~-2, -2~0, 0~2, 2~5, 5~7, 7~9.5, >=9.5)
        dist_rows = _duckdb_query("""
            WITH latest AS (SELECT MAX(date) AS d FROM daily_bars),
            base AS (
                SELECT change_pct FROM daily_bars WHERE date = (SELECT d FROM latest)
            )
            SELECT
                SUM(CASE WHEN change_pct < -9.5 THEN 1 ELSE 0 END) AS d_lt_m95,
                SUM(CASE WHEN change_pct >= -9.5 AND change_pct < -7 THEN 1 ELSE 0 END) AS d_m95_m7,
                SUM(CASE WHEN change_pct >= -7 AND change_pct < -5 THEN 1 ELSE 0 END) AS d_m7_m5,
                SUM(CASE WHEN change_pct >= -5 AND change_pct < -2 THEN 1 ELSE 0 END) AS d_m5_m2,
                SUM(CASE WHEN change_pct >= -2 AND change_pct < 0 THEN 1 ELSE 0 END) AS d_m2_0,
                SUM(CASE WHEN change_pct >= 0 AND change_pct < 2 THEN 1 ELSE 0 END) AS d_0_2,
                SUM(CASE WHEN change_pct >= 2 AND change_pct < 5 THEN 1 ELSE 0 END) AS d_2_5,
                SUM(CASE WHEN change_pct >= 5 AND change_pct < 7 THEN 1 ELSE 0 END) AS d_5_7,
                SUM(CASE WHEN change_pct >= 7 AND change_pct < 9.5 THEN 1 ELSE 0 END) AS d_7_95,
                SUM(CASE WHEN change_pct >= 9.5 THEN 1 ELSE 0 END) AS d_ge_95,
                COUNT(*) AS total
            FROM base
        """)
        if dist_rows:
            r = dist_rows[0]
            labels = ["<-9.5", "-9.5~-7", "-7~-5", "-5~-2", "-2~0",
                      "0~2", "2~5", "5~7", "7~9.5", "≥9.5"]
            counts = [int(x or 0) for x in r[:10]]
            out["components"]["distribution"] = [
                {"label": lab, "count": c} for lab, c in zip(labels, counts)
            ]
        # 4) 情绪雷达 (从 market.json 取 sentiment_score + 衍生 5 维)
        market_dir = os.path.join(DATA_DIR, "market")
        sentiment_score = None
        market_data = None
        if os.path.exists(market_dir):
            days = sorted(os.listdir(market_dir), reverse=True)
            for d in days[:3]:
                p = os.path.join(market_dir, d, "market.json")
                if os.path.exists(p):
                    try:
                        market_data = json.load(open(p, encoding="utf-8"))
                        sentiment_score = market_data.get("sentiment_score")
                        break
                    except Exception:
                        pass
        if market_data:
            breadth = kpi.get("breadth_ratio", 0.0)
            avg_chg = kpi.get("avg_change_pct", 0.0)
            n_lim_up = kpi.get("n_limit_up", 0)
            n_lim_dn = kpi.get("n_limit_dn", 0)
            # 5 维雷达: 情绪/宽度/涨跌/涨停/活跃
            radar = [
                {"label": "情绪",      "value": max(0, min(100, sentiment_score or 50))},
                {"label": "宽度",      "value": max(0, min(100, (breadth + 1) * 50))},
                {"label": "涨跌",      "value": max(0, min(100, (avg_chg + 3) * 16.67))},
                {"label": "涨停强度",  "value": max(0, min(100, n_lim_up * 2))},
                {"label": "活跃度",    "value": max(0, min(100, (kpi.get("total_amount", 0) / 2e10) * 100))},
            ]
            out["components"]["radar"] = radar
            out["components"]["sentiment_score"] = sentiment_score
        # 5) 连板梯队: 按连续涨停天数聚合 (近似用 change_pct 序列, 简化版)
        # 完整实现需要按 symbol 看近 N 日连续 >=9.5, 这里用近似:
        # 取今日 change_pct >= 9.5 的股票, 按 turnover / amount 排名, 模拟连板池
        ladder_rows = _duckdb_query("""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars)
              AND change_pct >= 9.5
            ORDER BY amount DESC
            LIMIT 30
        """)
        # 简易分组: amount 前 1/3 为 4-5 板 (强), 中 1/3 为 2-3 板, 后 1/3 为 1 板
        n = len(ladder_rows)
        tiers = []
        if n:
            for i, r in enumerate(ladder_rows):
                if i < n * 0.15:
                    boards = 5
                elif i < n * 0.35:
                    boards = 4
                elif i < n * 0.6:
                    boards = 3
                elif i < n * 0.85:
                    boards = 2
                else:
                    boards = 1
                tiers.append({
                    "boards": boards,
                    "symbol": r[0],
                    "name": "",  # 简化: 不查 name
                    "close": float(r[1]) if r[1] is not None else None,
                    "change_pct": float(r[2]) if r[2] is not None else None,
                    "amount": float(r[3]) if r[3] is not None else None,
                })
        # 按板数聚合
        ladder_agg = {}
        for t in tiers:
            b = t["boards"]
            ladder_agg.setdefault(b, []).append(t)
        out["components"]["ladder"] = sorted([
            {"boards": b, "count": len(v), "stocks": v[:3]} for b, v in ladder_agg.items()
        ], key=lambda x: -x["boards"])
        # 6) 活跃 Top 5
        active_rows = _duckdb_query("""
            SELECT symbol, close, change_pct, amount
            FROM daily_bars
            WHERE date = (SELECT MAX(date) FROM daily_bars)
            ORDER BY amount DESC NULLS LAST
            LIMIT 10
        """)
        out["components"]["active_top"] = [
            {"symbol": r[0], "close": float(r[1]) if r[1] is not None else None,
             "change_pct": float(r[2]) if r[2] is not None else None,
             "amount": float(r[3]) if r[3] is not None else None}
            for r in active_rows
        ]
        # 7) 北向资金最近日
        nb_row = _duckdb_query("""
            SELECT trade_date, net_buy_amount, buy_amount, sell_amount,
                   daily_balance, sh_index_change_pct
            FROM northbound_money
            WHERE trade_date = (SELECT MAX(trade_date) FROM northbound_money)
        """)
        if nb_row:
            r = nb_row[0]
            out["components"]["northbound"] = {
                "trade_date": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])[:10]) if r[0] else None,
                "net_buy_amount": float(r[1]) if r[1] is not None else 0.0,
                "buy_amount": float(r[2]) if r[2] is not None else 0.0,
                "sell_amount": float(r[3]) if r[3] is not None else 0.0,
                "daily_balance": float(r[4]) if r[4] is not None else 0.0,
                "sh_index_change_pct": float(r[5]) if r[5] is not None else 0.0,
            }
        else:
            out["components"]["northbound"] = {"trade_date": None}
    except Exception as e:
        return {"ok": False, "error": f"DuckDB 异常: {e}"}
    return out


def _read_market_board_h5i() -> dict:
    """read_market_board 的 h5i 等价实现 (BAR_STORE=h5i).

    组件 1/2/3/5/6 (KPI/指数/宽度分布/连板/活跃) 改从 h5i daily_bars 读取
    (ts -> CAST(ts AS DATE)); 组件 4 情绪雷达来自 data/market/*/market.json
    (与 duck 版同文件); 组件 7 北向资金 (northbound_money, m4 已迁入 h5i)
    走 h5i -> duck 只读兜底, 全缺失时返回空结构. 返回结构/字段与 duck 版一致.
    """
    store = _h5i_store()
    out = {"ok": True, "components": {}}
    if store is None:
        return {"ok": False, "error": "h5i 不存在"}
    try:
        latest_date = _h5i_max_bar_date()
        if not latest_date:
            return {"ok": False, "error": "h5i daily_bars 无数据"}
        day = f"DATE '{latest_date}'"

        def q(sql):
            rows = _h5i_query(sql)
            return rows if rows is not None else []

        # 1) KPI 总览
        kpi_row = q(f"""
            SELECT
                MAX(CAST(ts AS DATE)) AS latest_date,
                COUNT(DISTINCT symbol) AS universe,
                SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS n_up,
                SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS n_down,
                SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS n_flat,
                SUM(CASE WHEN change_pct >= 9.5 THEN 1 ELSE 0 END) AS n_limit_up,
                SUM(CASE WHEN change_pct <= -9.5 THEN 1 ELSE 0 END) AS n_limit_dn,
                SUM(amount) AS total_amount,
                AVG(change_pct) AS avg_chg,
                MEDIAN(change_pct) AS med_chg
            FROM daily_bars
            WHERE CAST(ts AS DATE) = {day}
        """)
        kpi = {}
        if kpi_row:
            r = kpi_row[0]
            kpi = {
                "latest_date": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])[:10]) if r[0] else None,
                "universe": int(r[1] or 0),
                "n_up": int(r[2] or 0),
                "n_down": int(r[3] or 0),
                "n_flat": int(r[4] or 0),
                "n_limit_up": int(r[5] or 0),
                "n_limit_dn": int(r[6] or 0),
                "total_amount": float(r[7] or 0),
                "avg_change_pct": float(r[8]) if r[8] is not None else 0.0,
                "median_change_pct": float(r[9]) if r[9] is not None else 0.0,
            }
            kpi["breadth_ratio"] = (kpi["n_up"] - kpi["n_down"]) / max(1, kpi["universe"])
        out["components"]["kpi"] = kpi
        # 2) 主流指数 (A 股核心指数)
        indices = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000300.SH", "沪深300"),
            ("000688.SH", "科创50"),
            ("000016.SH", "上证50"),
            ("399905.SZ", "中证500"),
            ("000852.SH", "中证1000"),
        ]
        idx_symbols_6 = ["000001", "399001", "399006", "000300", "000688",
                         "000016", "399905", "000852"]
        ph = ",".join("'%s'" % s for s in idx_symbols_6)
        idx_rows = q(f"""
            SELECT symbol, CAST(ts AS DATE) AS date, close, change_pct
            FROM daily_bars
            WHERE CAST(ts AS DATE) = {day}
              AND symbol IN ({ph})
            ORDER BY symbol
        """)
        name_map = {s: n for s, n in indices}
        idx_out = []
        for sym, date_, close, chg in idx_rows:
            idx_out.append({
                "symbol": sym,
                "name": name_map.get(sym + ".SH", name_map.get(sym + ".SZ", sym)),
                "close": float(close) if close is not None else None,
                "change_pct": float(chg) if chg is not None else None,
            })
        out["components"]["indices"] = idx_out
        # 3) 涨跌幅分布 (与 duck 版同 10 段口径)
        dist_rows = q(f"""
            SELECT
                SUM(CASE WHEN change_pct < -9.5 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= -9.5 AND change_pct < -7 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= -7 AND change_pct < -5 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= -5 AND change_pct < -2 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= -2 AND change_pct < 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= 0 AND change_pct < 2 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= 2 AND change_pct < 5 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= 5 AND change_pct < 7 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= 7 AND change_pct < 9.5 THEN 1 ELSE 0 END),
                SUM(CASE WHEN change_pct >= 9.5 THEN 1 ELSE 0 END),
                COUNT(*) AS total
            FROM daily_bars
            WHERE CAST(ts AS DATE) = {day}
        """)
        if dist_rows:
            r = dist_rows[0]
            labels = ["<-9.5", "-9.5~-7", "-7~-5", "-5~-2", "-2~0",
                      "0~2", "2~5", "5~7", "7~9.5", "≥9.5"]
            counts = [int(x or 0) for x in r[:10]]
            out["components"]["distribution"] = [
                {"label": lab, "count": c} for lab, c in zip(labels, counts)
            ]
        # 4) 情绪雷达 (从 market.json 取 sentiment_score + 衍生 5 维, 与 duck 版同源)
        market_dir = os.path.join(DATA_DIR, "market")
        sentiment_score = None
        market_data = None
        if os.path.exists(market_dir):
            days = sorted(os.listdir(market_dir), reverse=True)
            for d in days[:3]:
                p = os.path.join(market_dir, d, "market.json")
                if os.path.exists(p):
                    try:
                        market_data = json.load(open(p, encoding="utf-8"))
                        sentiment_score = market_data.get("sentiment_score")
                        break
                    except Exception:
                        pass
        if market_data:
            breadth = kpi.get("breadth_ratio", 0.0)
            avg_chg = kpi.get("avg_change_pct", 0.0)
            n_lim_up = kpi.get("n_limit_up", 0)
            n_lim_dn = kpi.get("n_limit_dn", 0)
            radar = [
                {"label": "情绪",      "value": max(0, min(100, sentiment_score or 50))},
                {"label": "宽度",      "value": max(0, min(100, (breadth + 1) * 50))},
                {"label": "涨跌",      "value": max(0, min(100, (avg_chg + 3) * 16.67))},
                {"label": "涨停强度",  "value": max(0, min(100, n_lim_up * 2))},
                {"label": "活跃度",    "value": max(0, min(100, (kpi.get("total_amount", 0) / 2e10) * 100))},
            ]
            out["components"]["radar"] = radar
            out["components"]["sentiment_score"] = sentiment_score
        # 5) 连板梯队 (与 duck 版同近似口径)
        ladder_rows = q(f"""
            SELECT symbol, close, change_pct, amount, turnover
            FROM daily_bars
            WHERE CAST(ts AS DATE) = {day}
              AND change_pct >= 9.5
            ORDER BY amount DESC
            LIMIT 30
        """)
        n = len(ladder_rows)
        tiers = []
        if n:
            for i, r in enumerate(ladder_rows):
                if i < n * 0.15:
                    boards = 5
                elif i < n * 0.35:
                    boards = 4
                elif i < n * 0.6:
                    boards = 3
                elif i < n * 0.85:
                    boards = 2
                else:
                    boards = 1
                tiers.append({
                    "boards": boards,
                    "symbol": r[0],
                    "name": "",
                    "close": float(r[1]) if r[1] is not None else None,
                    "change_pct": float(r[2]) if r[2] is not None else None,
                    "amount": float(r[3]) if r[3] is not None else None,
                })
        ladder_agg = {}
        for t in tiers:
            b = t["boards"]
            ladder_agg.setdefault(b, []).append(t)
        out["components"]["ladder"] = sorted([
            {"boards": b, "count": len(v), "stocks": v[:3]} for b, v in ladder_agg.items()
        ], key=lambda x: -x["boards"])
        # 6) 活跃 Top 10
        active_rows = q(f"""
            SELECT symbol, close, change_pct, amount
            FROM daily_bars
            WHERE CAST(ts AS DATE) = {day}
            ORDER BY amount DESC NULLS LAST
            LIMIT 10
        """)
        out["components"]["active_top"] = [
            {"symbol": r[0], "close": float(r[1]) if r[1] is not None else None,
             "change_pct": float(r[2]) if r[2] is not None else None,
             "amount": float(r[3]) if r[3] is not None else None}
            for r in active_rows
        ]
        # 7) 北向资金最近日 (优先 h5i.northbound_money, duck 只读兜底, 缺失空结构)
        nb_row = _northbound_latest()
        if nb_row:
            r = nb_row[0]
            out["components"]["northbound"] = {
                "trade_date": (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])[:10]) if r[0] else None,
                "net_buy_amount": float(r[1]) if r[1] is not None else 0.0,
                "buy_amount": float(r[2]) if r[2] is not None else 0.0,
                "sell_amount": float(r[3]) if r[3] is not None else 0.0,
                "daily_balance": float(r[4]) if r[4] is not None else 0.0,
                "sh_index_change_pct": float(r[5]) if r[5] is not None else 0.0,
            }
        else:
            out["components"]["northbound"] = {"trade_date": None}
    except Exception as e:
        return {"ok": False, "error": f"h5i 异常: {e}"}
    return out


def read_logs(pos: int = 0, limit: int = 300):
    """读取守护日志增量. pos=字符偏移, 返回 {lines, pos, eof}.
    日志来源: logs/daemon_tail.log (守护进程实时收集引擎/收盘/守护日志)."""
    p = os.path.join(_BASE, "logs", "daemon_tail.log")
    lines = []
    eof = True
    new_pos = pos
    if os.path.exists(p):
        try:
            size = os.path.getsize(p)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                if pos > 0 and pos <= size:
                    f.seek(pos)
                    new = f.read()
                else:
                    # 从头读, 截取尾部 limit 行
                    f.seek(0)
                    all_lines = f.readlines()
                    lines = all_lines[-limit:]
                    new_pos = size
                    new = None
                if new:
                    for ln in new.splitlines():
                        if ln.strip():
                            lines.append(ln)
                    new_pos = size
                    if len(lines) > limit:
                        lines = lines[-limit:]
        except Exception:
            pass
    return {"lines": lines, "pos": new_pos, "eof": eof}


def fallback_state():
    """live_state 缺省时回退到 state.json, 保证页面可渲染."""
    sp = os.path.join(DATA_DIR, "state.json")
    if os.path.exists(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                st = json.load(f)
            source_updated = st.get("updated") or st.get("timestamp")
            if not source_updated:
                source_updated = datetime.fromtimestamp(
                    os.path.getmtime(sp), _SHANGHAI
                ).strftime("%Y-%m-%d %H:%M:%S")
            return {
                "updated": source_updated,
                "stale": True,
                "stale_reason": "live_state_unavailable",
                "day": st.get("day"),
                "mode": "离线(显示最近快照)",
                "in_session": False,
                "live_source": "snapshot",
                "capital": {
                    "init_capital": INIT_CAPITAL,
                    "equity": st.get("equity"),
                    "cash": st.get("cash"),
                    "market_value": round(sum(
                        p.get("qty", 0) * p.get("last_price", 0)
                        for p in st.get("positions", {}).values()), 2),
                    "realized": st.get("realized", 0),
                },
                "positions": [
                    {"canon": c, "name": c, "qty": p.get("qty"),
                     "avg_cost": p.get("avg_cost"), "last_price": p.get("last_price"),
                     "mv": round(p.get("qty", 0) * p.get("last_price", 0), 2),
                     "pnl_pct": round((p.get("last_price", 0) / p.get("avg_cost", 1) - 1) * 100, 2)
                     if p.get("avg_cost") else 0,
                     "tplus1_locked": False}
                    for c, p in st.get("positions", {}).items()
                ],
                "targets": [],
                "trades_today": [],
            }
        except Exception:
            pass
    return {}


def _load_dashboard_page(path: Path = TEMPLATE_PATH) -> str:
    """启动时读取静态模板；模板缺失必须在启动阶段暴露。"""
    template_path = Path(path)
    if not template_path.is_file():
        raise FileNotFoundError(
            f"Dashboard template not found: {template_path.resolve()}"
        )
    return template_path.read_text(encoding="utf-8")


def _static_resource(raw_path: str, if_none_match: str | None = None):
    """读取白名单静态资源，返回 ``(status, headers, body)``。"""
    path = urlsplit(raw_path).path
    decoded = unquote(path)
    if "\x00" in decoded or "\\" in decoded:
        return 403, {}, b""
    prefix = "/static/"
    if not decoded.startswith(prefix):
        return 403, {}, b""

    relative = decoded[len(prefix):]
    canonical = posixpath.normpath(relative)
    if (not canonical or canonical == "." or canonical.startswith("../")
            or canonical.startswith("/")):
        return 403, {}, b""
    if canonical not in ALLOWED_STATIC:
        return 403, {}, b""

    static_root = Path(STATIC_ROOT).resolve()
    candidate = (static_root / Path(*canonical.split("/"))).resolve()
    if not candidate.is_relative_to(static_root):
        return 403, {}, b""
    if not candidate.is_file():
        return 404, {}, b""

    stat = candidate.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {
        "Content-Type": ALLOWED_STATIC[canonical],
        "Cache-Control": "no-cache",
        "ETag": etag,
    }
    if if_none_match == etag:
        return 304, headers, b""
    return 200, headers, candidate.read_bytes()


PAGE = _load_dashboard_page()


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200, etag=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self):
        status, headers, body = _static_resource(
            self.path, self.headers.get("If-None-Match")
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self):
        """读 POST body 并解析为 JSON dict. 无 body 返回 {}."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            raise ValueError(f"JSON 解析失败: {e}")

    def log_message(self, *a):
        pass  # 静默访问日志

    def do_GET(self):
        # 复用 do_GET 路径, POST 也走同一段分发
        return self._dispatch()

    def do_POST(self):
        return self._dispatch()

    def _dispatch(self):
        path = self.path.split("?")[0]
        if path.startswith("/static/"):
            return self._static()
        if path == "/api/db_stats/latest":
            try:
                import db_stats as _dbs
                import tasks_db as _tdb
                hist = _dbs.history_frame(limit=300)
                last = {nm: pts[-1] for nm, pts in hist.items()}
                return self._json({"ok": True, "last": last, "history": hist,
                                   "update": _tdb._read_state(),
                                   "run_daily": _tdb.read_run_daily_state()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if path == "/api/db_stats/refresh":
            try:
                import db_stats as _dbs
                rec = _dbs.append_history()
                return self._json({"ok": True, "snap_ts": rec["ts"]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if path == "/api/db_stats/manual":
            if self.command != "POST":
                return self._json({"ok": False, "error": "use POST"})
            try:
                import tasks_db as _tdb
                return self._json(_tdb.enqueue_update())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if path == "/api/db_stats/run_daily":
            if self.command != "POST":
                return self._json({"ok": False, "error": "use POST"})
            try:
                import tasks_db as _tdb
                return self._json(_tdb.enqueue_run_daily())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if path == "/api/health":
            # 健康检查端点: dashboard_keepalive.py 用 HTTP 200 判定服务可用性。
            # 2026-09-19: 此处曾内联返回 {ok,deps} 并**遮蔽**了下方 read_health(),
            # 而前端 renderHealth 期望 {level,summary,checks[]} ⇒ 卡片渲染成
            # "OK + 空表"。现统一走 read_health_merged()(运行期 + 盘前 + 阻塞项)。
            return self._json(read_health_merged())
        if path == "/api/live":
            lv = read_live_payload()
            source_path = LIVE_STATE if os.path.exists(LIVE_STATE) else os.path.join(DATA_DIR, "state.json")
            return self._json(lv, etag=source_etag(source_path))
        if path == "/api/signal-freeze":
            return self._json(read_signal_freeze_status())
        if path == "/api/history":
            return self._json(read_history())
        if path == "/api/backtest":
            return self._json(read_backtest())
        if path == "/api/market":
            return self._json(read_market())
        if path == "/api/perf":
            return self._json(read_perf())
        if path == "/api/drl":
            return self._json(read_drl())
        if path == "/api/degradation":
            return self._json(read_degradation())
        if path == "/api/weights":
            return self._json(read_weights())
        if path == "/api/acceptance":
            return self._json(read_acceptance())
        if path == "/api/views":
            return self._json(read_views())
        if path == "/api/target_plan":
            return self._json(read_target_plan())
        if path == "/api/logs":
            # /api/logs?pos=字符偏移 -> 增量日志
            qs = self.path.split("?", 1)
            pos = 0
            if len(qs) > 1:
                import urllib.parse
                params = urllib.parse.parse_qs(qs[1])
                try:
                    pos = int(params.get("pos", ["0"])[0])
                except Exception:
                    pos = 0
            return self._json(read_logs(pos=pos))
        # ===================== 新 8 面板 API =====================
        if path == "/api/backtest/history":
            return self._json(read_backtest_history())
        if path == "/api/concept":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            tag = (params.get("tag") or [""])[0] or None
            try: top_n = int((params.get("top") or ["50"])[0])
            except Exception: top_n = 50
            sort_by = (params.get("sort") or ["strength"])[0]
            return self._json(read_concept(tag=tag, top_n=top_n, sort_by=sort_by))
        if path == "/api/industry":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            tag = (params.get("tag") or [""])[0] or None
            try: top_n = int((params.get("top") or ["50"])[0])
            except Exception: top_n = 50
            sort_by = (params.get("sort") or ["strength"])[0]
            return self._json(read_industry(tag=tag, top_n=top_n, sort_by=sort_by))
        if path == "/api/concept/symbol":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            sym = (params.get("sym") or [""])[0]
            return self._json(read_concept_symbol(sym))
        if path == "/api/concept/symbols":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            name = (params.get("name") or [""])[0]
            return self._json(read_concept_name_symbols(name))
        if path == "/api/industry/symbols":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            name = (params.get("name") or [""])[0]
            return self._json(read_industry_name_symbols(name))
        if path == "/api/monitor":
            return self._json(read_monitor())
        if path == "/api/regime":
            return self._json(read_regime())
        if path == "/api/overview":
            return self._json(read_overview())
        if path == "/api/kline":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            sym = (params.get("sym") or [""])[0]
            try:
                days = int((params.get("days") or ["200"])[0])
            except Exception:
                days = 200
            return self._json(read_kline(sym=sym, days=days))
        if path == "/api/curve":
            return self._json(read_curve())
        if path == "/api/monthly_returns":
            return self._json(read_monthly())
        if path == "/api/holdings_profile":
            return self._json(read_holdings_profile())
        if path == "/api/riskops":
            return self._json(read_riskops())
        if path == "/api/alerts":
            return self._json(read_alerts())
        if path == "/api/factor_lab":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            try:
                days = int((params.get("days") or ["300"])[0])
            except Exception:
                days = 300
            return self._json(read_factor_lab(days=days))
        if path == "/api/bt_scan":
            qs = self.path.split("?", 1)
            force = "force" in (qs[1] if len(qs) > 1 else "")
            return self._json(read_bt_scan(force=force))
        if path == "/api/push":
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln).decode("utf-8") if ln else "{}"
                body = json.loads(raw or "{}")
            except Exception:
                body = {}
            return self._json(_send_push(str(body.get("channel", "")),
                                         str(body.get("message", ""))))
        if path == "/api/abnormal":
            qs = self.path.split("?", 1)
            limit = 50
            if len(qs) > 1:
                import urllib.parse
                params = urllib.parse.parse_qs(qs[1])
                try:
                    limit = int(params.get("limit", ["50"])[0])
                except Exception:
                    limit = 50
            return self._json(read_abnormal(limit=limit))
        if path == "/api/db_meta":
            qs = self.path.split("?", 1)
            import urllib.parse
            params = urllib.parse.parse_qs(qs[1]) if len(qs) > 1 else {}
            # ?refresh=1 强制重算 (绕过 60s 缓存)
            if (params.get("refresh") or ["0"])[0] in ("1", "true", "yes"):
                _AGG_CACHE.pop("db_meta", None)
            return self._json(read_db_meta())
        if path == "/api/db_sync/incremental":
            if self.command == "POST":
                # raw body 读 (避免 _read_json 在 manual 路由后流位置错乱)
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length > 0 else b""
                try:
                    body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except Exception as e:
                    self._json({"ok": False, "error": f"body 解析失败: {e}"})
                    return
                only = body.get("only")
                day = body.get("day")
                if isinstance(only, str):
                    only = [only]
                try:
                    _LAST_SYNC_RESULT.clear()
                    _LAST_SYNC_RESULT.update({
                        "job_id": f"incr_{int(time.time())}",
                        "status": "running", "started_at": _now(),
                        "logs": [{"ts": _now(), "level": "info", "msg": f"开始增量同步 day={day or 'today'} only={only or 'all'}"}],
                    })
                    result = start_incremental_sync(only=only, day=day)
                    _LAST_SYNC_RESULT.update({
                        "status": "ok" if result.get("ok") else ("error" if result.get("error") else "partial"),
                        "finished_at": _now(),
                        "tables": result.get("tables") or {},
                        "logs": result.get("logs") or _LAST_SYNC_RESULT.get("logs", []),
                        "error": result.get("error"),
                    })
                    self._json({"ok": True, "job_id": _LAST_SYNC_RESULT["job_id"], "result": result})
                except Exception as e:
                    import traceback as _tb
                    _tb.print_exc()
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            self._json({"ok": False, "error": "method not allowed"})
            return
        if path == "/api/db_sync/manual":
            if self.command == "POST":
                # 不 _read_json, 直接读 raw body 解析
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length > 0 else b""
                try:
                    body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
                except Exception as e:
                    self._json({"ok": False, "error": f"body 解析失败: {e}"})
                    return
                table = body.get("table")
                day = body.get("day")
                if not table:
                    self._json({"ok": False, "error": "table 不能为空"})
                    return
                try:
                    result = start_manual_sync_table(table, day=day)
                    self._json({"ok": True, "result": result})
                except Exception as e:
                    import traceback as _tb
                    _tb.print_exc()
                    self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            self._json({"ok": False, "error": "method not allowed"})
            return
        if path.startswith("/api/db_sync/status"):
            # 返回最近一次同步结果 (内存, 同步模式不需要轮询)
            return self._json({"ok": True, "job": _LAST_SYNC_RESULT if _LAST_SYNC_RESULT else None})
        if path == "/api/db_sync/list":
            return self._json(list_syncable_tables())
        if path == "/api/marketboard":
            return self._json(read_market_board())
        if path.startswith("/api/db_table/"):
            # 注意: dispatch() 入口已经把 self.path.split('?')[0] 给了 path,
            # 所以这里的 path 不含 query string, 需要从 self.path 重新解析.
            raw = self.path
            qs = raw.split("?", 1)
            table = qs[0][len("/api/db_table/"):]
            limit = 50
            if len(qs) > 1:
                import urllib.parse
                params = urllib.parse.parse_qs(qs[1])
                try:
                    limit = int(params.get("limit", ["50"])[0])
                except Exception:
                    limit = 50
            if validate_dashboard_table_name(table) is None:
                return self._json({"ok": False, "error": "table not allowed",
                                   "code": "TABLE_NOT_ALLOWED"}, code=403)
            return self._json(read_db_table(table, limit=limit))
        if path in ("/", "/index.html"):
            return self._html(PAGE)
        # 其余404
        return self._html("404", 404)





def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--threading", action="store_true", default=True,
                    help="启用多线程 (默认开启)")
    ap.add_argument("--no-threading", action="store_true", dest="no_threading",
                    help="禁用多线程回退单线程 (Python 3.14 + DuckDB + selectors 偶发 GIL 崩溃时用)")
    ap.add_argument("--pidfile", default=os.path.join(_BASE, "logs", "dashboard.pid"),
                    help="写入 PID 文件 (供 dashboard_keepalive.py 监控)")
    args = ap.parse_args()
    threading_mode = bool(args.threading) and not args.no_threading
    if threading_mode:
        class _ThreadingHTTPServer(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        # Dashboard is an operator-local surface; do not expose it on every
        # interface by default.  Remote access should go through an explicit
        # reverse proxy or tunnel with its own authentication.
        httpd = _ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    else:
        # 单线程 HTTPServer: 避免 Python 3.14 + DuckDB 在 Threading 下的 selectors GIL 冲突
        class _SingleThreadHTTPServer(HTTPServer):
            """BaseHTTPServer 默认就单线程处理, 这里只是显式标注."""
            allow_reuse_address = True
        httpd = _SingleThreadHTTPServer(("127.0.0.1", args.port), Handler)
    # 写 PID 文件 (供外部 keepalive 检测)
    try:
        os.makedirs(os.path.dirname(args.pidfile), exist_ok=True)
        with open(args.pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"[warn] 写 pidfile 失败: {e}")
    print(f"可视化仪表盘: http://localhost:{args.port} ({'threading' if threading_mode else 'single-thread'}) pid={os.getpid()}")
    # 端口占用早期检测
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(["netstat", "-ano"], text=True,
                                          encoding="utf-8", errors="replace")
            needle = f":{args.port}"
            for line in out.splitlines():
                if needle in line and "LISTENING" in line:
                    parts = line.split()
                    try:
                        holder_pid = int(parts[-1])
                        if holder_pid != os.getpid():
                            print(f"[warn] 端口 {args.port} 已被 pid={holder_pid} 占用, "
                                  f"如果不是你想要的, 请先 stop 旧进程")
                    except Exception:
                        pass
                    break
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        try:
            if os.path.exists(args.pidfile):
                os.remove(args.pidfile)
        except Exception:
            pass


if __name__ == "__main__":
    main()

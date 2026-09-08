# ============================================================
# dashboard.py -- 全A轮动模拟盘 Web 可视化
# 访问: http://localhost:8000
# 数据: 读取 data/live_state.json (盘中引擎实时写入)
#       以及 data/daily/<日>/paper_book.json + trades.json
# 特性: 纯标准库, 无第三方依赖. 前端每3s轮询 /api/live.
#   GET /            -> 可视化仪表盘(内联HTML)
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, HTTPServer

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE, "data")
LIVE_STATE = os.path.join(DATA_DIR, "live_state.json")
DAILY_DIR = os.path.join(DATA_DIR, "daily")
sys.path.insert(0, _BASE)
from config import INIT_CAPITAL, DUCKDB_PATH


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
    from h5i_bar_store import H5iBarStore  # noqa: 延迟导入
    return H5iBarStore()


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


def read_history(n: int = 10):
    """按日期倒序读 data/daily/*/daily_summary.json 基础字段."""
    out = []
    dirs = sorted(glob.glob(os.path.join(DAILY_DIR, "*")), reverse=True)
    for d in dirs[:n]:
        if not os.path.isdir(d):
            continue
        day = os.path.basename(d)
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
    """探测 pid 进程是否存活 (Windows: OpenProcess; 其它: os.kill(pid,0))."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
        if os.path.basename(d) in ("day",):
            continue  # 历史误创建的占位目录
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
    except Exception:
        pass
    return out


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
        alive = _proc_alive(pid) if pid else False
        if not alive:
            adds("critical", f"{name}进程", f"pid={pid} 未存活")
    try:
        with open(os.path.join(DATA_DIR, "live_state.json"), encoding="utf-8") as f:
            lv = json.load(f)
        upd = lv.get("updated") or ""
        if upd:
            try:
                age = (datetime.now() - datetime.strptime(upd, "%Y-%m-%d %H:%M:%S")).total_seconds()
                if lv.get("in_session") and age > 120:
                    adds("warn", "引擎心跳", f"盘中状态但 {int(age)}s 未刷新")
            except Exception:
                pass
    except Exception:
        pass
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
    rows = _read_view_parquet(
        "v_market_breadth.parquet", "ORDER BY date DESC LIMIT 30", _BREADTH_COLS)
    src = "parquet"
    if rows is None:
        src = "duckdb"
        try:
            rows = _duckdb_query("""
                SELECT date, total, n_up, n_down, n_limit_up, n_limit_dn,
                       total_amount, avg_change_pct, breadth_ratio
                FROM v_market_breadth
                ORDER BY date DESC
                LIMIT 30
            """)
        except Exception as e:
            rows = None
            out["components"]["breadth_error"] = str(e)
    if rows:
        out["components"]["breadth"] = [
            {"date": str(r[0]), "total": r[1], "n_up": r[2] or 0,
             "n_down": r[3] or 0, "n_limit_up": r[4] or 0,
             "n_limit_dn": r[5] or 0, "total_amount": r[6],
             "avg_chg": r[7], "breadth_ratio": r[8]}
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
    ic_rows = _read_view_parquet(
        "v_factor_ic_latest.parquet", "ORDER BY factor", _IC_COLS)
    ic_src = "parquet" if ic_rows is not None else "duckdb"
    if ic_rows is None:
        try:
            ic_rows = _duckdb_query(
                "SELECT factor, ic_value, mean_20, std_20, icir_20, win_rate_20, n_days "
                "FROM v_factor_ic_latest ORDER BY factor"
            )
        except Exception as e:
            ic_rows = None
            out["components"]["factor_ic_error"] = str(e)
    if ic_rows:
        out["components"]["factor_ic"] = [
            {"factor": r[0], "ic_value": r[1], "mean_20": r[2],
             "std_20": r[3], "icir_20": r[4], "win_rate_20": r[5],
             "n_days": r[6]}
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
        return subprocess.run([sys.executable, "-c", code],
                                cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8")
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
                                timeout=300, encoding="utf-8")
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
                    cwd=_BASE, capture_output=True, text=True, timeout=10)
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
            return {
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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


# ---------------- HTTP Handler ----------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
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
        if path == "/api/health":
            # 健康检查端点: dashboard_keepalive.py 用此判定服务可用性
            # 顺便检查 DuckDB / ArcticDB 是否能连, 提供给运维 dashboard.
            health = {
                "ok": True,
                "service": "dashboard",
                "pid": os.getpid(),
                "ts": _now(),
                "deps": {
                    "duckdb": False,
                    "arcticdb": False,
                },
            }
            try:
                import duckdb as _d
                con = _d.connect(DUCKDB_PATH, read_only=True)
                con.execute("SELECT 1").fetchone()
                con.close()
                health["deps"]["duckdb"] = True
            except Exception as e:
                health["deps"]["duckdb_error"] = str(e)[:120]
            try:
                from arctic_store import get_store as _gs
                _gs().health_check()
                health["deps"]["arcticdb"] = True
            except Exception as e:
                health["deps"]["arcticdb_error"] = str(e)[:120]
            return self._json(health)
        if path == "/api/live":
            lv = read_live() or fallback_state()
            return self._json(lv)
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
        if path == "/api/health":
            return self._json(read_health())
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
            return self._json(read_db_table(table, limit=limit))
        if path in ("/", "/index.html"):
            return self._html(PAGE)
        # 其余404
        return self._html("404", 404)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全A轮动 · 模拟盘 · Tick Stock Panel</title>
<link rel="preconnect" href="https://rsms.me/">
<link rel="stylesheet" href="https://rsms.me/inter/inter.css">
<style>
  /* ===== §1 设计变量 (HSL 形式, 兼容 Tailwind alpha) ===== */
  :root{
    --base: 222 24% 6%;          /* #0e1117  底色 */
    --surface: 222 22% 9%;      /* #14171f  卡片 */
    --elevated: 222 18% 13%;    /* #1c1f29  提升层 */
    --border: 222 14% 22%;      /* #2a2e3a  边线 */
    --line: 222 14% 22%;
    --fg-primary: 0 0% 98%;      /* #fafafa  主文字 */
    --fg-secondary: 222 12% 70%;/* #a8b0bf  次文字 */
    --fg-muted: 222 10% 56%;     /* #7a8294  弱文字 */
    --accent: 217 91% 60%;      /* #3b82f6  强调蓝 */
    --acc2: 152 67% 45%;        /* #12b76a  涨绿 (备用) */
    --bull: 4 87% 60%;          /* #f04438  红涨 (A 股惯例) */
    --bear: 152 67% 45%;        /* #12b76a  绿跌 */
    --warn: 32 95% 50%;         /* #f79009  警告 */
    --danger: 4 87% 60%;        /* #f04438  危险 */
    --info: 217 91% 60%;
    --p0: 4 87% 60%;            /* P0 严重 = 红 */
    --p1: 32 95% 50%;            /* P1 高 = 橙 */
    --p2: 217 91% 60%;           /* P2 中 = 蓝 */
    --p3: 152 67% 45%;           /* P3 低 = 绿 */
    --shadow-1: 0 1px 2px hsl(222 30% 2% / 0.4);
    --shadow-2: 0 4px 12px hsl(222 30% 2% / 0.5), 0 1px 2px hsl(222 30% 2% / 0.4);
    --shadow-glow: 0 0 0 1px hsl(var(--accent) / 0.3), 0 4px 12px hsl(var(--accent) / 0.15);
    --radius-sm: 6px;
    --radius: 10px;
    --radius-lg: 14px;
    --ease-smooth: cubic-bezier(.4, 0, .2, 1);
    /* 兼容老代码的别名 (老 JS/Python 里直接拼 var(--txt|--muted|--up|--dn|--panel)) */
    --txt: hsl(var(--fg-primary));
    --muted: hsl(var(--fg-muted));
    --up: hsl(var(--bull));
    --dn: hsl(var(--bear));
    --panel: hsl(var(--surface));
  }
  /* ===== §2 全局 ===== */
  *{box-sizing:border-box;margin:0;padding:0;border-color:hsl(var(--border))}
  html,body{height:100%}
  body{
    font-family:'Inter','Segoe UI','Microsoft YaHei',system-ui,sans-serif;
    background: hsl(var(--base));
    color: hsl(var(--fg-primary));
    padding: 0;
    font-feature-settings: 'cv02','cv03','cv04','cv11';
    -webkit-font-smoothing: subpixel-antialiased;
    -moz-osx-font-smoothing: auto;
    text-rendering: optimizeLegibility;
  }
  /* 全局滚动条 */
  *{scrollbar-width: thin;scrollbar-color: hsl(var(--border)) transparent}
  *::-webkit-scrollbar{width:8px;height:8px}
  *::-webkit-scrollbar-track{background:transparent}
  *::-webkit-scrollbar-thumb{background:hsl(var(--border)/.72);border:2px solid transparent;border-radius:999px;background-clip:content-box}
  *::-webkit-scrollbar-thumb:hover{background:hsl(var(--accent)/.75);background-clip:content-box}
  /* 等宽数字 */
  .num,.val,code,kbd,samp{font-variant-numeric: tabular-nums; font-family:'JetBrains Mono','SF Mono','Consolas','Microsoft YaHei',monospace}

  /* ===== §3 顶栏 (header) ===== */
  .topbar{
    position: sticky; top: 0; z-index: 50;
    backdrop-filter: blur(12px) saturate(140%);
    -webkit-backdrop-filter: blur(12px) saturate(140%);
    background: hsl(var(--base)/.85);
    border-bottom: 1px solid hsl(var(--border));
    padding: 12px 28px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }
  .topbar .brand{display:flex; align-items:center; gap:10px}
  .topbar .logo{
    width: 32px; height: 32px;
    background: linear-gradient(135deg, hsl(var(--accent)) 0%, hsl(var(--bull)) 100%);
    border-radius: 8px;
    display: grid; place-items: center;
    color: #fff;
    font-weight: 800;
    box-shadow: var(--shadow-2);
    letter-spacing: -0.5px;
  }
  .topbar h1{font-size:16px; font-weight:600; letter-spacing:-.2px; margin:0}
  .topbar h1 .sub{color: hsl(var(--fg-muted)); font-weight:400; font-size:12px; margin-left:8px}
  .topbar .badges{display:flex; gap:6px; flex-wrap:wrap}
  .badge{
    display:inline-flex; align-items:center; gap:5px;
    background: hsl(var(--elevated));
    border: 1px solid hsl(var(--border));
    color: hsl(var(--fg-secondary));
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px;
    line-height: 1;
    transition: all .15s var(--ease-smooth);
  }
  .badge .dot{width:6px; height:6px; border-radius:99px; background: hsl(var(--fg-muted))}
  .badge.live{color: hsl(var(--bear)); border-color: hsl(var(--bear)/.4)}
  .badge.live .dot{background: hsl(var(--bear)); box-shadow:0 0 8px hsl(var(--bear)/.6); animation:pulse 1.6s ease-in-out infinite}
  .badge.idle{color: hsl(var(--warn)); border-color: hsl(var(--warn)/.4)}
  .badge.idle .dot{background: hsl(var(--warn))}
  .badge.ok{color: hsl(var(--acc2)); border-color: hsl(var(--acc2)/.4)}
  .badge.ok .dot{background: hsl(var(--acc2))}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

  /* ===== §4 主体 ===== */
  .wrap{max-width: 1480px; margin: 0 auto; padding: 24px 28px 60px}
  .pg-head{display:flex; align-items:flex-end; justify-content:space-between; gap:12px; margin: 4px 0 18px}
  .pg-head h2{font-size:18px; font-weight:600; letter-spacing:-.2px; margin:0}
  .pg-head h2 small{color: hsl(var(--fg-muted)); font-weight:400; font-size:12px; margin-left:8px; letter-spacing:0}
  .pg-head .right{display:flex; gap:8px; align-items:center}

  /* ===== §5 Tabs ===== */
  .tabs{
    display: flex; gap: 4px; flex-wrap: wrap;
    border-bottom: 1px solid hsl(var(--border));
    padding: 0 4px;
    margin-bottom: 22px;
    overflow-x: auto;
  }
  .tabBtn{
    background: transparent;
    color: hsl(var(--fg-secondary));
    border: 0;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all .15s var(--ease-smooth);
    white-space: nowrap;
    line-height: 1;
  }
  .tabBtn:hover{color: hsl(var(--fg-primary)); background: hsl(var(--surface)/.6)}
  .tabBtn.active{
    color: hsl(var(--accent));
    border-bottom-color: hsl(var(--accent));
  }
  .tabBtn .ico{
    display: inline-block;
    margin-right: 5px;
    color: hsl(var(--fg-muted));
    font-weight: 400;
    font-size: 13px;
    transition: color .15s var(--ease-smooth);
  }
  .tabBtn:hover .ico{color: hsl(var(--accent))}
  .tabBtn .badge-count{
    display:inline-block; margin-left:5px;
    background: hsl(var(--elevated));
    color: hsl(var(--fg-muted));
    border-radius: 99px;
    padding: 1px 6px;
    font-size: 10px;
    font-family: 'JetBrains Mono', monospace;
  }
  .tabBtn.active .badge-count{background: hsl(var(--accent)/.18); color: hsl(var(--accent))}

  /* ===== §6 面板 + 卡片 ===== */
  .panel{
    background: hsl(var(--surface));
    border: 1px solid hsl(var(--border));
    border-radius: var(--radius);
    padding: 18px 20px;
    margin-bottom: 18px;
    transition: border-color .15s var(--ease-smooth);
  }
  .panel:hover{border-color: hsl(var(--border)/1.2)}
  .panel h3{
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: hsl(var(--fg-secondary));
    margin: 0 0 14px;
    display: flex; align-items: center; gap: 6px;
  }
  .panel h3::before{
    content: '';
    width: 3px; height: 14px;
    background: linear-gradient(180deg, hsl(var(--accent)) 0%, hsl(var(--accent)/.3) 100%);
    border-radius: 2px;
  }
  .panel h3 small{
    font-weight: 400;
    text-transform: none;
    letter-spacing: 0;
    font-size: 11px;
    color: hsl(var(--fg-muted));
  }

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:14px}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:900px){.row2{grid-template-columns:1fr}}

  .card{
    background: hsl(var(--elevated)/.85);
    border: 1px solid hsl(var(--border));
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    transition: all .15s var(--ease-smooth);
    position: relative;
    overflow: hidden;
  }
  .card:hover{
    border-color: hsl(var(--accent)/.5);
    background: hsl(var(--elevated));
    transform: translateY(-1px);
  }
  .card::before{
    content:''; position:absolute; left:0; top:0; bottom:0; width:2px;
    background: hsl(var(--fg-muted));
    opacity: .35;
  }
  .card.bull::before{background: hsl(var(--bull)); opacity:.65}
  .card.bear::before{background: hsl(var(--bear)); opacity:.65}
  .card.warn::before{background: hsl(var(--warn)); opacity:.65}
  .card.accent::before{background: hsl(var(--accent)); opacity:.75}
  .card .lbl{
    font-size: 11px;
    color: hsl(var(--fg-muted));
    margin-bottom: 6px;
    display: flex; align-items: center; gap: 4px;
  }
  .card .val{
    font-size: 22px;
    font-weight: 700;
    line-height: 1.1;
    letter-spacing: -0.3px;
  }
  .card .hint{
    font-size: 10px;
    color: hsl(var(--fg-muted));
    margin-top: 4px;
    font-family: 'JetBrains Mono', monospace;
  }
  .up, .bull, .text-bull {color: hsl(var(--bull))}
  .down, .bear, .text-bear {color: hsl(var(--bear))}
  .warn, .text-warn {color: hsl(var(--warn))}
  .accent, .text-accent {color: hsl(var(--accent))}
  .muted, .text-muted {color: hsl(var(--fg-muted))}

  /* ===== §7 表格 ===== */
  .tbl{width:100%; border-collapse:separate; border-spacing:0; font-size:12.5px}
  .tbl thead th{
    text-align: left;
    color: hsl(var(--fg-muted));
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 10.5px;
    padding: 9px 12px;
    border-bottom: 1px solid hsl(var(--border));
    background: hsl(var(--elevated)/.4);
    position: sticky; top: 0;
    z-index: 1;
  }
  .tbl tbody td{
    padding: 9px 12px;
    border-bottom: 1px solid hsl(var(--border)/.6);
    font-variant-numeric: tabular-nums;
    color: hsl(var(--fg-primary));
  }
  .tbl tbody tr:hover td{background: hsl(var(--accent)/.05)}
  .tbl tbody tr:last-child td{border-bottom:0}
  .tbl td.num,.tbl td.mono,.tbl code{font-family:'JetBrains Mono',monospace}
  .tbl td.r{text-align:right}
  .tbl .empty{color: hsl(var(--fg-muted)); text-align:center; padding:24px 0}

  /* ===== §8 状态徽章 (P0/P1/P2/P3/OK) ===== */
  .lvl{display:inline-flex; align-items:center; gap:4px;
       padding: 2px 9px; border-radius: 99px;
       font-size: 10.5px; font-weight: 600; line-height: 1.4;
       letter-spacing: 0.04em}
  .lvl::before{content:''; width:6px; height:6px; border-radius:99px; background:currentColor}
  .lvl.P0{color: hsl(var(--p0)); background: hsl(var(--p0)/.12); border:1px solid hsl(var(--p0)/.4)}
  .lvl.P1{color: hsl(var(--p1)); background: hsl(var(--p1)/.12); border:1px solid hsl(var(--p1)/.4)}
  .lvl.P2{color: hsl(var(--p2)); background: hsl(var(--p2)/.12); border:1px solid hsl(var(--p2)/.4)}
  .lvl.P3{color: hsl(var(--p3)); background: hsl(var(--p3)/.12); border:1px solid hsl(var(--p3)/.4)}
  .lvl.OK{color: hsl(var(--bear)); background: hsl(var(--bear)/.12); border:1px solid hsl(var(--bear)/.4)}
  .lvl.ERR{color: hsl(var(--danger)); background: hsl(var(--danger)/.12); border:1px solid hsl(var(--danger)/.4)}
  .lvl.WARN{color: hsl(var(--warn)); background: hsl(var(--warn)/.12); border:1px solid hsl(var(--warn)/.4)}

  /* 通用 chip */
  .chip{display:inline-flex; align-items:center; gap:4px;
         padding: 2px 8px; border-radius: 4px;
         font-size: 10.5px; font-weight: 500; line-height: 1.4;
         color: hsl(var(--fg-secondary)); background: hsl(var(--elevated));
         border: 1px solid hsl(var(--border))}
  .chip.bull{color: hsl(var(--bull)); background: hsl(var(--bull)/.1); border-color: hsl(var(--bull)/.35)}
  .chip.bear{color: hsl(var(--bear)); background: hsl(var(--bear)/.1); border-color: hsl(var(--bear)/.35)}
  .chip.accent{color: hsl(var(--accent)); background: hsl(var(--accent)/.1); border-color: hsl(var(--accent)/.35)}
  .chip.warn{color: hsl(var(--warn)); background: hsl(var(--warn)/.1); border-color: hsl(var(--warn)/.35)}

  /* 进度条 */
  .prog{height: 6px; background: hsl(var(--border)); border-radius: 99px; overflow: hidden; position: relative}
  .prog > i{display: block; height: 100%; background: linear-gradient(90deg, hsl(var(--accent)), hsl(var(--bull)));
            border-radius: 99px; transition: width .5s var(--ease-smooth)}
  .prog.bull > i{background: linear-gradient(90deg, hsl(var(--bull)), hsl(var(--warn)))}
  .prog.bear > i{background: linear-gradient(90deg, hsl(var(--bear)), hsl(var(--accent)))}

  /* 涨幅条 (宽度比) */
  .eq-bar{display:flex; height:18px; border-radius: 6px; overflow:hidden;
          font-size:11px; font-weight:600; line-height:1; margin: 8px 0}
  .eq-bar > div{display:flex; align-items:center; justify-content:center; min-width:0; overflow:hidden; white-space:nowrap}
  .eq-bar .up{background: hsl(var(--bull))}
  .eq-bar .flat{background: hsl(var(--fg-muted))}
  .eq-bar .dn{background: hsl(var(--bear))}

  /* 空状态 */
  .empty-state{
    display: grid; place-items: center;
    padding: 36px 16px;
    color: hsl(var(--fg-muted));
    font-size: 12.5px;
    text-align: center;
  }
  .empty-state .ic{
    width: 36px; height: 36px;
    border-radius: 99px;
    background: hsl(var(--elevated));
    display: grid; place-items: center;
    margin: 0 auto 8px;
    color: hsl(var(--fg-muted));
    font-size: 18px;
  }

  /* 标签 chip */
  .tag{display:inline-block; padding:1px 7px; border-radius: 4px; font-size:10.5px; font-weight:500; margin-right:6px}
  .tag.buy{background: hsl(var(--bull)/.18); color: hsl(var(--bull))}
  .tag.sell{background: hsl(var(--bear)/.18); color: hsl(var(--bear))}

  /* 操作按钮 */
  .btn{
    display: inline-flex; align-items: center; gap: 5px;
    background: hsl(var(--elevated));
    border: 1px solid hsl(var(--border));
    color: hsl(var(--fg-primary));
    padding: 6px 12px; border-radius: var(--radius-sm);
    font-size: 12px; font-weight: 500;
    cursor: pointer;
    transition: all .15s var(--ease-smooth);
    line-height: 1;
  }
  .btn:hover{background: hsl(var(--surface)); border-color: hsl(var(--accent)/.5); color: hsl(var(--accent))}
  .btn.primary{background: hsl(var(--accent)); border-color: hsl(var(--accent)); }
  .btn.primary:hover{background: hsl(var(--accent)/.85); }
  .btn.danger{color: hsl(var(--danger)); border-color: hsl(var(--danger)/.4)}
  .btn.danger:hover{background: hsl(var(--danger)/.1)}
  .btn input[type=date],.btn input,.btn select{
    background: hsl(var(--surface)); color: hsl(var(--fg-primary));
    border: 1px solid hsl(var(--border));
    padding: 4px 8px; border-radius: var(--radius-sm);
    font-size: 12px;
  }
  .btn input[type=date]:focus,.btn input:focus,.btn select:focus{outline:none; border-color: hsl(var(--accent))}

  /* ===== §9 tabView 切换 ===== */
  .tabView{display:none; animation: fadein .25s var(--ease-smooth)}
  .tabView.active{display:block}
  @keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
  footer{color:hsl(var(--fg-muted));font-size:11.5px;text-align:center;padding:20px 0 30px;border-top:1px solid hsl(var(--border));margin-top:30px}
  .rulesGrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}

  /* 暗色表单元素 (替代内联 background:#0f1420 等) */
  .input-dark{
    background: hsl(var(--base));
    color: hsl(var(--fg-primary));
    border: 1px solid hsl(var(--border));
    padding: 5px 8px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    font-family: inherit;
    transition: border-color .15s var(--ease-smooth);
  }
  .input-dark:focus{outline:none; border-color: hsl(var(--accent)); box-shadow: 0 0 0 3px hsl(var(--accent)/.15)}
  .input-dark::placeholder{color: hsl(var(--fg-muted))}
  .pgbtn{
    background: hsl(var(--base));
    color: hsl(var(--fg-primary));
    border: 1px solid hsl(var(--border));
    padding: 2px 8px;
    border-radius: var(--radius-sm);
    font-size: 12px;
    line-height: 1.5;
    cursor: pointer;
    transition: border-color .15s var(--ease-smooth), background .15s var(--ease-smooth);
  }
  .pgbtn:hover:not(:disabled){border-color: hsl(var(--accent)); background: hsl(var(--accent)/.08)}
  .pgbtn:disabled{opacity:.38; cursor: not-allowed}
  /* ===== §10 Legacy 兼容层 (老样式 -> 新设计变量) ===== */
  /* 老代码里使用 var(--txt|--muted|--up|--dn|--panel|--line) — 这里桥接到新设计变量 */
  .sentiBar{display:flex;align-items:flex-end;gap:5px;height:110px;margin-top:10px}
  .sentiBar>div{flex:1;background:linear-gradient(180deg,hsl(var(--acc2)/.7),hsl(var(--acc2)/.12));border-radius:4px 4px 0 0;position:relative;min-width:16px;transition:height .5s}
  .sentiBar>div.hot{background:linear-gradient(180deg,hsl(var(--bull)/.75),hsl(var(--bull)/.15))}
  .sentiBar>div i{position:absolute;bottom:-18px;left:0;right:0;text-align:center;font-size:10px;font-style:normal;color:var(--muted)}
  .sentiBar>div em{position:absolute;top:-16px;left:0;right:0;text-align:center;font-size:10px;font-style:normal;color:var(--muted)}
  .donutRow{display:flex;align-items:center;gap:22px;flex-wrap:wrap}
  .donut{width:130px;height:130px;border-radius:50%;position:relative;flex:0 0 auto}
  .donut center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .bigNum{font-size:22px;font-weight:700}
  .grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:12px}
  .metricGrp{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:18px}
  .miniCard{background:hsl(var(--elevated)/.6);border:1px solid hsl(var(--border));border-radius:10px;padding:12px 14px;transition:border-color .15s var(--ease-smooth)}
  .miniCard:hover{border-color: hsl(var(--accent)/.4)}
  .miniCard .k{color:var(--muted);font-size:11px}
  .miniCard .v{font-size:20px;font-weight:700;margin-top:4px;color:var(--txt)}
  .attrsRow{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:14px}
  .attrItem{background:hsl(var(--elevated)/.6);border:1px solid hsl(var(--border));border-radius:10px;padding:12px 14px;transition:border-color .15s var(--ease-smooth)}
  .attrItem:hover{border-color: hsl(var(--accent)/.4)}
  .attrItem .sym{font-weight:600;font-size:14px;margin-bottom:6px;color:var(--txt)}
  .attrItem .row{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:3px}
  .attrItem .row b{color:var(--txt);font-variant-numeric:tabular-nums}
  .perfEquity{display:flex;align-items:flex-end;gap:4px;height:130px;margin:14px 0 6px}
  .perfEquity>div{flex:1;min-width:10px;background:linear-gradient(180deg,hsl(var(--accent)/.75),hsl(var(--accent)/.12));border-radius:4px 4px 0 0;position:relative}
  .perfEquity>div i{position:absolute;bottom:-16px;left:0;right:0;font-size:9px;text-align:center;font-style:normal;color:var(--muted)}
  .legendDot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle}
  .ruleItem{background:hsl(var(--elevated)/.5);border:1px solid hsl(var(--border));border-radius:10px;padding:10px 12px;transition:border-color .15s var(--ease-smooth)}
  .ruleItem:hover{border-color: hsl(var(--accent)/.4)}
  .ruleItem .k{color:var(--muted);font-size:11px;display:flex;align-items:center;gap:6px}
  .ruleItem .v{font-size:15px;font-weight:600;margin-top:4px;color:var(--txt)}
  .logBox{background:hsl(var(--base));border:1px solid hsl(var(--border));border-radius:10px;padding:12px;height:260px;overflow-y:auto;font-family:'JetBrains Mono','Cascadia Mono','Consolas',monospace;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-all}
  .logLine{color:var(--muted)}
  .logLine .ts{color:hsl(var(--fg-muted)/.7);margin-right:6px}
  .logLine .tag-daemon{color:hsl(var(--accent))}
  .logLine .tag-daily{color:hsl(var(--acc2))}
  .logLine .tag-sub{color:hsl(var(--fg-secondary))}
  .logLine.err{color:hsl(var(--bull))}
  .btBars{display:flex;align-items:flex-end;gap:4px;height:90px;margin-top:10px}
  .btBars>div{flex:1;background:linear-gradient(180deg,hsl(var(--accent)/.7),hsl(var(--accent)/.15));border-radius:4px 4px 0 0;position:relative;min-width:14px}
  .btBars>div i{position:absolute;bottom:-18px;left:0;right:0;text-align:center;font-size:10px;font-style:normal;color:var(--muted)}
  .sBadge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;line-height:1.4}
  .sBadge.ok{background:hsl(var(--acc2)/.12);color:hsl(var(--acc2));border:1px solid hsl(var(--acc2)/.35)}
  .sBadge.warn{background:hsl(var(--warn)/.12);color:hsl(var(--warn));border:1px solid hsl(var(--warn)/.35)}
  .sBadge.bad{background:hsl(var(--bull)/.12);color:hsl(var(--bull));border:1px solid hsl(var(--bull)/.35)}
  .sBadge.alpha{background:hsl(var(--accent)/.18);color:hsl(var(--accent));border:1px solid hsl(var(--accent)/.35)}
</style>
</head>
<body>

  <header class="topbar">
    <div class="brand">
      <div class="logo">Q</div>
      <div>
        <h1>全A轮动 · 盘中模拟盘 <span class="sub">Tick Stock Panel · 实时投研终端</span></h1>
      </div>
    </div>
    <div class="badges">
      <span class="badge" id="bDay"><span class="dot"></span>—</span>
      <span class="badge" id="bMode"><span class="dot"></span>—</span>
      <span class="badge" id="bSrc"><span class="dot"></span>—</span>
      <span class="badge" id="bTick"><span class="dot"></span>—</span>
      <span class="badge" id="sub"><span class="dot"></span>—</span>
    </div>
  </header>

  <div class="wrap">

    <div class="tabs">
      <button class="tabBtn active" data-tab="overview"><span class="ico">◎</span>概览</button>
      <button class="tabBtn" data-tab="dashboard"><span class="ico">▦</span>市场看板<span class="badge-count" id="cnt-dashboard">·</span></button>
      <button class="tabBtn" data-tab="market"><span class="ico">≋</span>市场情绪<span class="badge-count" id="cnt-market">·</span></button>
      <button class="tabBtn" data-tab="deep"><span class="ico">◈</span>深度分析<span class="badge-count" id="cnt-deep">·</span></button>
      <button class="tabBtn" data-tab="riskview"><span class="ico">⚠</span>风控告警<span class="badge-count" id="cnt-riskview">·</span></button>
      <button class="tabBtn" data-tab="flab"><span class="ico">∷</span>因子实验室<span class="badge-count" id="cnt-flab">·</span></button>
      <button class="tabBtn" data-tab="pscan"><span class="ico">⌬</span>参数扫描<span class="badge-count" id="cnt-pscan">·</span></button>
      <button class="tabBtn" data-tab="perf"><span class="ico">⌬</span>绩效归因<span class="badge-count" id="cnt-perf">·</span></button>
      <button class="tabBtn" data-tab="backtest"><span class="ico">↻</span>回测<span class="badge-count" id="cnt-backtest">·</span></button>
      <button class="tabBtn" data-tab="concept"><span class="ico">◇</span>概念分析<span class="badge-count" id="cnt-concept">·</span></button>
      <button class="tabBtn" data-tab="industry"><span class="ico">▤</span>行业分析<span class="badge-count" id="cnt-industry">·</span></button>
      <button class="tabBtn" data-tab="monitor"><span class="ico">◉</span>监控中心<span class="badge-count" id="cnt-monitor">·</span></button>
      <button class="tabBtn" data-tab="regime"><span class="ico">∿</span>市场环境<span class="badge-count" id="cnt-regime">·</span></button>
      <button class="tabBtn" data-tab="abnormal"><span class="ico">⚡</span>异动监控<span class="badge-count" id="cnt-abnormal">·</span></button>
      <button class="tabBtn" data-tab="dbpanel"><span class="ico">▥</span>数据板块<span class="badge-count" id="cnt-dbpanel">·</span></button>
    </div>

  <!-- ===================== 市场看板 (Dashboard) ===================== -->
  <div id="view-dashboard" class="tabView">
    <div class="panel">
      <h3>KPI 总览 <span class="muted" style="font-weight:400;font-size:12px">基于 DuckDB daily_bars 最新日</span></h3>
      <div id="mbKpi"><div class="muted">加载中...</div></div>
    </div>
    <div class="row2">
      <div class="panel">
        <h3>主流指数</h3>
        <div id="mbIndices"><div class="muted">加载中...</div></div>
      </div>
      <div class="panel">
        <h3>市场情绪雷达</h3>
        <div id="mbRadar"><div class="muted">加载中...</div></div>
      </div>
    </div>
    <div class="panel">
      <h3>市场宽度 <span class="muted" style="font-weight:400;font-size:12px">涨/跌/平 + 涨停/跌停 + 宽度比</span></h3>
      <div id="mbBreadth"><div class="muted">加载中...</div></div>
    </div>
    <div class="panel">
      <h3>涨跌幅分布 <span class="muted" style="font-weight:400;font-size:12px">10 段: <-9.5% ~ ≥9.5%</span></h3>
      <div id="mbDist"><div class="muted">加载中...</div></div>
    </div>
    <div class="row2">
      <div class="panel">
        <h3>连板梯队</h3>
        <div id="mbLadder"><div class="muted">加载中...</div></div>
      </div>
      <div class="panel">
        <h3>北向资金 / 活跃 Top</h3>
        <div id="mbActive"><div class="muted">加载中...</div></div>
      </div>
    </div>
  </div><!-- /view-dashboard -->

  <div id="view-overview" class="tabView active">
  <section class="grid" id="cards"></section>

  <div class="row2">
    <div class="panel">
      <h3>系统健康 · 门控 <span class="muted" style="font-weight:400;font-size:12px">进程 / 引擎心跳 / 数据水位 / IC门控档位</span></h3>
      <div id="sysGate"><div class="muted">加载中...</div></div>
    </div>
    <div class="panel">
      <h3>风控事件流 <span class="muted" style="font-weight:400;font-size:12px">门控 / 熔断 / 止损 / 引擎异常 · 最近</span></h3>
      <div id="riskEvents"><div class="muted">加载中...</div></div>
    </div>
  </div>

  <div class="panel" id="rulesPanel">
    <h3>交易规则面板 <span class="muted" style="font-weight:400;font-size:12px">(实时生效)</span></h3>
    <div class="rulesGrid" id="rulesBody"><span class="muted">—</span></div>
  </div>

  <div class="row2">
    <div class="panel">
      <h3>持仓明细</h3>
      <table class="tbl">
        <thead><tr><th>名称/代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏%</th><th>仓位</th><th>可卖/T+1</th><th>距涨停/距跌停</th><th>状态</th></tr></thead>
        <tbody id="posBody"><tr><td colspan="10" class="empty">暂无数据</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        成交历史
        <select id="tradeDateFilter" class="input-dark" style="font-size:12px;padding:2px 6px">
          <option value="all">全部</option>
        </select>
        <span class="muted" style="font-weight:400;font-size:12px" id="tradeCount"></span>
        <span id="tradePager" style="display:flex;align-items:center;gap:6px;margin-left:auto"></span>
      </h3>
      <table class="tbl">
        <thead><tr><th>日期</th><th>时间</th><th>方向</th><th>代码</th><th>数量</th><th>价格</th><th>费用</th><th>盈亏</th></tr></thead>
        <tbody id="tradeBody"><tr><td colspan="8" class="empty">无成交</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <h3>目标池 (选股结果) <span id="middayTag" style="font-weight:400;font-size:12px"></span></h3>
    <table class="tbl">
      <thead><tr><th>代码</th><th>名称</th><th>综合分</th><th>信号</th></tr></thead>
      <tbody id="targetBody"><tr><td colspan="4" class="empty">—</td></tr></tbody>
    </table>
  </div>

  <div class="panel" id="logPanel">
    <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">实时运行日志
      <span class="sBadge ok" id="logDot" style="font-size:10px">● 守护在线</span>
      <span class="muted" style="font-weight:400;font-size:12px">(引擎/收盘/守护日志实时滚动 · 每3秒刷新)</span>
    </h3>
    <div id="logBox" class="logBox"><span class="muted">日志加载中…</span></div>
  </div>

  <div class="panel" id="btPanel">
    <h3>连续交易日回放 <span class="muted" style="font-weight:400;font-size:12px" id="btRange"></span></h3>
    <div id="btBody"><span class="empty">未运行回放</span></div>
  </div>

  </div><!-- /view-overview -->

  <div id="view-market" class="tabView">
    <div class="panel">
      <h3>市场情绪趋势 <span class="muted" style="font-weight:400;font-size:12px" id="mkRange"></span></h3>
      <div class="sentiBar" id="sentiBar"></div>
      <div class="muted" style="margin-top:26px;font-size:12px">情绪分: 上涨占比+中位数涨幅+强上涨占比+涨停家数 加权合成 (0~100)。点击 tab 后自动加载, 每30秒刷新。</div>
    </div>
    <div class="grid3" id="mkCards"></div>
    <div class="row2">
      <div class="panel">
        <h3>涨跌分布 (最新日)</h3>
        <div class="donutRow">
          <div class="donut" id="upDonut"></div>
          <div id="upLegend" class="muted" style="font-size:13px"></div>
        </div>
      </div>
      <div class="panel">
        <h3>板块成交分布 (亿) <span class="muted" style="font-weight:400;font-size:12px" id="secDay"></span></h3>
        <div id="secBody" class="muted" style="font-size:13px"></div>
      </div>
    </div>
  </div><!-- /view-market -->

  <div id="view-perf" class="tabView">
    <div class="panel" id="pfCommentaryPanel">
      <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        LLM 盘后点评
        <span class="sBadge alpha" id="pfAiBadge" style="font-size:10px">MiniMax-M3</span>
        <span class="muted" id="pfAiMeta" style="font-weight:400;font-size:12px"></span>
      </h3>
      <div id="pfCommentary"><div class="muted">等待 LLM 点评 (盘后 run_daily 自动生成)...</div></div>
    </div>
    <div class="panel">
      <h3>绩效指标 <span class="muted" style="font-weight:400;font-size:12px" id="pfRange"></span></h3>
      <div class="grid" id="pfMetrics"></div>
    </div>
    <div class="panel">
      <h3>组合净值曲线 <span class="muted" style="font-weight:400;font-size:12px">(逐日回执权益)</span></h3>
      <div class="perfEquity" id="pfEquity"></div>
    </div>
    <div class="panel">
      <h3>持仓归因 (最新日, 按浮盈排序)</h3>
      <table class="tbl">
        <thead><tr><th>代码</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>权重</th><th>浮盈</th><th>浮盈%</th></tr></thead>
        <tbody id="pfAttr"><tr><td colspan="8" class="empty">暂无持仓</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <h3>因子 IC 监控 <span class="muted" style="font-weight:400;font-size:12px">(H20 前瞻, 联动 ic_curve)</span></h3>
      <table class="tbl">
        <thead><tr><th>因子</th><th>样本数</th><th>IC均值</th><th>ICIR</th><th>近20日IC均值</th></tr></thead>
        <tbody id="pfIc"><tr><td colspan="5" class="empty">无 IC 数据</td></tr></tbody>
      </table>
    </div>
    <div class="panel">
      <h3>因子权重自适应 <span class="muted" style="font-weight:400;font-size:12px">(D反馈→B迭代: ICIR 强度驱动 alpha 再平衡)</span></h3>
      <div class="grid" id="pfWeights"></div>
      <div id="pfWmeta" class="muted" style="font-size:12px;margin-top:8px"></div>
    </div>
    <div class="panel">
      <h3 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        DRL 微调
        <span class="sBadge alpha" style="font-size:10px">LLM brief + PPO</span>
        <span class="muted" style="font-weight:400;font-size:12px">(LLM 市场研判 + 情绪因子注入 obs / stance 调节扰动 / factor_recommendations 先验权重)</span>
      </h3>
      <div id="pfDrl"><div class="muted">DRL 训练历史加载中...</div></div>
    </div>
    <div class="panel">
      <h3>
        策略退化 + SPC 过程控制
        <span class="chip accent" style="margin-left:4px">LLM+P3</span>
        <small>(退化指数 / Shewhart 控制限 / CUSUM 漂移 / 增量学习闭环)</small>
      </h3>
      <div id="pfDegradation"><div class="muted">退化监控加载中...</div></div>
    </div>

    <!-- DRL 目标计划 (盘中 realtime_engine 优先消费) -->
    <div class="panel">
      <h3>DRL 目标计划 <span class="muted" style="font-weight:400;font-size:12px">盘中引擎优先消费, 缺失回退 selection.json</span></h3>
      <div id="pfTargetPlan"><div class="muted">加载中...</div></div>
    </div>

    <!-- 盘前健康检查 -->
    <div class="panel">
      <h3>盘前健康检查 <span class="muted" style="font-weight:400;font-size:12px">磁盘 / DuckDB / ArcticDB / 行情 / 订单簿 / state_io</span></h3>
      <div id="pfHealth"><div class="muted">加载中...</div></div>
    </div>

    <!-- 因子物化视图 -->
    <div class="panel">
      <h3>因子物化视图 <span class="muted" style="font-weight:400;font-size:12px">DuckDB 物化表 + Parquet 导出</span></h3>
      <div id="pfViews"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-perf -->

  <!-- ===================== 回测 ===================== -->
  <div id="view-backtest" class="tabView">
    <div class="panel">
      <h3>回测历史 <span class="muted" style="font-weight:400;font-size:12px">所有 backtest_latest.json + data/backtest/*</span></h3>
      <div id="btHistoryBody"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-backtest -->

  <!-- ===================== 概念分析 ===================== -->
  <div id="view-concept" class="tabView">
    <div class="panel">
      <h3>概念/题材分析 <span class="muted" style="font-weight:400;font-size:12px">数据源: <code style="color:#fff">ext_gn_ths</code> (同花顺概念, 5559 标的) + 当日行情聚合</span></h3>
      <div id="conceptCtrl" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;align-items:center">
        <input id="conceptSearch" placeholder="搜索概念名 (例: 人形机器人)" style="flex:1;padding:5px 8px;border:1px solid #334;border-radius:6px;background:#0f1420;color:#fff"/>
        <select id="conceptSort" class="input-dark" style="padding:5px 8px">
          <option value="strength">综合热度</option>
          <option value="change_pct">平均涨幅</option>
          <option value="amount">总成交额</option>
          <option value="turnover">平均换手</option>
          <option value="count">成分数</option>
        </select>
        <select id="conceptTop" class="input-dark" style="padding:5px 8px">
          <option value="30">top 30</option>
          <option value="50" selected>top 50</option>
          <option value="100">top 100</option>
          <option value="200">top 200</option>
        </select>
        <button class="tabBtn" id="conceptRefresh">刷新</button>
      </div>
      <div id="conceptBody"><div class="muted">加载中...</div></div>
      <div id="conceptDetail" style="margin-top:10px"></div>
    </div>
  </div><!-- /view-concept -->

  <!-- ===================== 行业分析 ===================== -->
  <div id="view-industry" class="tabView">
    <div class="panel">
      <h3>行业分析 <span class="muted" style="font-weight:400;font-size:12px">数据源: <code style="color:#fff">ext_hy_ths</code> (同花顺行业三级分类, 5559 标的) + 当日行情聚合</span></h3>
      <div id="industryCtrl" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;align-items:center">
        <input id="industrySearch" placeholder="搜索行业名 (例: 汽车-汽车零部件)" style="flex:1;padding:5px 8px;border:1px solid #334;border-radius:6px;background:#0f1420;color:#fff"/>
        <select id="industrySort" class="input-dark" style="padding:5px 8px">
          <option value="strength">综合热度</option>
          <option value="change_pct">平均涨幅</option>
          <option value="amount">总成交额</option>
          <option value="turnover">平均换手</option>
          <option value="count">成分数</option>
        </select>
        <select id="industryTop" class="input-dark" style="padding:5px 8px">
          <option value="30">top 30</option>
          <option value="50" selected>top 50</option>
          <option value="100">top 100</option>
          <option value="200">top 200</option>
        </select>
        <button class="tabBtn" id="industryRefresh">刷新</button>
      </div>
      <div id="industryBody"><div class="muted">加载中...</div></div>
      <div id="industryDetail" style="margin-top:10px"></div>
    </div>
  </div><!-- /view-industry -->

  <!-- ===================== 监控中心 ===================== -->
  <div id="view-monitor" class="tabView">
    <div class="panel">
      <h3>监控中心 <span class="muted" style="font-weight:400;font-size:12px">盘前健康 + 退化 + SPC + DRL plan ready</span></h3>
      <div id="monitorBody"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-monitor -->

  <!-- ===================== 市场环境 ===================== -->
  <div id="view-regime" class="tabView">
    <div class="panel">
      <h3>市场环境 <span class="muted" style="font-weight:400;font-size:12px">市场宽度 + 情绪分位 + 阶段判别</span></h3>
      <div id="regimeBody"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-regime -->

  <!-- ===================== 异动监控 ===================== -->
  <div id="view-abnormal" class="tabView">
    <div class="panel">
      <h3>异动监控 <span class="muted" style="font-weight:400;font-size:12px">涨幅/跌幅/成交额 Top, 涨停跌停统计</span></h3>
      <div id="abnormalBody"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-abnormal -->

  <!-- ===================== 数据板块 ===================== -->
  <div id="view-deep" class="tabView">
    <div class="panel">
      <h3 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        组合净值 · 回撤
        <span class="muted" style="font-weight:400;font-size:12px">(逐日回执权益; 悬停查看十字值)</span>
        <span id="eqRange" class="btn" style="gap:0;padding:2px">
          <button class="btn" data-r="all" style="padding:3px 9px">全部</button>
          <button class="btn" data-r="120" style="padding:3px 9px">120日</button>
          <button class="btn" data-r="60" style="padding:3px 9px">60日</button>
          <button class="btn" data-r="20" style="padding:3px 9px">20日</button>
        </span>
      </h3>
      <canvas id="eqCanvas" width="1200" height="330" style="width:100%;background:hsl(var(--base));border-radius:var(--radius-sm)"></canvas>
      <div id="eqTip" class="muted" style="font-size:12px;min-height:16px;margin-top:4px"></div>
    </div>

    <div class="panel">
      <h3 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        K线 · 技术指标
        <span class="muted" style="font-weight:400;font-size:12px">(h5i 日线; MA/MACD/RSI; ◀ ▶ 平移 · 放大/缩小 · 悬停十字)</span>
        <select id="kSymSel" class="input-dark" style="font-size:12px;padding:2px 6px"></select>
        <input id="kSymInput" class="input-dark" placeholder="代码 如 600519" style="width:120px;font-size:12px">
        <button id="kGo" class="btn" style="padding:3px 9px">加载</button>
        <select id="kInd" class="input-dark" style="font-size:12px;padding:2px 6px">
          <option value="macd">MACD</option><option value="rsi">RSI</option>
        </select>
        <span style="margin-left:auto"></span>
        <button id="kPrev" class="btn" style="padding:3px 9px">◀ 更早</button>
        <button id="kNext" class="btn" style="padding:3px 9px">更新 ▶</button>
        <button id="kZoomIn" class="btn" style="padding:3px 9px">+</button>
        <button id="kZoomOut" class="btn" style="padding:3px 9px">−</button>
      </h3>
      <canvas id="kCanvas" width="1200" height="470" style="width:100%;background:hsl(var(--base));border-radius:var(--radius-sm)"></canvas>
      <div id="kTip" class="muted" style="font-size:12px;min-height:16px;margin-top:4px"></div>
    </div>

    <div class="row2">
      <div class="panel">
        <h3>月度收益热力图 <span class="muted" style="font-weight:400;font-size:12px">(月末回执环比)</span></h3>
        <div id="mHeat"><div class="muted">加载中...</div></div>
      </div>
      <div class="panel">
        <h3>持仓分布 · 盈亏贡献 <span class="muted" style="font-weight:400;font-size:12px">(市值占比 / 浮盈贡献)</span></h3>
        <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
          <div id="holdPie" style="flex:1;min-width:230px"><div class="muted">加载中...</div></div>
          <div id="holdBar" style="flex:1;min-width:200px"><div class="muted">加载中...</div></div>
        </div>
      </div>
    </div>

    <div class="row2">
      <div class="panel">
        <h3>持仓行业分布 <span class="muted" style="font-weight:400;font-size:12px">(同花顺一级行业 · 按持仓市值)</span></h3>
        <div id="holdInd"><div class="muted">加载中...</div></div>
      </div>
      <div class="panel">
        <h3>持仓市值分布 <span class="muted" style="font-weight:400;font-size:12px">(公司总市值: 小盘&lt;100 / 中盘100-500 / 大盘≥500亿)</span></h3>
        <div id="holdCap"><div class="muted">加载中...</div></div>
      </div>
    </div>

    <div class="panel" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <h3 style="margin:0">一键复盘报告</h3>
      <span class="muted" style="font-size:12px">聚合当前持仓/净值/绩效/门控/因子 为 Markdown, 便于复盘与分享</span>
      <button id="btnExport" class="btn primary" style="margin-left:auto">导出复盘报告 (.md)</button>
      <button id="btnPrintPDF" class="btn">导出 PDF (打印)</button>
    </div>
  </div>

  <div id="view-riskview" class="tabView">
    <div class="panel">
      <h3>风险 · 绩效指标 <span class="muted" style="font-weight:400;font-size:12px">VaR / 单日盈亏 / 滚动夏普 / Sortino / 胜率盈亏比 / 运维计数 (回执日收益口径)</span></h3>
      <div class="grid" id="riskKpis" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))"><div class="muted">加载中...</div></div>
      <div id="riskNote" class="muted" style="font-size:12px;margin-top:6px"></div>
    </div>
    <div class="row2">
      <div class="panel">
        <h3>告警中心 <span class="muted" style="font-weight:400;font-size:12px">门控 / 进程 / 心跳 / 数据滞后 / 回撤 / 单日亏损 · 15s 刷新</span></h3>
        <div id="alertBox"><div class="muted">加载中...</div></div>
      </div>
      <div class="panel">
        <h3>告警推送 <span class="muted" style="font-weight:400;font-size:12px">可选扩展(当前无需外部推送, 不配置即可正常使用)</span></h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <select id="pushChannel" class="input-dark"><option value="dingtalk">钉钉</option><option value="email">邮件</option></select>
          <button id="btnPushTest" class="btn">发送测试告警</button>
          <button id="btnPushNow" class="btn primary">推送当前告警</button>
        </div>
        <div id="pushResult" class="muted" style="font-size:12px;margin-top:8px"></div>
        <div class="muted" style="font-size:11px;margin-top:6px">如需启用: 钉钉 <code>DINGTALK_WEBHOOK</code>; 邮件 <code>SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO</code>(均环境变量)</div>
      </div>
    </div>
  </div>

  <div id="view-flab" class="tabView">
    <div class="panel">
      <h3 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        因子 IC 时序
        <span class="muted" style="font-weight:400;font-size:12px">(2013~ 真实历史 IC · 悬停查看 · 点击因子隐藏/显示)</span>
        <select id="flabHorizon" class="input-dark" style="font-size:12px;padding:2px 6px">
          <option value="h5">5日IC</option><option value="h1">1日IC</option><option value="h3">3日IC</option>
          <option value="h10">10日IC</option><option value="h20">20日IC</option>
        </select>
        <select id="flabRange" class="input-dark" style="font-size:12px;padding:2px 6px">
          <option value="120">近120日</option><option value="300">近300日</option><option value="500">近500日</option>
        </select>
      </h3>
      <canvas id="flabCanvas" width="1200" height="360" style="width:100%;background:hsl(var(--base));border-radius:var(--radius-sm)"></canvas>
      <div id="flabTip" class="muted" style="font-size:12px;min-height:16px;margin-top:4px"></div>
      <div id="flabStats" style="display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:6px"></div>
    </div>
    <div class="panel">
      <h3>因子五分位 · 最新截面 <span class="muted" style="font-weight:400;font-size:12px">(v_factor_scores_daily 最新日 5 等分箱均值/区间 · 截面分布, 非未来收益)</span></h3>
      <div id="flabQ"><div class="muted">加载中...</div></div>
    </div>
  </div>

  <div id="view-dbpanel" class="tabView">
    <div class="panel">
      <h3>本地数据库表头情况 <span class="muted" style="font-weight:400;font-size:12px">DuckDB + ArcticDB 全部表</span></h3>
      <div id="dbpanelBody"><div class="muted">加载中...</div></div>
    </div>
  </div><!-- /view-dbpanel -->

  <footer>全A轮动模拟盘 · 只做多 / T+1 / 涨停不可买 跌停不可卖 停牌跳过 滑点万分5 · 数据源 AKShare+DuckDB · 每3秒刷新</footer>
</div>

<script>
const $ = s => document.querySelector(s);
function fmt(n, d=2){ if(n==null||isNaN(n)) return '—'; return Number(n).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}); }
function pnlCls(v){ return v>0?'up':(v<0?'down':''); }
function esc(s){ return (s==null?'':String(s)); }

function renderCards(s){
  const C = s.capital||{};
  const cap = [
    {label:'总权益', v:fmt(C.equity), h:'初始 '+fmt(C.init_capital)+' 元', cls:(C.total_pnl<0?'down':'up')},
    {label:'累计盈亏', v:fmt(C.total_pnl), h:fmt(C.total_pnl_pct)+'%', cls:pnlCls(C.total_pnl)},
    {label:'持仓市值', v:fmt(C.market_value), h:'仓位 '+fmt((C.market_value??0)/(C.equity||1)*100,1)+'%'},
    {label:'可用现金', v:fmt(C.cash), h:'现金占比 '+fmt((C.cash??0)/(C.equity||1)*100,1)+'%'},
    {label:'已实现盈亏', v:fmt(C.realized), h:'—'},
    {label:'持仓数量', v:(C.open_positions??0)+' 只', h:'目标 '+fmt(s.targets?.length||0)+' 只'},
  ];
  $('#cards').innerHTML = cap.map(c=>`
    <div class="card">
      <div class="lbl">${c.label}</div>
      <div class="val ${c.cls||''}">${c.v}</div>
      <div class="hint">${c.h}</div>
    </div>`).join('');
}

function renderPos(positions){
  if(!positions||!positions.length){ $('#posBody').innerHTML='<tr><td colspan="10" class="empty">暂无持仓</td></tr>'; return; }
  const rows = positions.map(p=>{
    const locked = p.locked_qty||0;
    const sellable = p.sellable_qty!=null?p.sellable_qty:(p.qty-locked);
    const luTxt = p.to_limit_up_pct!=null?fmt(p.to_limit_up_pct,1)+'%':'—';
    const ldTxt = p.to_limit_down_pct!=null?fmt(p.to_limit_down_pct,1)+'%':'—';
    let sb='<span class="sBadge ok">可交易</span>';
    const ts=(p.trade_state||'').toLowerCase();
    if(ts.indexOf('涨停')>=0) sb='<span class="sBadge bad">涨停·不可买</span>';
    else if(ts.indexOf('跌停')>=0) sb='<span class="sBadge bad">跌停·不可卖</span>';
    else if(ts.indexOf('停牌')>=0) sb='<span class="sBadge bad">停牌</span>';
    else if(ts.indexOf('t+1')>=0) sb='<span class="sBadge warn">T+1锁定</span>';
    return `<tr>
    <td><div class="name"><span>${p.name||p.canon}</span><span class="code">${p.canon}</span></div></td>
    <td>${fmt(p.qty,0)}</td>
    <td>${fmt(p.avg_cost)}</td>
    <td>${fmt(p.last_price,3)}</td>
    <td>${fmt(p.mv)}</td>
    <td class="${pnlCls(p.pnl_pct)}">${fmt(p.pnl_pct,1)}%</td>
    <td><div style="white-space:nowrap">${fmt((p.weight??0)*100,1)}%<div class="prog" style="width:70px;margin-top:4px"><i style="width:${Math.min((p.weight??0)*100/15*100,100)}%"></i></div></div></td>
    <td style="white-space:nowrap">${fmt(sellable,0)} <span class="muted">/</span> ${locked>0?'<span class="lock">'+fmt(locked,0)+'锁</span>':'<span class="muted">0</span>'}</td>
    <td style="white-space:nowrap"><span class="up">${luTxt}</span> <span class="muted">/</span> <span class="down">${ldTxt}</span></td>
    <td>${sb}</td>
  </tr>`;
  }).join('');
  $('#posBody').innerHTML = rows;
}

function renderRules(s){
  const R = s.rules||{};
  if(!Object.keys(R).length){ $('#rulesBody').innerHTML='<span class="muted">无规则配置</span>'; return; }
  const items = [
    {k:'起始资金', v:fmt(R.init_capital,0)+' 元'},
    {k:'目标持仓数', v:R.max_stocks+' 只'},
    {k:'最大仓位 / 现金底线', v:fmt((R.max_pos_ratio||0)*100,0)+'% / '+fmt((R.cash_cushion||0)*100,1)+'%'},
    {k:'成交单位(整手)', v:R.lot_size+' 股'},
    {k:'T+1 当日锁仓', v:R.tplus1?'生效':'关闭', cls:(R.tplus1?'ok':'bad')},
    {k:'滑点', v:fmt((R.slippage||0)*100,2)+'%(买/卖方向)'},
    {k:'佣金 / 印花税 / 过户', v:fmt((R.commission||0)*10000,1)+'‱ / '+fmt((R.stamp_tax||0)*10000,1)+'‱ / '+fmt((R.transfer_fee||0)*10000,1)+'‱'},
    {k:'止损线', v:'-'+fmt((R.stop_loss_pct||0)*100,0)+'%'},
    {k:'涨跌幅限制', v:Object.values(R.board_limits||{}).join(' / '), note:'主板/创业板/北交所'},
  ];
  $('#rulesBody').innerHTML = items.map(it=>`
    <div class="ruleItem"><div class="k">${it.k}${it.note?'<span class="muted" style="font-weight:400">('+it.note+')</span>':''}</div>
      <div class="v ${it.cls?'down':''}">${it.v}</div></div>`).join('');
}

function renderBacktest(bt){
  if(!bt||!bt.curve||!bt.curve.length){
    $('#btRange').textContent=''; $('#btBody').innerHTML='<span class="empty">尚未运行连续交易日回放(可在终端执行 python backtest_engine.py --days 10)</span>'; return;
  }
  $('#btRange').textContent = `区间 ${bt.start||''} ~ ${bt.end||''} · ${bt.trade_days||bt.curve.length} 个交易日`;
  const maxE = Math.max(...bt.curve.map(c=>c.equity));
  const minE = Math.min(...bt.curve.map(c=>c.equity));
  const span = (maxE-minE)||1;
  const bars = bt.curve.map(c=>{
    const h = 8 + ((c.equity-minE)/span)*82;
    const d = String(c.day||'').slice(5);
    return `<div style="height:${h}%" title="${c.day} ${fmt(c.equity,2)}"><i>${d}</i></div>`;
  }).join('');
  const last = bt.curve[bt.curve.length-1];
  const cls = last.pnl_pct>=0?'up':'down';
  $('#btBody').innerHTML = `
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:6px">
      <span>初始资金 <b>${fmt(bt.init_capital,0)}</b> 元</span>
      <span>期末权益 <b>${fmt(bt.final_equity,2)}</b></span>
      <span>总收益 <b class="${cls}">${fmt(bt.total_return,2)}%</b></span>
      <span>最大回撤 <b>${fmt(bt.max_drawdown_pct,2)}%</b></span>
      <span>总成交 <b>${fmt(bt.total_trades,0)}</b> 笔</span>
    </div>
    <div class="btBars">${bars}</div>
    <div class="muted" style="margin-top:22px;font-size:12px">${bt.note||''}</div>`;
}

// 成交历史翻页状态 (模块级)
var _tState = { all: [], filtered: [], page: 1, perPage: 10 };

function _renderTradePage(){
  const s = _tState;
  const body = $('#tradeBody'); if (!body) return;
  const total = s.filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / s.perPage));
  if (s.page > totalPages) s.page = totalPages;
  if (s.page < 1) s.page = 1;
  const start = (s.page - 1) * s.perPage;
  const pageRows = s.filtered.slice(start, start + s.perPage);
  if (!total) {
    body.innerHTML = '<tr><td colspan="8" class="empty">无成交</td></tr>';
  } else {
    body.innerHTML = pageRows.map(t=>`<tr>
      <td>${t.date||''}</td>
      <td>${t.time||''}</td>
      <td><span class="tag ${t.type}">${t.type==='buy'?'买入':'卖出'}</span></td>
      <td>${t.canon}</td><td>${fmt(t.qty,0)}</td><td>${fmt(t.price)}</td><td>${fmt(t.fee,2)}</td>
      <td class="${t.pnl!=null?(t.pnl>0?'up':(t.pnl<0?'down':'')):''}">${t.pnl!=null?fmt(t.pnl,2):'—'}</td>
    </tr>`).join('');
  }
  // 翻页控件
  const pager = $('#tradePager'); if (!pager) return;
  pager.innerHTML =
    `<span class="muted" style="font-size:12px">${total}笔·第${s.page}/${totalPages}页</span>` +
    `<button class="pgbtn" data-act="first" ${s.page<=1?'disabled':''} title="首页">⟪</button>` +
    `<button class="pgbtn" data-act="prev" ${s.page<=1?'disabled':''} title="上一页">‹</button>` +
    `<input type="number" id="tradePgInput" min="1" max="${totalPages}" value="${s.page}" ` +
    `style="width:48px;text-align:center;font-size:12px;padding:1px 2px" title="跳转页码">` +
    `<button class="pgbtn" data-act="next" ${s.page>=totalPages?'disabled':''} title="下一页">›</button>` +
    `<button class="pgbtn" data-act="last" ${s.page>=totalPages?'disabled':''} title="末页">⟫</button>`;
}

function renderTrades(tradesHistory, tradesToday){
  // tradesHistory: {date: [trade, ...]}  跨日累积; tradesToday: 当日 list (备援)
  // 1) 拍平为数组 + 按 date 倒序
  let all = [];
  if (tradesHistory && typeof tradesHistory === 'object') {
    const dates = Object.keys(tradesHistory).sort().reverse();
    for (const d of dates) {
      for (const t of (tradesHistory[d] || [])) {
        all.push(Object.assign({}, t, { date: d }));
      }
    }
  }
  // 兼容旧数据: 若 history 为空, 用 tradesToday 兜底 (date 用今日)
  if (!all.length && Array.isArray(tradesToday) && tradesToday.length) {
    const today = new Date().toISOString().slice(0,10);
    all = tradesToday.map(t => Object.assign({}, t, { date: t.date || today }));
  }
  // 2) 重建日期过滤选项
  const filter = $('#tradeDateFilter');
  const datesSet = Array.from(new Set(all.map(t => t.date))).sort().reverse();
  const prev = filter.value;
  filter.innerHTML = '<option value="all">全部</option>' +
    datesSet.map(d => `<option value="${d}">${d}${d===datesSet[0]?' (今日)':''}</option>`).join('');
  if (prev && (prev === 'all' || datesSet.includes(prev))) filter.value = prev;
  // 3) 应用过滤
  const sel = filter.value;
  _tState.all = all;
  _tState.filtered = sel === 'all' ? all : all.filter(t => t.date === sel);
  _tState.page = 1;  // 数据刷新后回到第一页
  // 4) 渲染当前页
  _renderTradePage();
  // 5) 计数
  const buyN = _tState.filtered.filter(t=>t.type==='buy').length;
  const sellN = _tState.filtered.filter(t=>t.type==='sell').length;
  const pnlSum = _tState.filtered.reduce((a,t)=>a+(t.pnl||0),0);
  const pnlCls = pnlSum > 0 ? 'up' : (pnlSum < 0 ? 'down' : '');
  const pnlStr = `${pnlSum>=0?'+':''}${fmt(pnlSum,2)}`;
  $('#tradeCount').innerHTML =
    `共 <b>${_tState.filtered.length}</b> 笔 · 买 ${buyN} · 卖 ${sellN} · 累计已实现 <span class="${pnlCls}">${pnlStr} 元</span>`;
}

function renderTargets(targets){
  if(!targets||!targets.length){ $('#targetBody').innerHTML='<tr><td colspan="4" class="empty">—</td></tr>'; return; }
  $('#targetBody').innerHTML = targets.map(t=>`<tr><td>${t.canon}</td><td>${t.name||'—'}</td><td>${fmt(t.score)}</td><td class="${pnlCls(t.signal)}">${fmt(t.signal,3)}</td></tr>`).join('');
}

function renderMidday(m){
  const tag = $('#middayTag');
  if(!tag) return;
  if(!m || !m.active){
    tag.textContent = ''; return;
  }
  const t = m.result ? (m.result.time||'') : '';
  const n = m.targets ? m.targets.length : 0;
  tag.innerHTML = '<span style="color:hsl(var(--bear));font-weight:600">已午间重选' + (t?'  '+t:'') + (n?' · '+n+'只':'') + '</span>';
  if(m.result && m.result.top){ tag.innerHTML += '<span class="muted" style="margin-left:6px">→ ' + m.result.top.join(',') + '</span>'; }
}

// ---------- 市场情绪 ----------
function sentiCls(v){ if(v>=60) return 'hot'; if(v<40) return ''; return ''; }
function renderMarket(days){
  if(!days || !days.length){ $('#sentiBar').innerHTML='<span class="empty">暂无市场情绪数据</span>'; return; }
  $('#mkRange').textContent = days[0].day + ' ~ ' + days[days.length-1].day + ' · ' + days.length + ' 个交易日';
  // 情绪分柱状图
  const maxV = 100;
  $('#sentiBar').innerHTML = days.map(d=>{
    const v = d.sentiment!=null?d.sentiment:0;
    const h = Math.max(4, (v/maxV)*100);
    return `<div class="${v>=60?'hot':''}" style="height:${h}%" title="${d.day} 情绪${v}">`+
           `<em>${Math.round(v)}</em><i>${String(d.day).slice(5)}</i></div>`;
  }).join('');
  // 概览卡片
  const last = days[days.length-1];
  const w = last.width||{}; const l = last.limit||{}; const b = last.breadth||{};
  const tv = last.turnover||{};
  const cards = [
    {k:'最新情绪分', v:last.sentiment!=null?Math.round(last.sentiment)+'/100':'—', cls:last.sentiment>=60?'up':(last.sentiment<40?'down':'')},
    {k:'上涨/下跌家数', v:(w.up||0)+' / '+(w.down||0), h:'涨占比 '+(w.up_ratio!=null?w.up_ratio+'%':'—')},
    {k:'涨停 / 跌停', v:(l.limit_up||0)+' / '+(l.limit_down||0), h:'涨停占比 '+(l.limit_up_ratio!=null?l.limit_up_ratio+'%':'—')},
    {k:'中位数涨幅', v:b.median_chg!=null?fmt(b.median_chg,2)+'%':'—', cls:b.median_chg>0?'up':(b.median_chg<0?'down':'')},
    {k:'两市成交额', v:tv.total_amount_yi!=null?fmt(tv.total_amount_yi,0)+' 亿':'—'},
    {k:'强上涨(≥3%)', v:(b.strong_up_count||0)+' 只', h:'占比 '+(b.strong_up_ratio!=null?b.strong_up_ratio+'%':'—')},
  ];
  $('#mkCards').innerHTML = cards.map(c=>`<div class="miniCard"><div class="k">${c.k}</div>`+
    `<div class="v ${c.cls||''}">${c.v}</div>${c.h?`<div class="muted" style="font-size:11px;margin-top:3px">${c.h}</div>`:''}</div>`).join('');
  renderDonut(last);
  renderSector(last);
}

function renderDonut(last){
  const w = last.width||{};
  const up=w.up||0, down=w.down||0, flat=w.flat||0, tot=w.total||1;
  const upP=up/tot*100, downP=down/tot*100, flatP=flat/tot*100;
  const red='#ff5b6a', green='#2fe6a6', gray='#5a6b8c';
  // 环形: 红=上涨, 绿=下跌, 灰=平盘
  const donut = document.getElementById('upDonut');
  let acc=0;
  const segs=[
    {c:red, p:upP},{c:green,p:downP},{c:gray,p:flatP}
  ].filter(s=>s.p>0.05);
  const stops = segs.map(s=>{ const a=acc.toFixed(2)+'%'; acc+=s.p; const b=acc.toFixed(2)+'%'; return `${s.c} ${a} ${b}`; }).join(',');
  donut.style.background = `conic-gradient(${stops})`;
  donut.innerHTML = `<center><span class="bigNum">${upP.toFixed(1)}%</span><span class="muted" style="font-size:11px">上涨</span></center>`;
  $('#upLegend').innerHTML =
    `<div style="margin-bottom:6px"><span class="legendDot" style="background:${red}"></span>上涨 ${up} 只 (${upP.toFixed(1)}%)</div>`+
    `<div style="margin-bottom:6px"><span class="legendDot" style="background:${green}"></span>下跌 ${down} 只 (${downP.toFixed(1)}%)</div>`+
    `<div><span class="legendDot" style="background:${gray}"></span>平盘 ${flat} 只 (${flatP.toFixed(1)}%)</div>`;
}

function renderSector(last){
  const s = last.sector||{};
  const day = last.day||'';
  $('#secDay').textContent = day;
  const names={'SH':'沪市','SZ':'深市','BJ':'北交所'};
  const keys = Object.keys(s).filter(k=>s[k]!=null);
  if(!keys.length){ $('#secBody').innerHTML='暂无板块成交数据'; return; }
  const maxV = Math.max(...keys.map(k=>s[k]||0),1);
  $('#secBody').innerHTML = keys.map(k=>{
    const v = s[k]||0;
    return `<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
      <span style="width:46px">${names[k]||k}</span>
      <div class="prog" style="flex:1"><i style="width:${(v/maxV*100).toFixed(1)}%"></i></div>
      <b style="min-width:70px;text-align:right">${fmt(v,1)} 亿</b>
    </div>`;
  }).join('');
}

// ---------- 绩效归因 ----------
function renderCommentary(c, meta){
  const $box = $('#pfCommentary'), $ai = $('#pfAiMeta');
  if(!c || typeof c !== 'object' || !c.commentary){
    $box.innerHTML = '<div class="muted">尚无 LLM 点评 — 盘后 run_daily 完成后会在此显示 (每30秒刷新)</div>';
    if($ai) $ai.textContent = '';
    return;
  }
  const pr = c.performance_review || {};
  const nx = c.next_session_brief || {};
  const stanceCls = nx.stance === '加仓' ? 'up' : (nx.stance === '减仓' ? 'down' : '');
  const driversHtml = (pr.drivers||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const risksHtml   = (pr.risks||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const watchHtml   = (nx.watchlist||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const actHtml     = (nx.action_items||[]).map(s=>`<li>${esc(s)}</li>`).join('') || '<li class="muted">无</li>';
  const scoreVal = Number(pr.score||0);
  const scoreCls = scoreVal >= 60 ? 'up' : (scoreVal <= 35 ? 'down' : '');
  const conf = Number(c.confidence||0);
  const confLow = conf <= 0.3;

  $box.innerHTML = `
    <div style="font-size:14px;font-weight:600;margin-bottom:10px;line-height:1.6">${esc(c.commentary)}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;font-size:13px;line-height:1.7">
      <div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">净值解读</div>
        <div style="margin-bottom:6px">${esc(pr.headline||'证据不足')}</div>
        <div style="display:flex;gap:14px;margin-bottom:8px">
          <span>综合评分 <b class="${scoreCls}">${fmt(scoreVal,0)}</b>/100</span>
          <span>置信度 <b class="${confLow?'down':''}">${fmt(conf*100,0)}</b>%${confLow?' <span class="muted">(证据不足)</span>':''}</span>
        </div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">关键驱动</div>
        <ul style="margin:0 0 6px 16px;padding:0">${driversHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">主要风险</div>
        <ul style="margin:0;padding:0 0 0 16px">${risksHtml}</ul>
      </div>
      <div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">次日建议</div>
        <div style="margin-bottom:8px">仓位倾向 <b class="${stanceCls}">${esc(nx.stance||'观望')}</b></div>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">关注要点</div>
        <ul style="margin:0 0 6px 16px;padding:0">${watchHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">建议操作</div>
        <ul style="margin:0 0 8px 16px;padding:0">${actHtml}</ul>
        <div style="color:hsl(var(--fg-muted));margin-bottom:4px">情绪解读</div>
        <div>${esc(c.sentiment_narrative||'证据不足')}</div>
      </div>
    </div>
  `;
  if($ai && meta){
    const lat = meta.latency_seconds ? `${meta.latency_seconds}s` : '';
    const tok = (meta.tokens_in!=null && meta.tokens_out!=null)
      ? `${meta.tokens_in}+${meta.tokens_out} tokens` : '';
    $ai.textContent = `${meta.model||'LLM'} · ${meta.generated_at||''}${lat?' · '+lat:''}${tok?' · '+tok:''}`;
  }
}

function renderPerf(r){
  if(!r || !r.ok){ $('#pfMetrics').innerHTML='<span class="empty">'+(r&&r.error?r.error:'暂无绩效数据')+'</span>'; return; }
  const pd=r.period||{};
  $('#pfRange').textContent = pd.start + ' ~ ' + pd.end + ' · ' + pd.n_days + ' 个交易日（每30秒刷新）';
  // LLM 盘后点评 (MiniMax-M3 归因)
  renderCommentary(r.llm_commentary, r.llm_commentary_meta);
  const m = r.metrics||{};
  const cards=[
    {k:'累计收益', v:fmt(m.total_return,2)+'%', cls:m.total_return>0?'up':(m.total_return<0?'down':'')},
    {k:'年化收益', v:fmt(m.annual_return,2)+'%', cls:m.annual_return>0?'up':(m.annual_return<0?'down':'')},
    {k:'最大回撤', v:fmt(m.max_drawdown,2)+'%', cls:'down'},
    {k:'夏普(年化)', v:m.sharpe_annual!=null?fmt(m.sharpe_annual,2):'—'},
    {k:'索提诺(年化)', v:m.sortino_annual!=null?fmt(m.sortino_annual,2):'—'},
    {k:'Calmar', v:m.calmar!=null?fmt(m.calmar,2):'—'},
    {k:'期末权益', v:fmt(m.final_equity,0), cls:'up'},
  ];
  const b = r.benchmark||{};
  if(b.bench_total_ret!=null){ cards.push({k:'基准超额', v:fmt(b.excess_total!=null?b.excess_total:0,2)+'%', cls:(b.excess_total||0)>=0?'up':'down'}); }
  $('#pfMetrics').innerHTML = cards.map(c=>`<div class="miniCard"><div class="k">${c.k}</div><div class="v ${c.cls||''}">${c.v}</div></div>`).join('');
  // 净值曲线
  const rows = (b.daily&&b.daily.length)?b.daily:null;
  if(rows && rows.length){
    const cum = rows.map(x=>x.port_cum!=null?x.port_cum:0);
    const minC=Math.min(...cum), maxC=Math.max(...cum,0); const span=(maxC-minC)||1;
    $('#pfEquity').innerHTML = cum.map((v,i)=>{
      const h=4+((v-minC)/span)*96;
      return `<div style="height:${h}%" title="${rows[i].day} ${fmt((v)*100,2)}%"><i>${String(rows[i].day).slice(5)}</i></div>`;
    }).join('');
  } else {
    $('#pfEquity').innerHTML='<span class="empty">回执样本不足，无法绘制净值曲线</span>';
  }
  // 持仓归因
  const att = r.attribution||[];
  if(att.length){
    $('#pfAttr').innerHTML = att.map(x=>`<tr>
      <td>${x.canon}</td><td>${fmt(x.qty,0)}</td><td>${fmt(x.avg_cost)}</td>
      <td>${fmt(x.last_price,3)}</td><td>${fmt(x.market_value,2)}</td>
      <td>${fmt((x.weight||0)*100,2)}%</td>
      <td class="${pnlCls(x.unrealized_pnl)}">${fmt(x.unrealized_pnl,2)}</td>
      <td class="${pnlCls(x.unrealized_pct)}">${fmt(x.unrealized_pct,2)}%</td>
    </tr>`).join('');
  } else { $('#pfAttr').innerHTML='<tr><td colspan="8" class="empty">暂无持仓</td></tr>'; }
  // IC监控
  const ic = r.ic_summary||{};
  const keys=Object.keys(ic);
  if(keys.length){
    $('#pfIc').innerHTML = keys.map(k=>`<tr>
      <td>${k}</td><td>${ic[k].n}</td>
      <td class="${ic[k].ic_mean>=0?'up':'down'}">${fmt(ic[k].ic_mean,4)}</td>
      <td>${ic[k].icir!=null?fmt(ic[k].icir,3):'—'}</td>
      <td class="${ic[k].recent_mean20>=0?'up':'down'}">${fmt(ic[k].recent_mean20,4)}</td>
    </tr>`).join('');
  } else { $('#pfIc').innerHTML='<tr><td colspan="5" class="empty">无 IC 数据</td></tr>'; }
  // 因子权重自适应 (异步拉取 /api/weights)
  renderWeights();
  // DRL 微调 (异步拉取 /api/drl)
  loadDrl();
  // 退化监控 (异步拉取 /api/degradation)
  loadDegradation();
}

async function loadDegradation(){
  const $box = $('#pfDegradation');
  if (!$box) return;
  try{
    const r = await fetch('/api/degradation'); const d = await r.json();
    renderDegradation(d);
  }catch(e){ $box.innerHTML = '<div class="empty">退化监控加载失败: '+e+'</div>'; }
}

function _lvlBadge(lvl){
  if (!lvl || lvl === 'OK') return '<span class="lvl OK">OK</span>';
  if (lvl === 'P0') return '<span class="lvl P0">P0 严重</span>';
  if (lvl === 'P1') return '<span class="lvl P1">P1 高</span>';
  if (lvl === 'P2') return '<span class="lvl P2">P2 中</span>';
  if (lvl === 'P3') return '<span class="lvl P3">P3 低</span>';
  return '<span class="lvl">'+esc(lvl)+'</span>';
}

function renderDegradation(d){
  const $box = $('#pfDegradation');
  if (!d || d.error){ $box.innerHTML = '<div class="empty">'+(d && d.error ? d.error : '暂无退化数据')+'</div>'; return; }
  const idx = d.index || {};
  const spc = d.spc || [];
  const alerts = d.alerts || [];
  const inc = d.incremental_learn;
  const rc = d.reward_config || {};

  // 退化综合分 (环形进度)
  const score = Number(idx.overall_score || 0);
  const worst = idx.worst_level || 'OK';
  const components = idx.components || [];

  let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">';

  // 左: 退化指数 + 维度
  html += '<div>';
  html += `<div style="display:flex;align-items:center;gap:14px;margin-bottom:10px">
    <div style="position:relative;width:90px;height:90px">
      <svg viewBox="0 0 36 36" style="width:90px;height:90px;transform:rotate(-90deg)">
        <circle cx="18" cy="18" r="15" fill="none" stroke="#1f2a3a" stroke-width="3"/>
        <circle cx="18" cy="18" r="15" fill="none" stroke="${score>=70?'#2fe6a6':(score>=40?'#f59e0b':'#ff5b6a')}"
          stroke-width="3" stroke-dasharray="${(score/100)*94.2} 94.2" stroke-linecap="round"/>
      </svg>
      <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700">${Math.round(score)}</div>
    </div>
    <div>
      <div style="font-size:13px;color:hsl(var(--fg-muted))">综合退化指数 (越高越健康)</div>
      <div style="margin-top:4px">最差维度 ${_lvlBadge(worst)}</div>
    </div>
  </div>`;
  // 维度
  if (components.length){
    html += '<div style="margin-top:8px">';
    for (const c of components){
      const s = Number(c.score || 0);
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;font-size:12px">
        <span style="width:120px">${esc(c.name)}</span>
        ${_lvlBadge(c.level)}
        <span style="flex:1;background:hsl(var(--elevated));border-radius:3px;height:6px;position:relative;overflow:hidden">
          <span style="position:absolute;left:0;top:0;height:100%;width:${Math.max(2,s)}%;background:${s>=70?'#2fe6a6':(s>=40?'#f59e0b':'#ff5b6a')}"></span>
        </span>
        <span style="width:36px;text-align:right">${s}</span>
      </div>`;
    }
    html += '</div>';
  }
  html += '</div>';

  // 右: SPC 告警 + 增量学习
  html += '<div>';
  // SPC 卡片
  if (spc.length){
    html += '<div style="margin-bottom:12px"><div style="color:hsl(var(--fg-muted));font-size:12px;margin-bottom:6px">SPC 过程控制 (近 40 日)</div>';
    for (const s of spc){
      const lvl = s.level || 'OK';
      const cls = lvl === 'P0' ? '#ff5b6a' : (lvl === 'P1' ? '#f59e0b' : (lvl === 'P2' ? '#3b82f6' : '#2fe6a6'));
      html += `<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;margin-bottom:4px;border-left:3px solid ${cls};background:hsl(var(--elevated));border-radius:3px">
        ${_lvlBadge(lvl)}
        <span style="width:140px">${esc(s.indicator)}</span>
        <span style="flex:1;font-size:12px;color:hsl(var(--fg-secondary))">${esc((s.violations||[])[0]||s.message||'OK')}</span>
      </div>`;
    }
    html += '</div>';
  }
  // reward_config
  html += `<div style="margin-bottom:8px;font-size:12px;color:hsl(var(--fg-muted))">
    DRL reward 权重: vnpy <b>${(rc.vnpy_weight||0.6).toFixed(2)}</b>
    / ic <b>${(rc.ic_weight||0.4).toFixed(2)}</b>
  </div>`;
  // 增量学习
  if (inc && inc._day_dir){
    const opt = inc.optimization || {};
    const ar = opt.action_recommendation || {};
    html += `<div style="padding:8px;background:hsl(var(--elevated));border-radius:5px;border-left:3px solid #8b5cf6">
      <div style="font-size:12px;color:hsl(var(--fg-muted));margin-bottom:4px">LLM 策略优化 (${esc(inc._day_dir)})</div>
      <div style="font-size:13px;margin-bottom:4px"><b>${esc(opt.diagnosis_summary||'—')}</b></div>
      <div style="font-size:12px;margin-bottom:4px">操作: ${_lvlBadge(ar.priority||'OK')} ${esc(ar.primary||'hold')}</div>
      ${(opt.root_causes||[]).length ? `<div style="font-size:11px;color:hsl(var(--fg-secondary));margin-top:4px">根因: ${esc((opt.root_causes||[]).slice(0,3).join('; '))}</div>` : ''}
    </div>`;
  } else {
    html += '<div style="font-size:12px;color:hsl(var(--fg-muted))">增量学习: 未触发 (无 P0/P1 退化)</div>';
  }
  html += '</div>';

  html += '</div>';  // 闭合 grid
  $box.innerHTML = html;
}

async function loadDrl(){
  const $box = $('#pfDrl');
  if (!$box) return;
  try{
    const r = await fetch('/api/drl'); const d = await r.json();
    renderDrl(d);
  }catch(e){ $box.innerHTML = '<div class="empty">DRL 数据加载失败: '+e+'</div>'; }
}

function _sentBar(label, v, lo, hi){
  const norm = lo < 0 ? (v - lo) / (hi - lo) : 0.5;
  const pct = Math.max(0, Math.min(100, norm * 100));
  const cls = v > 0.05 ? 'up' : (v < -0.05 ? 'down' : '');
  return `<div style="margin-bottom:6px">
    <span style="display:inline-block;width:120px">${label}</span>
    <span style="display:inline-block;width:60px;text-align:right" class="${cls}">${v>=0?'+':''}${Number(v).toFixed(2)}</span>
    <span style="display:inline-block;width:200px;vertical-align:middle">
      <span style="display:inline-block;width:${pct}%;height:6px;background:#2fe6a6;border-radius:3px"></span>
    </span>
  </div>`;
}

function _weightBar(label, base, prior){
  const final = prior; // 仅展示 base -> prior, 避免与 PPO 最终权重混淆
  const maxV = Math.max(base, prior, 0.05);
  const baseW = Math.max(2, base / maxV * 100);
  const priorW = Math.max(2, prior / maxV * 100);
  return `<tr>
    <td>${label}</td>
    <td style="width:40%">
      <div style="display:flex;gap:8px;font-size:11px;color:hsl(var(--fg-muted));margin-bottom:2px">
        <span>base ${(base*100).toFixed(1)}%</span>
        <span>prior ${(prior*100).toFixed(1)}%</span>
      </div>
      <div style="background:hsl(var(--elevated));border-radius:3px;overflow:hidden;height:14px;position:relative">
        <div style="position:absolute;left:0;top:0;height:100%;width:${baseW}%;background:hsl(var(--fg-muted));opacity:0.5"></div>
        <div style="position:absolute;left:0;top:0;height:100%;width:${priorW}%;background:#1f9d55"></div>
      </div>
    </td>
  </tr>`;
}

function renderDrl(d){
  const $box = $('#pfDrl');
  if (!d || !d.days || !d.days.length){
    $box.innerHTML = '<div class="muted">尚无 DRL 训练产物 — 盘后 run_daily 完成后会在此显示</div>';
    return;
  }
  const day = d.days[0];
  const m = day.meta || {};
  const b = day.brief || {};
  const sf = b.sentiment_factors || {};
  const fr = b.factor_recommendations || {};
  const bw = m.base_weights || {};
  const pw = m.prior_weights || {};

  const stanceCls = b.stance === '加仓' ? 'up' : (b.stance === '减仓' ? 'down' : '');
  const regimeStr = b.regime || '—';
  const deltaScale = (m.llm_brief && m.llm_brief.delta_scale) || 1.0;
  const conf = b.confidence || 0;

  let briefHtml = '';
  if (b.market_summary || b.stance){
    briefHtml = `
      <div style="margin-bottom:10px;padding:10px;background:hsl(var(--elevated));border-radius:6px;border-left:3px solid #1f9d55">
        <div style="font-size:13px;line-height:1.6;margin-bottom:6px">${esc(b.market_summary||'证据不足')}</div>
        <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:hsl(var(--fg-secondary))">
          <span>regime <b>${esc(regimeStr)}</b></span>
          <span>stance <b class="${stanceCls}">${esc(b.stance||'—')}</b></span>
          <span>置信度 <b>${(conf*100).toFixed(0)}</b>%</span>
          <span>扰动幅度 ×${deltaScale.toFixed(2)}</span>
        </div>
      </div>
    `;
  } else {
    briefHtml = '<div class="muted">未找到 pre_drl_brief 产物 (本次 DRL 未引用 LLM brief)</div>';
  }

  // 情绪因子条
  const sentHtml = (sf.risk_on_off !== undefined) ? `
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px">
      ${_sentBar('风险偏好', sf.risk_on_off ?? 0, -1, 1)}
      ${_sentBar('轮动强度', sf.rotation_intensity ?? 0, -1, 1)}
      ${_sentBar('流动性(反转)', sf.liquidity_stress ?? 0, -1, 1)}
      ${_sentBar('政策催化', sf.policy_catalyst ?? 0, -1, 1)}
    </div>
  ` : '';

  // 先验权重 vs 基础权重
  const factors = Object.keys(bw).filter(k => pw[k] != null);
  let weightsHtml = '';
  if (factors.length){
    weightsHtml = `
      <table class="tbl" style="margin-bottom:10px">
        <thead><tr><th style="width:90px">因子</th><th>基础 vs LLM先验权重</th></tr></thead>
        <tbody>${factors.map(k => _weightBar(k, bw[k], pw[k])).join('')}</tbody>
      </table>
    `;
  }

  // 训练概要
  const finalW = m.final_weights || {};
  const topFactors = Object.entries(finalW).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const metaHtml = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px;font-size:12px">
      <div><div class="muted">Timesteps</div><b>${m.total_timesteps || '—'}</b></div>
      <div><div class="muted">Mean Reward</div><b class="${(m.mean_reward||0)>=0?'up':'down'}">${fmt(m.mean_reward, 4)}</b></div>
      <div><div class="muted">Obs Dim</div><b>${(m.llm_brief && m.llm_brief.obs_dim) || '—'}</b></div>
      <div><div class="muted">vnpy Reward</div><b class="${(m.vnpy_reward||0)>=0?'up':'down'}">${fmt(m.vnpy_reward, 4)}</b></div>
    </div>
    <div style="font-size:12px;color:hsl(var(--fg-secondary))">
      Top-3 因子权重: ${topFactors.map(([k, v]) => `${k} ${(v*100).toFixed(1)}%`).join(' · ')}
    </div>
  `;

  $box.innerHTML = `
    <div style="margin-bottom:8px;color:hsl(var(--fg-muted));font-size:12px">交易日 ${day.day}</div>
    ${briefHtml}
    ${sentHtml}
    ${weightsHtml}
    ${metaHtml}
  `;
}

async function renderWeights(){
  const $w = $('#pfWeights'), $m = $('#pfWmeta');
  let d;
  try{ const resp = await fetch('/api/weights'); d = await resp.json(); }
  catch(e){ $w.innerHTML='<span class="empty">权重数据读取失败</span>'; return; }
  if(!d || !d.ok || !Object.keys(d.weights||{}).length){
    $w.innerHTML='<span class="empty">尚无自适应权重 (weight_optimizer 未运行)</span>';
    $m.textContent=''; return;
  }
  const meta = d.meta||{};
  const icir = meta.icir||{};
  // 权重卡片(含 ICIR 依据标注)
  const wt = d.weights||{};
  const cards = Object.keys(wt).map(k=>{
    const base = (meta.static_base||{})[k];
    const g = icir[k];
    const isAlpha = !!g;
    const hintParts=[];
    if(isAlpha){
      hintParts.push('ICIR '+(g.icir!=null?fmt(g.icir,3):'—'));
      hintParts.push('|ICIR| '+(g.icir!=null?fmt(Math.abs(g.icir),3):'—'));
    }
    hintParts.push('静态 '+fmt((base!=null?base:0)*100,1)+'%');
    let cls = '';
    if(isAlpha && g.icir!=null) cls = g.icir<0?'down':'up';
    return `<div class="miniCard"><div class="k">${k}${isAlpha?' <span class="sBadge alpha">α</span>':''}</div>
      <div class="v ${cls}">${fmt(wt[k]*100,2)}%</div>
      <div class="hint">${hintParts.join(' · ')}</div></div>`;
  }).join('');
  $w.innerHTML = cards;
  // 说明行
  const updated = d.updated||meta.generated||'';
  $m.innerHTML = '方法: ' + (meta.note||'') + ' · 窗口 ' + (meta.window||'—')
    + ' 日 / alpha 占比 ' + fmt((meta.alpha_share!=null?meta.alpha_share*100:0),0) + '%'
    + (updated?(' · 更新 ' + updated):'');
}

function apply(s){
  window._lastState = s;   // 缓存: 成交历史日期过滤 change 时复用
  // 安全写入: 任一元素不存在或缺数据时不中断整体渲染
  const _setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  _setTxt('sub', '更新 ' + (s.updated||'—'));
  _setTxt('bDay', '交易日 ' + (s.day||'—'));
  const m = s.mode||'';
  const bm = document.getElementById('bMode');
  if (bm) { bm.textContent = m; bm.className='badge '+(s.in_session?'live':'idle'); }
  const bs = document.getElementById('bSrc');
  if (bs) bs.textContent = '数据源: ' + (s.live_source||'—');
  _setTxt('bTick', 'tick #' + (s.tick||'-'));
  try {
    if(s.feed_error){ if(bs) bs.textContent += ' ⚠'; _setTxt('sub', '更新 ' + (s.updated||'—') + ' · 实时告警:' + s.feed_error.slice(0,40)); }
    const _tryRender = (name, fn) => { try{ fn(); }catch(e){ console.error('render '+name+' err:', e); } };
    _tryRender('Cards',    () => renderCards(s));
    _tryRender('Pos',      () => renderPos(s.positions));
    _tryRender('Trades',   () => renderTrades(s.trades_history, s.trades_today));
    _tryRender('Targets',  () => renderTargets(s.targets));
    _tryRender('Midday',   () => renderMidday(s.midday));
    _tryRender('Rules',    () => renderRules(s));
  } catch(err) {
    if (typeof console !== 'undefined') console.error('apply err:', err);
    // 把错误显式渲染到 #cards, 避免静默失败
    const cards = document.getElementById('cards');
    if(cards) cards.innerHTML = '<div style="color:#f55;padding:8px;border:1px solid #f55;border-radius:4px">渲染异常: '+err+'</div>';
  }
}

let logPos = 0; let logBuf = []; let logErrCnt = 0;
function renderLogs(){
  const box = $('#logBox'); if(!box) return;
  box.innerHTML = logBuf.map(l=>{
    let cls='', tag='';
    if(l.indexOf('[daemon]')>=0) tag='tag-daemon';
    else if(l.indexOf('[daily]')>=0) tag='tag-daily';
    else if(/\[sub\]/.test(l)) tag='tag-sub';
    if(/error|异常|失败|卡死/i.test(l)) cls='err';
    // 提取时间戳(前19字符)与剩余内容
    const m = l.match(/^(\S+\s+\S+)\s+(.*)$/);
    return `<div class="logLine ${cls}">${m?`<span class="ts">${m[1]}</span>${m[2].replace(/\[(daemon|daily|sub)\]/, (mm,tg)=>`<span class="tag-${tg}">[${tg}]</span>`)}`:l}</div>`;
  }).join('');
  // 自动滚到底
  if(box.scrollTop + box.clientHeight >= box.scrollHeight - 30 || box._follow===undefined){
    box.scrollTop = box.scrollHeight; box._follow=1;
  }
  if(logErrCnt>5){ $('#logDot').textContent='● 守护离线'; $('#logDot').className='sBadge bad'; }
}
async function loadLogs(){
  try{
    const r = await fetch('/api/logs?pos='+logPos);
    const d = await r.json();
    logErrCnt=0; $('#logDot').textContent='● 守护在线'; $('#logDot').className='sBadge ok';
    if(d && d.lines && d.lines.length){
      logBuf = logBuf.concat(d.lines);
      if(logBuf.length>500) logBuf = logBuf.slice(-500);
      renderLogs();
    }
    if(d && d.pos){ logPos = d.pos; }
  }catch(e){
    logErrCnt++; $('#logDot').textContent='● 守护离线'; $('#logDot').className='sBadge bad';
  }
}

async function load(){
  try{
    const r = await fetch('/api/live'); const s = await r.json();
    if(s){ apply(s); }
  }catch(e){
    const sub = document.getElementById('sub');
    if(sub) sub.textContent='连接失败: '+e;
    // 把诊断信息也写到面板上, 避免静默错误
    const cards = document.getElementById('cards');
    if(cards) cards.innerHTML = '<div style="color:#f55">fetch /api/live 异常: '+e+'</div>';
    if(typeof console !== 'undefined') console.error('load /api/live err:', e);
  }
  try{
    const rb = await fetch('/api/backtest'); const bt = await rb.json();
    renderBacktest(bt);
  }catch(e){ /* 忽略回放加载错误 */ }
}
load(); setInterval(load, 3000);
loadLogs(); setInterval(loadLogs, 3000);
loadOverview(); setInterval(loadOverview, 15000);
loadBacktestHistory();  // 回测历史面板首屏即加载 (与用户是否切 tab 无关)
// 系统健康 · 门控 与 风控事件流 (对应规范"系统状态"与"风险风控"面板)
async function loadOverview(){
  try{
    const d = await fetch('/api/overview').then(r=>r.json());
    const G = document.getElementById('sysGate');
    if(G && d.gate){
      const g = d.gate;
      if(g.error){ G.innerHTML='<div class="muted">门控数据缺失: '+esc(g.error)+'</div>'; }
      else{
        const col = g.regime==='risk' ? '#f66' : (g.regime==='caution' ? '#fa0' : '#2d7');
        const dot = n => { const v=(d.procs||{})[n];
          if(!v||v.alive===null) return '<span style="color:#888">●</span>';
          return v.alive?'<span style="color:#2d7">●</span>':'<span style="color:#f66">●</span>'; };
        const dt = d.data||{};
        const stale = (dt.stale&&dt.stale.length) ? dt.stale.join(', ') : '无';
        const hb = d.engine_heartbeat||{};
        G.innerHTML =
          '<div style="display:flex;flex-wrap:wrap;gap:8px 22px;align-items:center">'
          +'<span>IC门控 <b style="color:'+col+'">'+esc(g.regime||'—')+'</b></span>'
          +'<span>暴露 ×'+fmt(g.exposure_mult!=null?g.exposure_mult:1,2)+'</span>'
          +'<span>调仓间隔 '+esc(g.interval_days||'—')+'日</span>'
          +'<span>IC均值 '+fmt(g.ic_mean,4)+' (as_of '+esc(g.ic_as_of||'—')+')</span>'
          +'<span>冻结新买 '+(g.freeze_new_buys?'<b style="color:#f66">是</b>':'否')+'</span>'
          +'</div>'
          +'<div style="margin-top:8px;font-size:12px">'
          +'守护 '+dot('daemon')+' 引擎 '+dot('engine')+' 仪表台 '+dot('dashboard')
          +' <span class="muted">|</span> 引擎心跳 '+esc(hb.updated||'—')
          +' <span class="muted">|</span> 模式 '+esc(hb.mode||'—')
          +'<br>数据巡检 '+esc(dt.report_ts||'未生成')+' · 基准 '+esc(dt.base||'—')
          +' · 规范表 '+((dt.checks)?dt.checks.ok+'/'+dt.checks.total+' 通过':'—')
          +' · 滞后表: <b style="color:'+(stale==='无'?'#2d7':'#f66')+'">'+esc(stale)+'</b>'
          +'</div>';
      }
    }
    const R = document.getElementById('riskEvents');
    if(R){
      const ev = d.risk_events||[];
      if(!ev.length){ R.innerHTML='<div class="muted">暂无风控/门控/异常事件</div>'; }
      else{
        R.innerHTML = ev.map(e=>
          '<div style="font-size:12px;padding:3px 0;border-bottom:1px dashed rgba(255,255,255,.08)">'
          +'<span class="muted">'+esc(e.ts||'')+'</span> [<b>'+esc(e.src)+'</b>] '+esc(e.line)+'</div>'
        ).join('');
      }
    }
  }catch(e){ /* 概览面板加载失败不阻塞主流程 */ }
}

// tab 徽章数: 启动后延迟 800ms 首次刷新, 此后每 15s 一次
setTimeout(refreshTabBadges, 800);
setInterval(refreshTabBadges, 15000);

// 成交历史日期过滤 -> 切换时仅重渲染 tradeBody (无需等下次状态拉取)
(function(){
  const f = $('#tradeDateFilter');
  if (f) f.addEventListener('change', () => {
    // 重新触发一次过滤渲染: 用上一次的 trades_history / trades_today
    if (window._lastState) renderTrades(window._lastState.trades_history, window._lastState.trades_today);
  });
})();

// 成交历史翻页控件事件委托 (控件每次重建, 用 document 委托避免重复绑定)
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest && ev.target.closest('.pgbtn');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  const s = _tState;
  const totalPages = Math.max(1, Math.ceil(s.filtered.length / s.perPage));
  let target = s.page;
  if (act === 'prev') target = s.page - 1;
  else if (act === 'next') target = s.page + 1;
  else if (act === 'first') target = 1;
  else if (act === 'last') target = totalPages;
  if (target < 1 || target > totalPages) return;
  if (target !== s.page) { s.page = target; _renderTradePage(); }
});
document.addEventListener('change', (ev) => {
  if (ev.target && ev.target.id === 'tradePgInput') {
    const s = _tState;
    const totalPages = Math.max(1, Math.ceil(s.filtered.length / s.perPage));
    let v = parseInt(ev.target.value, 10);
    if (isNaN(v)) v = s.page;
    v = Math.min(Math.max(v, 1), totalPages);
    if (v !== s.page) { s.page = v; _renderTradePage(); }
  }
});

// ---------- Tab 切换与二级加载 ----------
let mkLoaded=false, pfLoaded=false;
let mbLoaded=false;
let btLoaded=false, cptLoaded=false, indLoaded=false;
let monLoaded=false, regLoaded=false, abnLoaded=false, dbpLoaded=false;

// 工具: 给 tab 按钮的 badge-count 写值, 0 / 异常时不显示
function _setBadge(name, val){
  const el = document.getElementById('cnt-' + name);
  if (!el) return;
  if (val == null || val === 0 || isNaN(val)) el.textContent = '·';
  else if (val > 999) el.textContent = '999+';
  else el.textContent = String(val);
}

// 异步刷新所有 tab 的徽章数 (基于 /api 各端点的真实数据)
async function refreshTabBadges(){
  // 异动 (涨停数)
  try {
    const r = await fetch('/api/abnormal?limit=100').then(r=>r.json());
    let upCount = 0;
    if (Array.isArray(r)) upCount = r.filter(x => (x.change_pct || x.pct_chg || 0) >= 9.5).length;
    else if (r && Array.isArray(r.limit_up)) upCount = r.limit_up.length;
    else if (r && Array.isArray(r.top_change)) upCount = r.top_change.filter(x => (x.change_pct || 0) >= 9.5).length;
    else if (r && Array.isArray(r.rows)) upCount = r.rows.filter(x => (x.change_pct || x.pct_chg || 0) >= 9.5).length;
    _setBadge('abnormal', upCount);
  } catch(e) { _setBadge('abnormal', null); }
  // 回测历史
  try {
    const r = await fetch('/api/backtest/history').then(r=>r.json());
    _setBadge('backtest', (r.rows || []).length);
  } catch(e) { _setBadge('backtest', null); }
  // 监控中心
  try {
    const r = await fetch('/api/monitor').then(r=>r.json());
    const n = (r.alerts || r.items || []).length;
    _setBadge('monitor', n);
  } catch(e) { _setBadge('monitor', null); }
  // 数据板块 (表数量)
  try {
    const r = await fetch('/api/db_meta').then(r=>r.json());
    const n = (r.tables || []).length;
    _setBadge('dbpanel', n);
  } catch(e) { _setBadge('dbpanel', null); }
  // 市场看板 (指数数)
  try {
    const r = await fetch('/api/marketboard').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.indices)) n = r.indices.length;
    else if (r && Array.isArray(r.kpi)) n = r.kpi.length;
    else if (r && r.kpi) n = Object.keys(r.kpi).length;
    _setBadge('dashboard', n);
  } catch(e) { _setBadge('dashboard', null); }
  // 市场情绪
  try {
    const r = await fetch('/api/market').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.rows)) n = r.rows.length;
    else if (r && r.up_count != null) n = r.up_count + (r.down_count || 0);
    else if (r && r.series) n = r.series.length;
    _setBadge('market', n);
  } catch(e) { _setBadge('market', null); }
  // 概念
  try {
    const r = await fetch('/api/concept').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.concepts)) n = r.concepts.length;
    else if (r && Array.isArray(r.items)) n = r.items.length;
    _setBadge('concept', n);
  } catch(e) { _setBadge('concept', null); }
  // 行业
  try {
    const r = await fetch('/api/industry').then(r=>r.json());
    let n = 0;
    if (Array.isArray(r)) n = r.length;
    else if (r && Array.isArray(r.industries)) n = r.industries.length;
    else if (r && Array.isArray(r.items)) n = r.items.length;
    _setBadge('industry', n);
  } catch(e) { _setBadge('industry', null); }
}

// ===================== 深度分析 (K线/净值/月收益/持仓/导出) =====================
const UP='#ff6b6b', DOWN='#3dd68c', AXIS='#8b98b3', GRID='rgba(255,255,255,.07)';
const MA_COL={ma5:'#f0b400',ma10:'#4f8cff',ma20:'#c678dd',ma60:'#26d0ce'};
let deepLoaded=false;

function deepColors(){
  const cs=getComputedStyle(document.documentElement);
  const r=cs.getPropertyValue('--base')||'224 232 245';
  return 'hsl('+r+')';
}
function axisLabel(ctx,x,y,txt,align){
  ctx.fillStyle=AXIS; ctx.font='10px ui-monospace,Consolas,monospace'; ctx.textAlign=align||'center';
  ctx.fillText(txt,x,y);
}
function gridLines(ctx,vals,x0,x1,y,y0){
  ctx.strokeStyle=GRID; ctx.lineWidth=1;
  for(const v of vals){ const yy=y0+(v-y0)/1; ctx.beginPath(); ctx.moveTo(x0, yy); ctx.lineTo(x1, yy); ctx.stroke(); }
}
function niceMinMax(a,b,n){
  const pad=(b-a)*0.08||1; let lo=a-pad, hi=b+pad;
  return [lo,hi];
}

// ---------- 组合净值·回撤 ----------
let eqCache=null;
async function loadEq(){
  try{ const d=await fetch('/api/curve').then(r=>r.json()); eqCache=d; drawEq(ksRange); }
  catch(e){ const t=document.getElementById('eqTip'); if(t)t.textContent='净值加载失败: '+e; }
}
let ksRange='all';
function drawEq(range, hoverIdx){
  hoverIdx = (hoverIdx==null)?-1:hoverIdx;
  const cv=document.getElementById('eqCanvas'); if(!cv||!eqCache||!eqCache.ok){return;}
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  let rows=eqCache.rows;
  const nMax = range==='all'?rows.length:(+range);
  rows=rows.slice(-nMax);
  if(rows.length<2){ const t=document.getElementById('eqTip'); if(t)t.textContent='回执样本不足, 暂无法绘制'; return; }
  const L=70,R=16,T=14,B=24, M=T+14;
  const mainBtm=H-B-((H-T-B)*0.26);
  // 上:净值; 下:回撤面积
  const navs=rows.map(r=>r.nav), dds=rows.map(r=>r.dd);
  const [lo,hi]=niceMinMax(Math.min.apply(0,navs),Math.max.apply(0,navs),10);
  const x=i=>L+i*(W-L-R)/(rows.length-1);
  const yN=v=>M+(1-(v-lo)/(hi-lo))*(mainBtm-M);
  // 回撤区 (负数)
  const yD0=H-B-8, ddTop=H-B-(H-T-B)*0.26;
  // grid & axis nav
  const gridN=6;
  for(let i=0;i<=gridN;i++){ const v=lo+(hi-lo)*i/gridN; const yy=yN(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(3),'right'); }
  // 净值线
  ctx.strokeStyle='#4f8cff'; ctx.lineWidth=1.6; ctx.beginPath();
  rows.forEach((r,i)=>{ const px=x(i),py=yN(r.nav); i?ctx.lineTo(px,py):ctx.moveTo(px,py); });
  ctx.stroke();
  // 首日基准虚线
  ctx.strokeStyle='rgba(255,255,255,.25)'; ctx.setLineDash([4,4]); ctx.beginPath();
  ctx.moveTo(L,yN(1)); ctx.lineTo(W-R,yN(1)); ctx.stroke(); ctx.setLineDash([]);
  axisLabel(ctx,L+4,yN(1)-4,'1.00 (基准)','left');
  // 回撤填充(底部区)
  const dLo=Math.min.apply(0,dds), dHi=0;
  ctx.fillStyle='rgba(255,107,107,.20)'; ctx.beginPath();
  ctx.moveTo(x(0),yD0);
  dds.forEach((v,i)=>ctx.lineTo(x(i), yD0-(v-dLo)/((dHi-dLo)||1)*(yD0-ddTop)));
  ctx.lineTo(x(rows.length-1),yD0); ctx.closePath(); ctx.fill();
  // 回撤轴
  for(let i=0;i<=3;i++){ const v=dLo+(0-dLo)*i/3; const yy=yD0-(v-dLo)/((dHi-dLo)||1)*(yD0-ddTop);
    axisLabel(ctx,L-6,yy+3,v.toFixed(1)+'%','right'); }
  axisLabel(ctx,L,ddTop-4,'回撤%','left');
  // x 轴日期
  const step=Math.max(1,Math.floor(rows.length/8));
  for(let i=0;i<rows.length;i+=step){ axisLabel(ctx,x(i),H-6,rows[i].day.slice(2),'center'); }
  axisLabel(ctx,L,10,'净值','left');
  // 十字光标
  if(hoverIdx>=0 && hoverIdx<rows.length){
    const px=x(hoverIdx);
    ctx.strokeStyle='rgba(255,255,255,.45)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, H-B); ctx.stroke();
    const py=yN(rows[hoverIdx].nav);
    ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(W-R, py); ctx.stroke();
    ctx.fillStyle='#4f8cff';
    ctx.beginPath(); ctx.arc(px, py, 3.2, 0, Math.PI*2); ctx.fill();
  }
  // hover
  cv._rows=rows; cv._xf=x; cv._yf=yN; cv._type='eq';
}
function fmtPct(v){ return (v>=0?'+':'')+fmt(v,2)+'%'; }

// ---------- K线 · 技术指标 ----------
const ks={bars:[],winStart:0,winLen:110,ind:'macd'};
async function loadK(sym){
  const code=(sym||document.getElementById('kSymInput').value||'600519').trim();
  if(!/^\d{6}$/.test(code.split('.')[0])){ return; }
  try{
    const d=await fetch('/api/kline?sym='+encodeURIComponent(code)+'&days=260').then(r=>r.json());
    if(!d.ok){ const t=document.getElementById('kTip'); if(t)t.textContent=d.error||'K线加载失败'; return; }
    ks.bars=d.bars; ks.winLen=Math.min(ks.winLen,d.bars.length);
    ks.winStart=Math.max(0,d.bars.length-ks.winLen);
    drawK(-1);
  }catch(e){ const t=document.getElementById('kTip'); if(t)t.textContent='K线加载失败: '+e; }
}
function drawK(hoverIdx){
  const cv=document.getElementById('kCanvas'); if(!cv||!ks.bars.length){return;}
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  const bars=ks.bars.slice(ks.winStart, ks.winStart+ks.winLen);
  if(!bars.length) return;
  const L=70,R=16,T=12,B=20;
  const topH=Math.round((H-T-B)*0.52), volTop=T+topH+6, volH=Math.round((H-T-B)*0.14),
        indTop=volTop+volH+8, indH=H-B-indTop-4;
  // 主区
  const hs=bars.map(b=>b.h).filter(v=>v!=null), ls=bars.map(b=>b.l).filter(v=>v!=null);
  let mn=Math.min.apply(0,ls), mx=Math.max.apply(0,hs);
  const [lo,hi]=niceMinMax(mn,mx,12);
  const cw=(W-L-R)/bars.length, bw=Math.max(1,Math.min(10,cw*0.62));
  const x=i=>L+i*cw+cw/2, yP=v=>T+(1-(v-lo)/(hi-lo))*topH;
  // grid & price axis
  for(let i=0;i<=6;i++){ const v=lo+(hi-lo)*i/6; const yy=yP(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(2),'right'); }
  // candles
  bars.forEach((b,i)=>{
    if(b.o==null||b.c==null) return;
    const up=b.c>=b.o, col=up?UP:DOWN;
    const yy=yP;
    ctx.strokeStyle=col; ctx.fillStyle=col;
    ctx.beginPath(); ctx.moveTo(x(i),yP(b.h)); ctx.lineTo(x(i),yP(b.l)); ctx.stroke();
    const yO=yP(b.o),yC=yP(b.c),yy0=Math.min(yO,yC),hh=Math.max(1,Math.abs(yO-yC));
    ctx.fillRect(x(i)-bw/2, yy0, bw, hh);
  });
  // MA lines (主区)
  for(const k of ['ma5','ma10','ma20','ma60']){
    const col=MA_COL[k]; ctx.strokeStyle=col; ctx.lineWidth=1.1; ctx.beginPath(); let started=false;
    bars.forEach((b,i)=>{ const v=b[k]; if(v==null){started=false;return;} const px=x(i),py=yP(v);
      if(!started){ctx.moveTo(px,py);started=true;} else ctx.lineTo(px,py); });
    ctx.stroke();
  }
  // 成交量
  let vmax=0; bars.forEach(b=>{ if(b.v!=null && b.v>vmax) vmax=b.v; });
  bars.forEach((b,i)=>{
    if(b.o==null||b.c==null) return;
    const up=b.c>=b.o; const col=up?UP:DOWN;
    const hh=b.v!=null? (b.v/vmax)*volH : 0;
    ctx.fillStyle=up?col:col;
    ctx.globalAlpha=.55; ctx.fillRect(x(i)-bw/2, volTop+volH-hh, bw, hh); ctx.globalAlpha=1;
  });
  axisLabel(ctx,L,volTop-4,'量','left');
  // 副图指标
  axisLabel(ctx,L,indTop-4,ks.ind.toUpperCase(),'left');
  if(ks.ind==='macd'){
    const vs=bars.map(b=>[b.dif,b.dea,b.macd]).flat().filter(v=>v!=null);
    let mi=Math.min.apply(0,vs), ma=Math.max.apply(0,vs); const pad=(ma-mi)*.1||1; mi-=pad; ma+=pad;
    const yy=v=>indTop+indH-(v-mi)/(ma-mi)*indH;
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy(0)); ctx.lineTo(W-R,yy(0)); ctx.stroke();
    bars.forEach((b,i)=>{ if(b.macd==null)return; const c=b.macd>=0?UP:DOWN;
      ctx.fillStyle=c; const y0=yy(0),ym=yy(b.macd); ctx.fillRect(x(i)-bw/2,Math.min(y0,ym),bw,Math.max(1,Math.abs(ym-y0))); });
    for(const k of ['dif','dea']){ const col=k==='dif'?'#f0b400':'#4f8cff'; ctx.strokeStyle=col; ctx.lineWidth=1.1;
      ctx.beginPath(); let s=false; bars.forEach((b,i)=>{ const v=b[k]; if(v==null){s=false;return;} const px=x(i),py=yy(v); if(!s){ctx.moveTo(px,py);s=true;} else ctx.lineTo(px,py); }); ctx.stroke(); }
  } else { // rsi
    const yy=v=>indTop+(1-v/100)*indH;
    ctx.strokeStyle=GRID; [30,70].forEach(v=>{ ctx.beginPath(); ctx.moveTo(L,yy(v)); ctx.lineTo(W-R,yy(v)); ctx.stroke(); });
    ctx.strokeStyle='#4f8cff'; ctx.lineWidth=1.3; ctx.beginPath(); let s=false;
    bars.forEach((b,i)=>{ const v=b.rsi; if(v==null){s=false;return;} const px=x(i),py=yy(v); if(!s){ctx.moveTo(px,py);s=true;} else ctx.lineTo(px,py); }); ctx.stroke();
  }
  // x 轴日期
  const step=Math.max(1,Math.floor(bars.length/8));
  for(let i=0;i<bars.length;i+=step) axisLabel(ctx,x(i),H-6,bars[i].date.slice(2),'center');
  // 图例
  let leg='<span style="color:#8b98b3">MA5</span> ';
  for(const k of ['ma5','ma10','ma20','ma60']) leg+='<span style="color:'+MA_COL[k]+'">'+k.toUpperCase()+'</span> ';
  ctx.fillStyle='#e6edf7'; ctx.font='11px sans-serif'; ctx.fillText('MA5 MA10 MA20 MA60', L+4, T+2);
  // 十字光标 (hover)
  if(hoverIdx>=0 && hoverIdx<bars.length){
    const px=x(hoverIdx), b=bars[hoverIdx];
    ctx.strokeStyle='rgba(255,255,255,.4)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(px, T); ctx.lineTo(px, H-B); ctx.stroke();
    if(b.c!=null){ const py=yP(b.c);
      ctx.beginPath(); ctx.moveTo(L, py); ctx.lineTo(W-R, py); ctx.stroke(); }
  }
  cv._bars=bars; cv._x=x; cv._info={
    lo,hi,topH,T,L,R,W,B, top:topH, volTop,volTop2:volTop+volH, indTop, indH, indBottom:indTop+indH
  };
}
function kTipText(b){
  if(!b) return '';
  const chg = (b.c!=null && b.o!=null && b.o!==0)? (b.c/b.o-1)*100 : null;
  return esc(b.date)+'  O '+fmt(b.o,2)+'  H '+fmt(b.h,2)+'  L '+fmt(b.l,2)+'  C '+fmt(b.c,2)
    +'  涨跌 '+(chg==null?'—':'<b style="color:'+(chg>=0?UP:DOWN)+'">'+fmtPct(chg)+'</b>')
    +'  量 '+fmt((b.v||0)/1e6,2)+'M  MA20 '+fmt(b.ma20,2)
    +'  DIF '+fmt(b.dif,3)+' DEA '+fmt(b.dea,3)+' MACD '+fmt(b.macd,3)+' RSI '+fmt(b.rsi,1);
}
function bindDeep(){
  const eq=document.getElementById('eqCanvas'), kv=document.getElementById('kCanvas');
  const eqTip=document.getElementById('eqTip'), kTip=document.getElementById('kTip');
  const ratio=cv=>{const r=cv.getBoundingClientRect(); return r.width?cv.width/r.width:1;};
  if(eq){
    eq.addEventListener('mousemove',e=>{
      const r=eq.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(eq);
      if(!eq._rows||!eq._rows.length) return; const rows=eq._rows, xf=eq._xf;
      const i=Math.round((rx-xf(0))/(xf(rows.length-1)-xf(0))*(rows.length-1));
      if(i<0||i>=rows.length) return; const row=rows[i];
      drawEq(ksRange, i);  // 重绘带十字
      eqTip.innerHTML='<span style="color:#4f8cff">'+esc(row.day)+'</span> 权益 '+fmt(row.equity,2)
        +'  净值 '+fmt(row.nav,4)+'  回撤 <b style="color:'+(row.dd<0?UP:AXIS)+'">'+fmt(row.dd,2)+'%</b>';
    });
    eq.addEventListener('mouseleave',()=>{ drawEq(ksRange, -1); });
  }
  if(kv){
    kv.addEventListener('mousemove',e=>{
      const r=kv.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(kv);
      const bars=kv._bars, xf=kv._x; if(!bars||!bars.length) return;
      const i=Math.round((rx-xf(0))/(xf(bars.length-1)-xf(0))*(bars.length-1));
      if(i<0||i>=bars.length) return;
      drawK(i);
      kTip.innerHTML=kTipText(bars[i]);
    });
    kv.addEventListener('mouseleave',()=>{ drawK(-1); kTip.innerHTML=''; });
  }
  const one=(id,fn)=>{const el=document.getElementById(id); if(el) el.addEventListener('click',fn);};
  one('kPrev',()=>{ ks.winStart=Math.max(0,ks.winStart-Math.round(ks.winLen*0.8)); drawK(-1); });
  one('kNext',()=>{ ks.winStart=Math.min(Math.max(0,ks.bars.length-ks.winLen),ks.winStart+Math.round(ks.winLen*0.8)); drawK(-1); });
  one('kZoomIn',()=>{ ks.winLen=Math.max(20,Math.floor(ks.winLen*0.7)); ks.winStart=Math.min(ks.winStart,Math.max(0,ks.bars.length-ks.winLen)); drawK(-1); });
  one('kZoomOut',()=>{ ks.winLen=Math.min(ks.bars.length,Math.ceil(ks.winLen*1.4)); ks.winStart=Math.min(ks.winStart,Math.max(0,ks.bars.length-ks.winLen)); drawK(-1); });
  one('kGo',()=>loadK(''));
  const kSel=document.getElementById('kSymSel'), kInd=document.getElementById('kInd');
  if(kSel) kSel.addEventListener('change',()=>loadK(kSel.value));
  if(kInd) kInd.addEventListener('change',()=>{ ks.ind=kInd.value; drawK(-1); });
  const indInput=document.getElementById('kSymInput');
  if(indInput) indInput.addEventListener('keydown',e=>{ if(e.key==='Enter') loadK(''); });
  const rangeBtns=document.querySelectorAll('#eqRange button');
  rangeBtns.forEach(b=>b.addEventListener('click',()=>{
    rangeBtns.forEach(x=>x.style.opacity=x===b?1:.5);
    ksRange=b.dataset.r; drawEq(ksRange);
  }));
  one('btnExport', exportMd);
}
async function loadHolds(){
  let st=window._lastState;
  try{
    if(!st){ st=await fetch('/api/live').then(r=>r.json()); }
    const pos=(st.positions||[]).filter(p=>p&&p.qty>0);
    const box=document.getElementById('holdPie'), bar=document.getElementById('holdBar');
    if(!pos.length){ if(box)box.innerHTML='<div class="muted">暂无持仓</div>'; if(bar)bar.innerHTML=''; return; }
    const tot=pos.reduce((a,p)=>a+(p.mv||0),0);
    if(box){
      let ang=-Math.PI/2, svg='<svg viewBox="0 0 200 200" style="max-width:210px;display:block;margin:0 auto">';
      const pal=['#4f8cff','#26d0ce','#f0b400','#c678dd','#ff6b6b','#3dd68c','#f88c4f'];
      pos.forEach((p,i)=>{
        const frac=(p.mv||0)/tot, a2=ang+frac*Math.PI*2;
        const x1=100+88*Math.cos(ang), y1=100+88*Math.sin(ang), x2=100+88*Math.cos(a2), y2=100+88*Math.sin(a2);
        svg+='<path d="M100 100 L'+x1.toFixed(1)+' '+y1.toFixed(1)+' A88 88 0 '+(frac>0.5?1:0)+' 1 '+x2.toFixed(1)+' '+y2.toFixed(1)+' Z" fill="'+pal[i%pal.length]+'" opacity=".85"><title>'+esc(p.canon)+' '+(frac*100).toFixed(1)+'%</title></path>';
        ang=a2;
      });
      svg+='</svg>';
      box.innerHTML=svg+'<div style="text-align:center;font-size:11px;color:#8b98b3;margin-top:4px">持仓市值 '+fmt(tot,0)+' (共'+pos.length+'只)</div>';
    }
    if(bar){
      pos.sort((a,b)=>(b.pnl_amt||0)-(a.pnl_amt||0));
      bar.innerHTML=pos.map(p=>{
        const w=Math.max(4,Math.min(100,Math.abs((p.pnl_amt||0)/Math.max(1,Math.max.apply(0,pos.map(x=>Math.abs(x.pnl_amt||0))))*100)));
        const c=(p.pnl_amt||0)>=0?UP:DOWN;
        return '<div style="margin:5px 0"><div style="display:flex;justify-content:space-between;font-size:12px"><span>'+esc(p.name||p.canon)+'</span><span style="color:'+c+'">'+fmt(p.pnl_amt||0,0)+' ('+fmt(p.pnl_pct||0,2)+'%)</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:6px;border-radius:3px"><div style="background:'+c+';width:'+w+'%;height:6px;border-radius:3px"></div></div></div>';
      }).join('');
    }
  }catch(e){ /* 忽略 */ }
}
async function loadProfile(){
  const indBox=document.getElementById('holdInd'), capBox=document.getElementById('holdCap');
  if(!indBox&&!capBox) return;
  const none=b=>{ if(b) b.innerHTML='<div class="muted">暂无持仓</div>'; };
  try{
    const d=await fetch('/api/holdings_profile').then(r=>r.json());
    if(!d.ok||!d.positions||!d.positions.length){ none(indBox); none(capBox); return; }
    if(indBox){
      const inds=d.industries||[];
      if(!inds.length){ indBox.innerHTML='<div class="muted">无行业映射</div>'; }
      else indBox.innerHTML=inds.map(x=>{
        return '<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:12px">'
          +'<span>'+esc(x.name)+' <span class="muted" style="font-size:11px">'+esc(x.codes.join(' '))+'</span></span>'
          +'<span>'+fmt(x.weight_pct,1)+'%</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:8px;border-radius:4px">'
          +'<div style="width:'+Math.max(2,Math.min(100,x.weight_pct))+'%;height:8px;border-radius:4px;background:#4f8cff"></div></div></div>';
      }).join('');
    }
    if(capBox){
      const caps=(d.caps||[]).filter(c=>c.count>0);
      const pal={大盘:'#c678dd',中盘:'#f0b400',小盘:'#26d0ce'};
      if(!caps.length){ capBox.innerHTML='<div class="muted">市值数据缺失(估值快照滞后)</div>'; }
      else capBox.innerHTML=caps.map(c=>{
        const sub=(c.positions||[]).map(p=>p.canon.split('.')[0]).join(' ');
        return '<div style="margin:7px 0"><div style="display:flex;justify-content:space-between;font-size:12px">'
          +'<span style="color:'+pal[c.bucket]+'">'+c.bucket+'</span><span>'+c.count+' 只 · '+fmt(c.weight_pct,1)+'%</span></div>'
          +'<div style="background:rgba(255,255,255,.08);height:8px;border-radius:4px">'
          +'<div style="width:'+Math.max(2,Math.min(100,c.weight_pct))+'%;height:8px;border-radius:4px;background:'+pal[c.bucket]+'"></div></div>'
          +'<div class="muted" style="font-size:11px">'+esc(sub)+'</div></div>';
      }).join('');
    }
  }catch(e){ const b=document.getElementById('holdInd'); if(b) b.innerHTML='<div class="muted">加载失败</div>'; }
}
async function loadMonthly(){
  const box=document.getElementById('mHeat'); if(!box)return;
  try{
    const d=await fetch('/api/monthly_returns').then(r=>r.json());
    if(!d.ok||!d.cells){ box.innerHTML='<div class="muted">回执样本不足(需跨月数据)</div>'; return; }
    const months=Object.keys(d.cells).sort();
    let html='<table class="tbl"><thead><tr><th>月份</th><th>收益</th><th>色阶</th></tr></thead><tbody>';
    months.forEach(m=>{
      const v=d.cells[m]; const abs=Math.min(2,Math.abs(v||0)/2);
      const col=v>=0?'rgba(255,80,80,'+(0.15+abs*0.75)+')':'rgba(80,220,140,'+(0.15+abs*0.75)+')';
      html+='<tr><td>'+esc(m)+'</td><td style="color:'+(v>=0?UP:DOWN)+'">'+fmtPct(v)+'</td>'
        +'<td style="background:'+col+'"></td></tr>';
    });
    html+='</tbody></table>';
    box.innerHTML=html;
  }catch(e){ box.innerHTML='<div class="muted">加载失败</div>'; }
}
async function exportMd(){
  try{
    const [live,perf,ov]=await Promise.all([
      fetch('/api/live').then(r=>r.json()).catch(()=>null),
      fetch('/api/perf').then(r=>r.json()).catch(()=>null),
      fetch('/api/overview').then(r=>r.json()).catch(()=>null)]);
    const C=(live&&live.capital)||{};
    const rows=(eqCache&&eqCache.rows)||[];
    const nav=(rows.length?rows[rows.length-1].nav:null);
    const dds=rows.map(r=>r.dd||0);
    const md=[];
    md.push('# A股轮动 · 模拟盘复盘', '');
    md.push('生成时间: '+new Date().toLocaleString('zh-CN'), '');
    md.push('## 账户概览');
    md.push('| 权益 | 现金 | 仓位 | 持仓 | 累计收益 |');
    md.push('|---|---|---|---|---|');
    md.push('| '+fmt(C.equity,2)+' | '+fmt(C.cash,2)+' | '+(C.cash_ratio!=null?fmt((1-C.cash_ratio)*100,1)+'%':'—')+' | '+(C.open_positions||0)+' 只 | '+(C.total_pnl_pct!=null?fmtPct(C.total_pnl_pct):'—')+' |','');
    md.push('## 持仓');
    const pos=(live&&live.positions||[]).filter(p=>p&&p.qty>0);
    if(pos.length){
      md.push('| 代码 | 数量 | 成本 | 现价 | 浮盈 | 仓位 |');
      md.push('|---|---|---|---|---|---|');
      pos.forEach(p=>md.push('| '+esc(p.canon)+' | '+p.qty+' | '+fmt(p.avg_cost,3)+' | '+fmt(p.last_price,2)+' | '+fmt(p.pnl_amt,1)+' ('+fmt(p.pnl_pct,2)+'%) | '+fmt((p.weight||0)*100,1)+'% |'));
    } else md.push('(空仓)');
    md.push('','## 绩效');
    if(perf&&perf.ok!==false&&perf.metrics){
      const m=perf.metrics;
      md.push('| 累计收益 | 年化 | 夏普 | 最大回撤 |');
      md.push('|---|---|---|---|');
      md.push('| '+(m.total_return!=null?fmtPct(m.total_return):'—')+' | '+(m.cagr!=null?fmt(m.cagr,2)+'%':'—')+' | '+(m.sharpe_annual!=null?fmt(m.sharpe_annual,2):'—')+' | '+(m.max_drawdown!=null?fmt(m.max_drawdown,2)+'%':'—')+' |');
    } else {
      md.push('最新净值: '+(nav?fmt(nav,4):'—')+' | 当前回撤: '+fmtPct(dds.length?dds[dds.length-1]:0));
    }
    md.push('','## 门控与系统');
    const g=(ov&&ov.gate)||{};
    md.push('- IC 门控: '+(g.regime||'—')+' | 暴露 ×'+(g.exposure_mult!=null?g.exposure_mult:1)+' | IC均值 '+fmt(g.ic_mean,4)+(g.ic_as_of?' (as_of '+esc(g.ic_as_of)+')':''));
    const dt=(ov&&ov.data)||{};
    if(dt.stale) md.push('- 数据滞后表: '+(dt.stale.length?dt.stale.join(', '):'无'));
    if(g.reasons&&g.reasons.length) md.push('- 门控原因: '+g.reasons.join('; '));
    md.push('','*本报告由系统自动生成, 仅供研究复盘, 不构成投资建议.*');
    const blob=new Blob(['\ufeff'+md.join('\n')],{type:'text/markdown;charset=utf-8'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download='复盘_'+new Date().toISOString().slice(0,10)+'.md';
    document.body.appendChild(a); a.click(); setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},300);
  }catch(e){ if(typeof console!=='undefined') console.error('export err',e); }
}
function initDeep(){
  bindDeep();
  loadEq(); loadMonthly(); loadHolds(); loadProfile();
  // K线初始标的: 持仓第一只, 否则固定示例
  const st=window._lastState||{};
  const opts=(st.positions||[]).filter(p=>p.qty>0).map(p=>p.canon);
  const tgt=(st.targets||st.top_targets||[]).map(t=>typeof t==='string'?t:(t&&t.canon));
  const all=[...new Set([...(opts||[]),...(tgt||[]).filter(Boolean)])].slice(0,12);
  const sel=document.getElementById('kSymSel');
  if(sel){
    sel.innerHTML=all.map(s=>'<option value="'+esc(s)+'">'+esc(s)+'</option>').join('')
      +'<option value="600519">600519 贵州茅台</option><option value="000001">000001 平安银行</option>';
    if(!all.length) sel.innerHTML='<option value="600519">600519 贵州茅台</option>';
  }
  loadK(all[0]||'600519');
}
// ===================== 风控告警 (riskview) =====================
let rvLoaded=false, _pushBound=false;
function kpiCard(label, val, color){
  return '<div style="background:hsl(var(--elevated));border:1px solid hsl(var(--border));border-radius:var(--radius-sm);padding:10px 12px">'
    +'<div class="muted" style="font-size:11px;margin-bottom:3px">'+esc(label)+'</div>'
    +'<div style="font-size:19px;font-weight:600;color:'+(color||'#e6edf7')+'">'+val+'</div></div>';
}
async function loadRiskview(){
  const box=document.getElementById('riskKpis'); if(!box) return;
  try{
    const [rk,al]=await Promise.all([
      fetch('/api/riskops').then(r=>r.json()),
      fetch('/api/alerts').then(r=>r.json())]);
    const m=rk.metrics||{};
    const fmtp=v=>v==null?'—':fmtPct(v);
    const fmtn=v=>v==null?'—':fmt(v,2);
    const up=v=>v!=null&&v>=0?'#3dd68c':'#ff6b6b';
    let html='';
    html+=kpiCard('今日盈亏', fmtp(m.daily_pnl_pct), up(m.daily_pnl_pct));
    html+=kpiCard('今日盈亏额', m.daily_pnl!=null?fmt(m.daily_pnl,0):'—', up(m.daily_pnl));
    html+=kpiCard('VaR95 (日)', fmtp(m.var95), '#f0b400');
    html+=kpiCard('VaR99 (日)', fmtp(m.var99), '#ff6b6b');
    html+=kpiCard('滚动Sharpe(60日)', fmtn(m.sharpe_w60), m.sharpe_w60!=null&&m.sharpe_w60>=0?'#3dd68c':'#ff6b6b');
    html+=kpiCard('Sharpe(近20日)', fmtn(m.sharpe_w20), m.sharpe_w20!=null&&m.sharpe_w20>=0?'#3dd68c':'#ff6b6b');
    html+=kpiCard('Sortino(年化)', fmtn(m.sortino), '#e6edf7');
    html+=kpiCard('年化波动', m.vol_annual!=null?fmt(m.vol_annual,2)+'%':'—', '#e6edf7');
    html+=kpiCard('最大回撤', m.max_dd!=null?fmt(m.max_dd,2)+'%':'—', '#ff6b6b');
    html+=kpiCard('胜率', m.win_rate!=null?fmt(m.win_rate,1)+'%':'—', '#e6edf7');
    html+=kpiCard('盈亏比', fmtn(m.pl_ratio), '#e6edf7');
    const op=rk.ops||{};
    html+=kpiCard('错误计数(24h)', op.err_24h!=null?op.err_24h:'—', (op.err_24h||0)>0?'#ff6b6b':'#3dd68c');
    const tt=op.today_trades||{};
    html+=kpiCard('今日成交', (tt.buy!=null?('买'+tt.buy+' 卖'+tt.sell):'—')+(tt.fee?' (费'+tt.fee+')':''), '#e6edf7');
    box.innerHTML=html;
    const note=document.getElementById('riskNote');
    if(note) note.innerHTML=(rk.note?('⚠ '+esc(rk.note)+' '):'')+'(回执 '+(rk.n_days||0)+' 个交易日)';
    // 告警
    const ab=document.getElementById('alertBox'); if(ab){
      const al_=al.alerts||[];
      if(!al_.length){ ab.innerHTML='<div style="color:#3dd68c">● 无活跃告警 · 系统运行正常</div>'; }
      else{
        ab.innerHTML=al_.map(a=>{
          const c=a.level==='critical'?'#ff6b6b':(a.level==='warn'?'#f0b400':'#e6edf7');
          return '<div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px dashed rgba(255,255,255,.08);align-items:center">'
            +'<span style="color:'+c+';flex:0 0 52px;font-size:11px">['+a.level.toUpperCase()+']</span>'
            +'<b style="flex:0 0 130px">'+esc(a.rule)+'</b><span class="muted" style="flex:1">'+esc(a.detail)+'</span>'
            +'<span class="muted" style="font-size:11px">'+esc(a.ts)+'</span></div>';
        }).join('');
      }
    }
    window._alertsNow=al;
  }catch(e){ box.innerHTML='<div class="muted">加载失败: '+esc(String(e).slice(0,80))+'</div>'; }
}
async function sendPush(channel, msg){
  const box=document.getElementById('pushResult'); if(box) box.innerHTML='发送中...';
  try{
    const r=await fetch('/api/push',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({channel:channel,message:msg})}).then(r=>r.json());
    if(box) box.innerHTML=r.ok?('✅ 已发送 ('+(r.status||r.body||'')+')')
      :('<b style="color:#f66">发送失败:</b> '+esc(r.error||''));
  }catch(e){ if(box) box.innerHTML='<b style="color:#f66">请求失败:</b> '+esc(String(e).slice(0,120)); }
}
function bindPush(){
  if(_pushBound) return; _pushBound=true;
  const ch=()=>{const s=document.getElementById('pushChannel'); return s?s.value:'dingtalk';};
  const bt=document.getElementById('btnPushTest'), bn=document.getElementById('btnPushNow');
  if(bt) bt.addEventListener('click',()=>sendPush(ch(),'【测试】A股轮动仪表台告警通道测试 '+new Date().toLocaleString('zh-CN')));
  if(bn) bn.addEventListener('click',()=>{
    const al=(window._alertsNow&&window._alertsNow.alerts)||[];
    const msg = al.length?('【A股轮动告警】当前 '+al.length+' 条:\n'+al.map(a=>'['+a.level+'] '+a.rule+' - '+a.detail).join('\n'))
      :('【A股轮动】当前无活跃告警, 系统运行正常。');
    sendPush(ch(), msg);
  });
}
// ===================== 因子实验室 (flab) =====================
let flLoaded=false, flCache=null, flHidden={}, flRange=120, flHorizon='h5';
const FCOL={vol:'#f0b400',mom_20:'#4f8cff',reversal:'#c678dd'};
async function loadF(){
  const cv=document.getElementById('flabCanvas'); if(!cv) return;
  try{
    const d=await fetch('/api/factor_lab?days='+flRange).then(r=>r.json());
    flCache=d;
    // stats 文本 + legend
    const st=document.getElementById('flabStats');
    if(st){
      let html='';
      (d.factors||[]).forEach(f=>{
        const s=(f.stats||{})[flHorizon]||{};
        html+='<span style="font-size:12px;cursor:pointer;opacity:'+(flHidden[f.key]?'.35':'1')+'" data-f="'+f.key+'" class="flLeg" '
          +'style2="">'
          +'<b style="color:'+FCOL[f.key]+'">'+esc(f.zh)+'</b> '
          +'近IC '+(s.last!=null?fmt(s.last,3):'—')+' | 均值 '+((s.mean!=null)?fmt(s.mean,3):'—')
          +' | 胜率 '+((s.win!=null)?fmt(s.win,0)+'%':'—')+'</span>';
      });
      st.innerHTML=html;
      st.querySelectorAll('.flLeg').forEach(el=>el.addEventListener('click',()=>{
        const k=el.dataset.f; flHidden[k]=!flHidden[k]; loadF(); }));
    }
    // quantiles
    const qb=document.getElementById('flabQ');
    if(qb){
      const qs=d.quantiles||[];
      if(!qs.length) qb.innerHTML='<div class="muted">五分位数据不可用(视图最新日)</div>';
      else{
        let h='<table class="tbl"><thead><tr><th>因子</th><th>Q1(低)</th><th>Q2</th><th>Q3</th><th>Q4</th><th>Q5(高)</th></tr></thead><tbody>';
        qs.forEach(f=>{
          const cells=[0,1,2,3,4].map(qi=>{
            const b=f.bins.find(x=>x.q===qi);
            if(!b) return '<td>—</td>';
            const col=b.mean>=0?'rgba(61,214,140,'+(0.15+Math.min(.55,Math.abs(b.mean))*0.5)+')'
                            :'rgba(255,107,107,'+(0.15+Math.min(.55,Math.abs(b.mean))*0.5)+')';
            return '<td style="background:'+col+'">'+fmt(b.mean,2)+'<br><span class="muted" style="font-size:10px">n='+b.n+' ['+fmt(b.lo,2)+','+fmt(b.hi,2)+']</span></td>';
          });
          h+='<tr><td>'+esc(f.zh)+'<br><span class="muted" style="font-size:10px">'+esc(String(f.date))+'</span></td>'+cells.join('')+'</tr>';
        });
        h+='</tbody></table>';
        qb.innerHTML=h;
      }
    }
    drawF(-1);
  }catch(e){ const t=document.getElementById('flabTip'); if(t)t.textContent='因子数据加载失败: '+e; }
}
function drawF(hoverIdx){
  const cv=document.getElementById('flabCanvas'); if(!cv||!flCache) return;
  const ctx=cv.getContext('2d'); const W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  const L=70,R=16,T=14,B=22;
  const data=[];
  (flCache.factors||[]).forEach(f=>{ if(!flHidden[f.key]) data.push({f:f,h:flHorizon}); });
  if(!data.length){ return; }
  const all=[];
  data.forEach(d=>d.f.series.forEach(s=>{const v=s[d.h]; if(v!=null) all.push(v);}));
  if(!all.length) return;
  let lo=Math.min.apply(0,all), hi=Math.max.apply(0,all); const pad=(hi-lo)*.1||.05; lo-=pad; hi+=pad;
  const series=data[0].f.series;
  const x=i=>L+i*(W-L-R)/(series.length-1), y=v=>T+(1-(v-lo)/(hi-lo))*(H-B-T);
  ctx.strokeStyle=GRID; [0].forEach(v=>{ const yy=y(v); ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke(); });
  for(let i=0;i<=5;i++){ const v=lo+(hi-lo)*i/5; const yy=y(v);
    ctx.strokeStyle=GRID; ctx.beginPath(); ctx.moveTo(L,yy); ctx.lineTo(W-R,yy); ctx.stroke();
    axisLabel(ctx,L-6,yy+3,v.toFixed(2),'right'); }
  const step=Math.max(1,Math.floor(series.length/8));
  for(let i=0;i<series.length;i+=step) axisLabel(ctx,x(i),H-4,series[i].d.slice(2),'center');
  data.forEach(d=>{
    const col=FCOL[d.f.key]||'#4f8cff'; ctx.strokeStyle=col; ctx.lineWidth=1.4; ctx.beginPath();
    let started=false;
    d.f.series.forEach((s,i)=>{ const v=s[d.h]; if(v==null){started=false;return;}
      const px=x(i),py=y(v); if(!started){ctx.moveTo(px,py);started=true;} else ctx.lineTo(px,py); });
    ctx.stroke();
  });
  ctx.fillStyle='#e6edf7'; ctx.font='11px sans-serif';
  ctx.fillText(data.map(d=>d.f.zh+'('+d.h.toUpperCase()+')').join('  '), L+4, T+2);
  if(hoverIdx>=0 && hoverIdx<series.length){
    const px=x(hoverIdx);
    ctx.strokeStyle='rgba(255,255,255,.4)'; ctx.beginPath(); ctx.moveTo(px,T); ctx.lineTo(px,H-B); ctx.stroke();
  }
  cv._fs=series; cv._fx=x; cv._fd=data;
}
function flTipText(i){
  if(!flCache) return '';
  const series=flCache.factors[0].series; if(i<0||i>=series.length) return '';
  let t='<span style="color:#4f8cff">'+esc(series[i].d)+'</span>';
  flCache.factors.forEach(f=>{ const s=f.series[i]; const v=s?s[flHorizon]:null;
    if(v!=null) t+='  <span style="color:'+FCOL[f.key]+'">'+esc(f.zh)+' '+fmt(v,4)+'</span>'; });
  return t;
}
function bindFlab(){
  const cv=document.getElementById('flabCanvas'), tip=document.getElementById('flabTip');
  const ratio=cv2=>{const r=cv2.getBoundingClientRect(); return r.width?cv2.width/r.width:1;};
  if(cv) cv.addEventListener('mousemove',e=>{
    const r=cv.getBoundingClientRect(), rx=(e.clientX-r.left)*ratio(cv);
    const s=cv._fs, xf=cv._fx; if(!s||!s.length) return;
    const i=Math.round((rx-xf(0))/(xf(s.length-1)-xf(0))*(s.length-1));
    drawF(i); if(tip) tip.innerHTML=flTipText(i);
  });
  const hz=document.getElementById('flabHorizon'), rg=document.getElementById('flabRange');
  if(hz) hz.addEventListener('change',()=>{ flHorizon=hz.value; loadF(); });
  if(rg) rg.addEventListener('change',()=>{ flRange=parseInt(rg.value,10)||120; loadF(); });
}
function initRiskview(){ bindPush(); loadRiskview(); setInterval(loadRiskview,15000); }
function initF(){ bindFlab(); loadF(); }
function switchTab(name){
  document.querySelectorAll('.tabBtn').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.tabView').forEach(v=>v.classList.toggle('active', v.id==='view-'+name));
  if(name==='market' && !mkLoaded){ mkLoaded=true; loadMarket(); }
  if(name==='perf' && !pfLoaded){ pfLoaded=true; loadPerf(); loadTargetPlan(); loadHealth(); loadViews(); }
  if(name==='dashboard' && !mbLoaded){ mbLoaded=true; loadMarketBoard(); }
  if(name==='backtest' && !btLoaded){ btLoaded=true; loadBacktestHistory(); }
  if(name==='concept' && !cptLoaded){
    cptLoaded=true;
    // 绑定控件事件 (仅首次)
    const cSearch = document.getElementById('conceptSearch');
    const cSort = document.getElementById('conceptSort');
    const cTop = document.getElementById('conceptTop');
    const cRef = document.getElementById('conceptRefresh');
    if(cSearch){ cSearch.addEventListener('input', debounce(loadConcept, 300)); }
    if(cSort){ cSort.addEventListener('change', loadConcept); }
    if(cTop){ cTop.addEventListener('change', loadConcept); }
    if(cRef){ cRef.addEventListener('click', loadConcept); }
    loadConcept();
  }
  if(name==='industry' && !indLoaded){
    indLoaded=true;
    const iSearch = document.getElementById('industrySearch');
    const iSort = document.getElementById('industrySort');
    const iTop = document.getElementById('industryTop');
    const iRef = document.getElementById('industryRefresh');
    if(iSearch){ iSearch.addEventListener('input', debounce(loadIndustry, 300)); }
    if(iSort){ iSort.addEventListener('change', loadIndustry); }
    if(iTop){ iTop.addEventListener('change', loadIndustry); }
    if(iRef){ iRef.addEventListener('click', loadIndustry); }
    loadIndustry();
  }
  if(name==='monitor' && !monLoaded){ monLoaded=true; loadMonitor(); }
  if(name==='regime' && !regLoaded){ regLoaded=true; loadRegime(); }
  if(name==='abnormal' && !abnLoaded){ abnLoaded=true; loadAbnormal(); }
  if(name==='dbpanel' && !dbpLoaded){ dbpLoaded=true; loadDbPanel(); }
  if(name==='deep' && !deepLoaded){ deepLoaded=true; initDeep(); }
  if(name==='riskview' && !rvLoaded){ rvLoaded=true; initRiskview(); }
  if(name==='flab' && !flLoaded){ flLoaded=true; initF(); }
  if(name==='pscan' && !psLoaded){ psLoaded=true; initScan(); }
}
document.querySelectorAll('.tabBtn').forEach(b=>b.addEventListener('click',()=>switchTab(b.dataset.tab)));

// ===================== 8 个新面板渲染函数 =====================

// ---- 回测 ----
async function loadBacktestHistory(){
  try{ const d = await fetch('/api/backtest/history').then(r=>r.json()); renderBacktestHistory(d); }
  catch(e){ const _b = document.getElementById('btHistoryBody'); if(_b) _b.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderBacktestHistory(d){
  const body = $('#btHistoryBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const rows = d.rows || [];
  if(!rows.length){ body.innerHTML='<div class="muted">暂无回测历史</div>'; return; }
  let html = `<div class="muted" style="margin-bottom:8px;font-size:12px">共 ${rows.length} 条记录</div>`;
  html += '<table class="tbl"><thead><tr><th>来源</th><th>日</th><th>标签</th><th>总收益</th><th>年化Sharpe</th><th>最大回撤</th><th>起始</th><th>结束</th><th>交易日</th><th>交易笔数</th></tr></thead><tbody>';
  rows.forEach(r=>{
    // 容错: 数据来源有三种 schema
    //   A. backtest_latest.json -> 扁平字段 total_return / total_trades / trade_days
    //   B. vnpy_backtest/summary.json -> 嵌套 stats.* (annual_return, sharpe_ratio 等)
    //   C. performance_report.json -> 嵌套 metrics.* + period.*
    const stats = r.stats || {};
    const m = r.metrics || {};
    const period = r.period || {};
    // 总收益: 自动判断是百分比还是小数 (|x|>1 -> 已%)
    let totalRet = r.total_return;
    if(totalRet==null) totalRet = stats.annual_return != null ? stats.annual_return : m.total_return;
    function _toPct(v){ if(v==null) return null; return Math.abs(v)>1 ? v : v*100; }
    totalRet = _toPct(totalRet);
    // Sharpe: vnpy sharpe_ratio; 其它 sharpe_annual
    const sharpe = stats.sharpe_ratio != null ? stats.sharpe_ratio : m.sharpe_annual;
    // 最大回撤: vnpy max_ddpercent (已%); 其它需 *100 (自动适配)
    let maxDd = stats.max_ddpercent != null ? stats.max_ddpercent : (r.max_drawdown_pct != null ? r.max_drawdown_pct : (m.max_drawdown != null ? m.max_drawdown*100 : null));
    maxDd = _toPct(maxDd);
    // 交易日 / 笔数
    const tradeDays = stats.total_days != null ? stats.total_days : r.trade_days;
    const totalTrades = r.total_trades != null ? r.total_trades : (stats.total_trade_count != null ? stats.total_trade_count : m.trade_count);
    // 起止日期
    const startD = r.start || period.start || stats.start_date;
    const endD = r.end || period.end || stats.end_date;
    // 来源
    const src = (r._source||'').replace(/^.*[\\/]/,'');
    html += `<tr>
      <td><code style="font-size:10px">${esc(src)}</code></td>
      <td>${esc(r._day||'')}</td>
      <td>${esc(r.tag||'')}</td>
      <td class="${(totalRet||0)>=0?'up':'down'}">${totalRet!=null?fmt(totalRet,2)+'%':'—'}</td>
      <td>${sharpe!=null?fmt(sharpe,3):'—'}</td>
      <td class="down">${maxDd!=null?fmt(maxDd,2)+'%':'—'}</td>
      <td>${esc(startD||'')}</td>
      <td>${esc(endD||'')}</td>
      <td>${tradeDays||''}</td>
      <td>${totalTrades||''}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  body.innerHTML = html;
}

// ---- 概念分析 ----
function debounce(fn, ms){ let t; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a), ms); }; }
let conceptLastData = null;
async function loadConcept(){
  const body = $('#conceptBody');
  body.innerHTML = '<div class="muted">加载中...</div>';
  const tag = $('#conceptSearch') ? $('#conceptSearch').value.trim() : '';
  const sort = $('#conceptSort') ? $('#conceptSort').value : 'strength';
  const top  = $('#conceptTop') ? $('#conceptTop').value : '50';
  try{
    const u = `/api/concept?top=${top}&sort=${encodeURIComponent(sort)}${tag?`&tag=${encodeURIComponent(tag)}`:''}`;
    const d = await fetch(u).then(r=>r.json());
    conceptLastData = d;
    renderConcept(d);
  }catch(e){ body.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderConcept(d){
  const body = $('#conceptBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const items = d.items || [];
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    源: ${esc(d.source||'')} · ext_gn_ths 抓取: ${esc(d.fetched_at||'')} ·
    共 <b style="color:#fff">${d.total_concepts||items.length}</b> 个概念 · 覆盖 ${d.total_symbols||0} 只标的 · 排序: ${esc(d.sort_by||'')}${d.tag_filter?` · 过滤: ${esc(d.tag_filter)}`:''}
  </div>`;
  if(!items.length){ html += '<div class="muted">无匹配概念</div>'; body.innerHTML = html; return; }
  html += `<div style="overflow-x:auto"><table class="tbl" style="white-space:nowrap">
    <thead><tr>
      <th>概念名 (点击展开)</th>
      <th>成分数</th>
      <th>当日参与</th>
      <th>涨停</th>
      <th>强势</th>
      <th>平均涨幅%</th>
      <th>中位涨幅%</th>
      <th>总成交额(亿)</th>
      <th>平均换手%</th>
      <th>热度</th>
      <th>前 3 龙头</th>
    </tr></thead><tbody id="conceptTbody">`;
  items.forEach(it=>{
    const leaders = (it.leaders || []).map(l => {
      const cls = (l.change_pct||0) >= 0 ? 'up' : 'down';
      return `<span class="badge" style="font-size:10px;margin-right:4px" title="${esc(l.name||'')}">${esc(l.symbol)} <span class="${cls}">${fmt(l.change_pct,2)}%</span></span>`;
    }).join('');
    const cls = (it.avg_change_pct||0) >= 0 ? 'up' : 'down';
    const sign = (it.avg_change_pct||0) >= 0 ? '+' : '';
    html += `<tr data-name="${esc(it.name)}" style="cursor:pointer">
      <td><b style="color:#fff">${esc(it.name)}</b></td>
      <td>${it.count}</td>
      <td>${it.today||0}</td>
      <td class="up">${it.limit_up_count||0}</td>
      <td class="up">${it.strong_up_count||0}</td>
      <td class="${cls}">${it.avg_change_pct!=null?sign+fmt(it.avg_change_pct,2):'—'}</td>
      <td class="${cls}">${it.median_change_pct!=null?sign+fmt(it.median_change_pct,2):'—'}</td>
      <td>${fmt((it.total_amount||0)/1e8, 2)}</td>
      <td>${it.avg_turnover!=null?fmt(it.avg_turnover,2):'—'}</td>
      <td><b style="color:#fff">${fmt(it.heat_score,1)}</b></td>
      <td>${leaders||'<span class="muted">—</span>'}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  body.innerHTML = html;
  // 行点击展开详情
  body.querySelectorAll('#conceptTbody tr').forEach(tr=>{
    tr.addEventListener('click', ()=>{
      const name = tr.dataset.name;
      showConceptDetail(name);
    });
  });
}
async function showConceptDetail(name){
  const det = $('#conceptDetail');
  det.innerHTML = '<div class="muted">查询 '+esc(name)+' 成分股...</div>';
  try{
    const r = await fetch(`/api/concept?tag=${encodeURIComponent(name)}&top=1`).then(r=>r.json());
    const item = (r.items||[]).find(x=>x.name===name) || (r.items||[])[0];
    if(!item){ det.innerHTML = '<span class="empty">无详情</span>'; return; }
    const symRes = await fetch(`/api/concept/symbols?name=${encodeURIComponent(name)}`).then(r=>r.json());
    const symbols = symRes.symbols || [];
    const related = symRes.related_tags || [];
    det.innerHTML = `<div class="panel" style="margin-top:6px;background:rgba(255,255,255,.02)">
      <h4 style="color:#fff">${esc(name)} <span class="muted" style="font-weight:400;font-size:12px">成分股 ${symbols.length} 只 · 平均 ${fmt(item.avg_change_pct||0,2)}% · 成交 ${fmt((item.total_amount||0)/1e8,2)} 亿</span></h4>
      <div style="font-family:monospace;font-size:11px;color:#0f0;max-height:200px;overflow-y:auto">${symbols.slice(0,300).join(' · ')}</div>
      ${related.length?`<div style="margin-top:6px;font-size:11px" class="muted">相关标签: ${esc(related.slice(0,8).join(' · '))}</div>`:''}
    </div>`;
  }catch(e){ det.innerHTML = '<span class="empty">查询失败: '+e+'</span>'; }
}

// ---- 行业分析 ----
let industryLastData = null;
async function loadIndustry(){
  const body = $('#industryBody');
  body.innerHTML = '<div class="muted">加载中...</div>';
  const tag = $('#industrySearch') ? $('#industrySearch').value.trim() : '';
  const sort = $('#industrySort') ? $('#industrySort').value : 'strength';
  const top  = $('#industryTop') ? $('#industryTop').value : '50';
  try{
    const u = `/api/industry?top=${top}&sort=${encodeURIComponent(sort)}${tag?`&tag=${encodeURIComponent(tag)}`:''}`;
    const d = await fetch(u).then(r=>r.json());
    industryLastData = d;
    renderIndustry(d);
  }catch(e){ body.innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderIndustry(d){
  const body = $('#industryBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const items = d.items || [];
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    源: ${esc(d.source||'')} · ext_hy_ths 抓取: ${esc(d.fetched_at||'')} ·
    共 <b style="color:#fff">${d.total_industries||items.length}</b> 个行业(含一级-二级-三级) · 覆盖 ${d.total_symbols||0} 只标的 · 排序: ${esc(d.sort_by||'')}${d.tag_filter?` · 过滤: ${esc(d.tag_filter)}`:''}
  </div>`;
  if(!items.length){ html += '<div class="muted">无匹配行业</div>'; body.innerHTML = html; return; }
  html += `<div style="overflow-x:auto"><table class="tbl" style="white-space:nowrap">
    <thead><tr>
      <th>行业 (一级-二级-三级, 点击展开)</th>
      <th>成分数</th>
      <th>当日参与</th>
      <th>涨停</th>
      <th>强势</th>
      <th>平均涨幅%</th>
      <th>中位涨幅%</th>
      <th>总成交额(亿)</th>
      <th>平均换手%</th>
      <th>热度</th>
      <th>前 3 龙头</th>
    </tr></thead><tbody id="industryTbody">`;
  items.forEach(it=>{
    const leaders = (it.leaders || []).map(l => {
      const cls = (l.change_pct||0) >= 0 ? 'up' : 'down';
      return `<span class="badge" style="font-size:10px;margin-right:4px" title="${esc(l.name||'')}">${esc(l.symbol)} <span class="${cls}">${fmt(l.change_pct,2)}%</span></span>`;
    }).join('');
    const cls = (it.avg_change_pct||0) >= 0 ? 'up' : 'down';
    const sign = (it.avg_change_pct||0) >= 0 ? '+' : '';
    html += `<tr data-name="${esc(it.name)}" style="cursor:pointer">
      <td><b style="color:#fff">${esc(it.name)}</b></td>
      <td>${it.count}</td>
      <td>${it.today||0}</td>
      <td class="up">${it.limit_up_count||0}</td>
      <td class="up">${it.strong_up_count||0}</td>
      <td class="${cls}">${it.avg_change_pct!=null?sign+fmt(it.avg_change_pct,2):'—'}</td>
      <td class="${cls}">${it.median_change_pct!=null?sign+fmt(it.median_change_pct,2):'—'}</td>
      <td>${fmt((it.total_amount||0)/1e8, 2)}</td>
      <td>${it.avg_turnover!=null?fmt(it.avg_turnover,2):'—'}</td>
      <td><b style="color:#fff">${fmt(it.heat_score,1)}</b></td>
      <td>${leaders||'<span class="muted">—</span>'}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  body.innerHTML = html;
  body.querySelectorAll('#industryTbody tr').forEach(tr=>{
    tr.addEventListener('click', ()=>{
      const name = tr.dataset.name;
      showIndustryDetail(name);
    });
  });
}
async function showIndustryDetail(name){
  const det = $('#industryDetail');
  det.innerHTML = '<div class="muted">查询 '+esc(name)+' 成分股...</div>';
  try{
    const r = await fetch(`/api/industry?tag=${encodeURIComponent(name)}&top=1`).then(r=>r.json());
    const item = (r.items||[]).find(x=>x.name===name) || (r.items||[])[0];
    if(!item){ det.innerHTML = '<span class="empty">无详情</span>'; return; }
    // 反查 symbol: 利用 concept/symbol API 按symbol查 (不可, 这里逐symbol查太慢 -> 用后端新增 endpoint 更稳)
    const symbols = await fetchIndustrySymbols(name);
    det.innerHTML = `<div class="panel" style="margin-top:6px;background:rgba(255,255,255,.02)">
      <h4 style="color:#fff">${esc(name)} <span class="muted" style="font-weight:400;font-size:12px">成分股 ${symbols.length} 只 · 平均 ${fmt(item.avg_change_pct||0,2)}% · 成交 ${fmt((item.total_amount||0)/1e8,2)} 亿</span></h4>
      <div style="font-family:monospace;font-size:11px;color:#0f0;max-height:200px;overflow-y:auto">${symbols.slice(0,300).join(' · ')}</div>
    </div>`;
  }catch(e){ det.innerHTML = '<span class="empty">查询失败: '+e+'</span>'; }
}
async function fetchIndustrySymbols(name){
  // 通过新增后端路由直接拿 symbol 列表 (按 name 过滤)
  try{
    const j = await fetch(`/api/industry/symbols?name=${encodeURIComponent(name)}`).then(r=>r.json());
    return j.symbols || [];
  }catch(_){ return []; }
}

// ---- 监控中心 ----
async function loadMonitor(){
  try{ const d = await fetch('/api/monitor').then(r=>r.json()); renderMonitor(d); }
  catch(e){ $('#monitorBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderMonitor(d){
  const body = $('#monitorBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px">';
  // 1) 盘前健康
  html += '<div class="card"><div class="lbl">盘前健康检查</div>';
  const h = c.health || {};
  if(h.ok !== undefined){
    const cls = h.level === 'OK' ? 'up' : (h.level === 'DEGRADED' ? 'warn' : 'down');
    html += `<div class="val ${cls}">${esc(h.level||'?')}</div><div class="hint">${esc(h.summary||'')}</div>`;
    if(h.checks){
      html += '<div style="margin-top:6px;font-size:11px">';
      h.checks.forEach(ch=>{ html += `<span class="badge ${ch.status==='OK'?'up':(ch.status==='WARN'?'warn':'down')}" style="margin:1px">${esc(ch.name)}=${esc(ch.status)}</span>`; });
      html += '</div>';
    }
  } else { html += '<div class="hint">'+esc(h.error||'未生成')+'</div>'; }
  html += '</div>';
  // 2) 策略退化
  html += '<div class="card"><div class="lbl">策略退化指数</div>';
  const dg = c.degradation || {};
  if(dg.index){
    html += `<div class="val">${fmt(dg.index.overall_score,1)}</div>
      <div class="hint">最差维度: <span class="${dg.index.worst_level==='P0'?'down':(dg.index.worst_level==='P1'?'warn':'')}">${esc(dg.index.worst_level||'-')}</span></div>`;
  } else { html += '<div class="hint">'+esc(dg.error||'未生成')+'</div>'; }
  html += '</div>';
  // 3) reward config
  html += '<div class="card"><div class="lbl">增量学习奖励权重</div>';
  const rc = c.reward_config || {};
  if(rc.vnpy_weight !== undefined){
    html += `<div class="val">vnpy ${fmt(rc.vnpy_weight,2)} / ic ${fmt(rc.ic_weight,2)}</div>
      <div class="hint">来源: ${esc(rc.source||'-')}</div>`;
  } else { html += '<div class="hint">'+esc(rc.error||'默认 0.6/0.4')+'</div>'; }
  html += '</div>';
  // 4) DRL plan ready
  html += '<div class="card"><div class="lbl">DRL 目标计划</div>';
  const dp = c.drl_plan || {};
  if(dp.ok){
    const p = dp.data || {};
    html += `<div class="val up">READY</div>
      <div class="hint">top ${p.top_n?p.top_n.length:0} · universe ${p.universe_size||0} · ${esc((p.generated_at||'').slice(0,16))}</div>`;
  } else { html += '<div class="val down">NOT READY</div><div class="hint">'+esc(dp.error||'')+'</div>'; }
  html += '</div>';
  // 5) daemon
  html += '<div class="card"><div class="lbl">守护进程</div>';
  const dm = c.daemon || {};
  if(dm.running_day){
    html += `<div class="val">${esc(dm.running_day||'-')}</div>
      <div class="hint">engine_pid: ${esc(String(dm.engine_pid||'-'))} · last_close: ${esc(dm.last_close_day||'-')}</div>`;
  } else { html += '<div class="hint">'+esc(dm.error||'未启动')+'</div>'; }
  html += '</div>';
  html += '</div>';
  body.innerHTML = html;
}

// ---- 市场环境 ----
async function loadRegime(){
  try{ const d = await fetch('/api/regime').then(r=>r.json()); renderRegime(d); }
  catch(e){ $('#regimeBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderRegime(d){
  const body = $('#regimeBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  let html = '';
  // 阶段
  if(c.phase){
    const ph = c.phase;
    html += '<div class="card" style="margin-bottom:14px"><div class="lbl">当前市场阶段</div>';
    const cls = ph.phase.indexOf('上行')>=0 || ph.phase.indexOf('偏多')>=0 ? 'up' : (ph.phase.indexOf('下行')>=0 || ph.phase.indexOf('偏空')>=0 ? 'down' : 'warn');
    html += `<div class="val ${cls}">${esc(ph.phase)}</div>
      <div class="hint">基于 ${esc(ph.based_on||'-')} · 宽度比 ${fmt(ph.breadth_ratio,3)} · 涨停 ${ph.n_limit_up} / 跌停 ${ph.n_limit_dn}</div>`;
    html += '</div>';
  }
  // 市场情绪
  if(c.market && c.market.data){
    const m = c.market.data;
    html += '<h3 style="font-size:14px;margin:8px 0">市场情绪 ('+esc(c.market.path.split(/[\\/]/).pop())+')</h3>';
    html += '<table class="tbl"><thead><tr><th>指标</th><th>值</th></tr></thead><tbody>';
    Object.keys(m).forEach(k=>{
      if(typeof m[k] === 'object' || k === 'ok') return;
      html += `<tr><td>${esc(k)}</td><td><code>${esc(JSON.stringify(m[k]))}</code></td></tr>`;
    });
    html += '</tbody></table>';
  }
  // 市场宽度
  if(c.breadth && c.breadth.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">市场宽度 (最近 ' + c.breadth.length + ' 日)</h3>';
    html += '<table class="tbl"><thead><tr><th>日期</th><th>总数</th><th>涨</th><th>跌</th><th>涨停</th><th>跌停</th><th>宽度比</th><th>均价%</th><th>总成交(亿)</th></tr></thead><tbody>';
    c.breadth.forEach(b=>{
      html += `<tr>
        <td>${esc(b.date)}</td>
        <td>${b.total}</td>
        <td class="up">${b.n_up}</td>
        <td class="down">${b.n_down}</td>
        <td class="up">${b.n_limit_up}</td>
        <td class="down">${b.n_limit_dn}</td>
        <td class="${(b.breadth_ratio||0)>=0?'up':'down'}">${fmt(b.breadth_ratio,3)}</td>
        <td class="${(b.avg_chg||0)>=0?'up':'down'}">${fmt(b.avg_chg,3)}</td>
        <td>${fmt((b.total_amount||0)/1e8,2)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
  }
  // 因子 IC
  if(c.factor_ic && c.factor_ic.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">因子 IC (近 20 日)</h3>';
    html += '<table class="tbl"><thead><tr><th>因子</th><th>IC</th><th>ICIR</th><th>胜率</th><th>N</th></tr></thead><tbody>';
    c.factor_ic.forEach(f=>{
      html += `<tr><td>${esc(f.factor)}</td>
        <td class="${(f.ic_value||0)>=0?'up':'down'}">${fmt(f.ic_value,4)}</td>
        <td class="${(f.icir_20||0)>=0?'up':'down'}">${fmt(f.icir_20,3)}</td>
        <td>${fmt((f.win_rate_20||0)*100,1)}%</td>
        <td>${f.n_days}</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;
}

// ---- 异动监控 ----
async function loadAbnormal(){
  try{ const d = await fetch('/api/abnormal?limit=30').then(r=>r.json()); renderAbnormal(d); }
  catch(e){ $('#abnormalBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderAbnormal(d){
  const body = $('#abnormalBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  let html = `<div class="muted" style="margin-bottom:8px;font-size:12px">基于 ${esc(d.latest_date)} 收盘</div>`;
  function tbl(t, title, color){
    if(!t||!t.length) return '';
    let h = `<h3 style="font-size:14px;margin:14px 0 8px;color:${color}">${title}</h3><table class="tbl"><thead><tr><th>代码</th><th>收盘</th><th>涨幅%</th><th>成交额</th></tr></thead><tbody>`;
    t.slice(0,15).forEach(r=>{
      h += `<tr><td><code>${esc(r.canon)}</code></td><td>${fmt(r.close,2)}</td><td class="${(r.change_pct||0)>=0?'up':'down'}">${(r.change_pct>=0?'+':'') + fmt(r.change_pct,2)}</td><td>${fmt((r.amount||0)/1e8,2)}亿</td></tr>`;
    });
    h += '</tbody></table>';
    return h;
  }
  html += tbl(d.limit_up, '涨停 (' + d.limit_up.length + ')', '#ff5b6a');
  html += tbl(d.limit_dn, '跌停 (' + d.limit_dn.length + ')', '#2fe6a6');
  html += tbl(d.top_change, '涨幅 Top', '#ff5b6a');
  html += tbl(d.top_drop, '跌幅 Top', '#2fe6a6');
  html += tbl(d.top_amount, '成交额 Top', '#4f8cff');
  // 放大量
  if(d.volume_surge && d.volume_surge.length){
    html += '<h3 style="font-size:14px;margin:14px 0 8px">放大量 (vs 20日均量)</h3>';
    html += '<table class="tbl"><thead><tr><th>代码</th><th>今日成交额</th><th>20日均</th><th>倍数</th></tr></thead><tbody>';
    d.volume_surge.slice(0,15).forEach(v=>{
      html += `<tr><td><code>${esc(v.canon)}</code></td><td>${fmt((v.amount||0)/1e8,2)}亿</td><td>${fmt((v.amt_avg_20d||0)/1e8,2)}亿</td><td class="up">${fmt(v.ratio,2)}x</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;
}

// ---- 数据板块 ----
let dbSyncJobId = null;       // 当前同步任务 ID
let dbSyncPollTimer = null;
async function loadDbPanel(){
  try{ const d = await fetch('/api/db_meta').then(r=>r.json()); renderDbPanel(d); }
  catch(e){ $('#dbpanelBody').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderDbPanel(d){
  const body = $('#dbpanelBody');
  if(!d.ok){ body.innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  // 头部 + 同步按钮
  const size_mb = d.duckdb.size_mb || 0;
  let html = `<div class="muted" style="margin-bottom:6px;font-size:12px">
    DuckDB: ${esc(d.duckdb.path)} (${size_mb} MB) · ArcticDB: ${esc((d.arcticdb.uri||'-'))}</div>`;
  // 同步控制条
  html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;padding:10px;background:hsl(var(--elevated)/.5);border-radius:8px;border:1px solid hsl(var(--border))">
    <button class="tabBtn dbp-sync-btn" data-mode="all">增量同步 (全部表)</button>
    <select id="dbpOnlySel" class="input-dark" style="padding:5px 8px"></select>
    <button class="tabBtn dbp-sync-btn" data-mode="partial">增量同步 (选中表)</button>
    <button class="tabBtn dbp-sync-btn" data-mode="manual" id="dbpManualBtn">手动同步 (单表)</button>
    <input id="dbpDayInput" class="input-dark" type="date" value="${new Date().toISOString().slice(0,10)}" style="padding:5px"/>
    <span class="muted" style="font-size:11px">同步日期 (默认今日)</span>
    <span class="muted" id="dbpSyncStatus" style="font-size:11px;margin-left:auto"></span>
  </div>`;
  html += '<div id="dbpSyncLog" style="display:none;background:hsl(var(--base));border:1px solid hsl(var(--border));border-radius:6px;padding:8px;max-height:160px;overflow-y:auto;font-family:monospace;font-size:10px;margin-bottom:10px;white-space:pre-wrap"></div>';

  // DuckDB 表 (按行数倒序) — 表头白色 + 中文名 + 数据时间范围
  const tables = d.duckdb.tables || [];
  const groups = d.duckdb.groups || {};
  // 分组下拉
  html += '<div id="dbpOnlySelInit" style="display:none">__GROUPS__</div>';
  let groupsOpts = '<option value="">(不限)</option>';
  Object.keys(groups).sort().forEach(g=>{
    groupsOpts += `<option value="${esc(g)}">${esc(g)} (${groups[g]})</option>`;
  });
  // 渲染分组
  html += '<h3 style="font-size:14px;margin:8px 0;">DuckDB 表 (' + tables.length + ' / ' + Object.keys(groups).length + ' 类)</h3>';
  // 同步过滤
  html += `<div style="margin-bottom:6px;display:flex;gap:6px;align-items:center">
    <input id="dbpFilterInput" placeholder="过滤表名/中文名" style="flex:1;padding:5px 8px;border:1px solid #334;border-radius:6px;background:#0f1420;color:hsl(var(--fg-primary))"/>
  </div>`;
  html += '<div style="max-height:600px;overflow-y:auto"><table class="tbl"><thead><tr><th>表名 (中文)</th><th>分组</th><th>行数</th><th>列数</th><th>数据时间范围</th><th>字段</th><th>操作</th></tr></thead><tbody id="dbpTbody">';
  tables.forEach(t=>{
    const cols = (t.columns||[]).map(c=>c.name).join(', ');
    const range = t.date_range || '-';
    html += `<tr data-name="${esc(t.name)}" data-zh="${esc(t.name_zh||'')}" data-group="${esc(t.group||'')}">
      <td><a href="#" class="dbp-table" data-name="${esc(t.name)}" ><b style="color:#fff;font-size:13px;letter-spacing:0.5px">${esc(t.name)}</b><br/><span class="muted" style="font-size:10px">${esc(t.name_zh||'')}</span></a></td>
      <td><span class="badge" style="font-size:10px">${esc(t.group||'-')}</span></td>
      <td>${(t.rows||0).toLocaleString()}</td>
      <td>${t.col_count||(t.columns||[]).length}</td>
      <td><code style="font-size:10px;color:hsl(var(--accent))">${esc(range)}</code></td>
      <td><code style="font-size:10px;display:block;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(cols)}</code></td>
      <td><button class="tabBtn dbp-manual-sync" data-name="${esc(t.name)}" style="font-size:10px;padding:2px 8px">同步</button></td>
    </tr>`;
  });
  html += '</tbody></table></div>';

  // ArcticDB 库
  const a = d.arcticdb || {};
  if(a.libraries){
    html += '<h3 style="font-size:14px;margin:14px 0 8px;">ArcticDB 库</h3>';
    html += '<table class="tbl"><thead><tr><th>库 (中文)</th><th>symbols</th><th>说明</th></tr></thead><tbody>';
    const libZh = {"bars":"K线明细","trade_records":"交易记录","daily_summary":"每日汇总","perf_report":"绩效报告","factor_ic":"因子IC","reward_curve":"奖励曲线"};
    Object.keys(a.libraries).forEach(k=>{
      html += `<tr><td><b style="color:#fff;font-size:13px">${esc(k)}</b> <span class="muted" style="font-size:10px">${esc(libZh[k]||k)}</span></td><td>${(a.stats&&a.stats[k])||0}</td><td class="muted">LMDB-backed time-series</td></tr>`;
    });
    html += '</tbody></table>';
  }
  body.innerHTML = html;

  // 注入分组选项到 dbpOnlySel
  document.getElementById('dbpOnlySel').innerHTML = groupsOpts;
  // 过滤输入
  document.getElementById('dbpFilterInput').oninput = (e)=>{
    const q = e.target.value.toLowerCase();
    body.querySelectorAll('#dbpTbody tr').forEach(tr=>{
      const hit = !q || tr.dataset.name.toLowerCase().includes(q) ||
        (tr.dataset.zh||'').toLowerCase().includes(q) ||
        (tr.dataset.group||'').toLowerCase().includes(q);
      tr.style.display = hit ? '' : 'none';
    });
  };
  // 表名点击展开样本
  body.querySelectorAll('.dbp-table').forEach(a=>{
    a.onclick = async (e) => {
      e.preventDefault();
      const name = a.dataset.name;
      const r = await fetch('/api/db_table/' + encodeURIComponent(name) + '?limit=10').then(r=>r.json());
      if(!r.ok){ alert('读取失败: ' + r.error); return; }
      let h = `<h3 style="font-size:14px;margin:14px 0 8px;">${esc(name)} · 样本 (${r.rows.length}/${r.total})</h3>`;
      h += '<div style="overflow-x:auto"><table class="tbl" style="font-size:11px"><thead><tr>';
      r.columns.forEach(c=>{ h += `<th>${esc(c)}</th>`; });
      h += '</tr></thead><tbody>';
      r.rows.forEach(row=>{
        h += '<tr>';
        row.forEach(v=>{ h += `<td><code style="font-size:10px">${esc(v===null?'NULL':String(v).slice(0,80))}</code></td>`; });
        h += '</tr>';
      });
      h += '</tbody></table></div>';
      const div = document.createElement('div');
      div.innerHTML = h;
      body.appendChild(div);
    };
  });
  // 同步按钮
  body.querySelectorAll('.dbp-sync-btn').forEach(b=>{
    b.onclick = async () => {
      const mode = b.dataset.mode;
      const day = document.getElementById('dbpDayInput').value || null;
      let payload = {day};
      if(mode === 'partial'){
        const sel = document.getElementById('dbpOnlySel').value;
        if(!sel){ alert('请先在分组下拉选择一组'); return; }
        payload.only = (window._dbpGroups||{})[sel] || [];
      } else if(mode === 'manual'){
        // 单表手动同步: 让用户选表
        const table = prompt('请输入要手动同步的表名 (例: daily_bars)');
        if(!table) return;
        startDbSync('manual', {table, day});
        return;
      }
      startDbSync('incremental', payload);
    };
  });
  body.querySelectorAll('.dbp-manual-sync').forEach(b=>{
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const t = b.dataset.name;
      const day = document.getElementById('dbpDayInput').value || null;
      startDbSync('manual', {table: t, day});
    };
  });
}
async function startDbSync(mode, payload){
  const url = mode === 'manual' ? '/api/db_sync/manual' : '/api/db_sync/incremental';
  const log = document.getElementById('dbpSyncLog');
  const stat = document.getElementById('dbpSyncStatus');
  log.style.display = 'block';
  log.textContent = `[${new Date().toLocaleTimeString()}] 启动 ${mode} 同步: ${JSON.stringify(payload)}\n`;
  stat.textContent = '调度中...';
  try{
    // 同步模式: POST 等到响应再显示完整结果. 同步大表可能耗时 30s+
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                  body: JSON.stringify(payload)});
    const j = await r.json();
    if(!j.ok){ log.textContent += `[ERR] ${j.error||JSON.stringify(j)}\n`; stat.textContent='失败'; return; }
    dbSyncJobId = j.job_id;
    log.textContent += `[${new Date().toLocaleTimeString()}] job_id=${dbSyncJobId}\n`;
    const res = j.result || {};
    const tbls = res.tables || {};
    Object.keys(tbls).forEach(t=>{
      const v = tbls[t] || {};
      const lvl = v.ok ? 'info' : 'warn';
      log.textContent += `[${new Date().toLocaleTimeString()}] ${t}: ok=${v.ok} rows=${v.rows}${v.error?(' err='+v.error.slice(0,80)):''}\n`;
    });
    if(res.error){ log.textContent += `[ERR] ${res.error}\n`; }
    stat.textContent = res.ok ? '完成' : (res.error ? '失败' : '部分失败');
    log.scrollTop = log.scrollHeight;
    setTimeout(()=>loadDbPanel(), 800);
  } catch(e){
    log.textContent += `[ERR] ${e}\n`; stat.textContent='失败';
  }
}


// ---- 市场看板 ----
async function loadMarketBoard(){
  try{ const d = await fetch('/api/marketboard').then(r=>r.json()); renderMarketBoard(d); }
  catch(e){
    ['#mbKpi','#mbIndices','#mbRadar','#mbBreadth','#mbDist','#mbLadder','#mbActive'].forEach(s=>{
      const el = document.querySelector(s); if(el) el.innerHTML='<span class="empty">加载失败: '+e+'</span>';
    });
  }
}
function renderMarketBoard(d){
  if(!d.ok){ $('#mbKpi').innerHTML='<span class="empty">'+esc(d.error||'error')+'</span>'; return; }
  const c = d.components || {};
  // KPI
  if(c.kpi){
    const k = c.kpi;
    const upCls = k.breadth_ratio > 0 ? 'up' : (k.breadth_ratio < 0 ? 'down' : '');
    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">';
    const cards = [
      {label: '交易日', val: esc(k.latest_date || '-'), tone: 'neutral'},
      {label: '股票家数', val: (k.universe||0).toLocaleString(), tone: 'neutral'},
      {label: '上涨', val: (k.n_up||0).toLocaleString(), tone: 'up'},
      {label: '下跌', val: (k.n_down||0).toLocaleString(), tone: 'down'},
      {label: '平盘', val: (k.n_flat||0).toLocaleString(), tone: 'neutral'},
      {label: '涨停', val: (k.n_limit_up||0).toLocaleString(), tone: 'up'},
      {label: '跌停', val: (k.n_limit_dn||0).toLocaleString(), tone: 'down'},
      {label: '宽度比', val: fmt(k.breadth_ratio, 3), tone: upCls},
      {label: '均价%', val: ((k.avg_change_pct||0)>=0?'+':'') + fmt(k.avg_change_pct, 3), tone: (k.avg_change_pct||0)>=0?'up':'down'},
      {label: '中位数%', val: ((k.median_change_pct||0)>=0?'+':'') + fmt(k.median_change_pct, 3), tone: (k.median_change_pct||0)>=0?'up':'down'},
      {label: '总成交(亿)', val: fmt((k.total_amount||0)/1e8, 1), tone: 'neutral'},
    ];
    cards.forEach(card=>{
      html += `<div class="card"><div class="lbl">${card.label}</div><div class="val ${card.tone}">${card.val}</div></div>`;
    });
    html += '</div>';
    $('#mbKpi').innerHTML = html;
  }
  // 指数
  if(c.indices){
    let html = '<table class="tbl"><thead><tr><th>名称</th><th>代码</th><th>收盘</th><th>涨跌%</th></tr></thead><tbody>';
    c.indices.forEach(idx=>{
      const cls = (idx.change_pct||0) >= 0 ? 'up' : 'down';
      const sign = (idx.change_pct||0) >= 0 ? '+' : '';
      html += `<tr><td>${esc(idx.name)}</td><td><code>${esc(idx.symbol)}</code></td><td>${fmt(idx.close, 2)}</td><td class="${cls}">${sign}${fmt(idx.change_pct, 2)}%</td></tr>`;
    });
    html += '</tbody></table>';
    $('#mbIndices').innerHTML = html;
  } else { $('#mbIndices').innerHTML = '<div class="muted">暂无指数数据 (daily_bars 可能不含指数代码)</div>'; }
  // 雷达
  if(c.radar){
    const radar = c.radar;
    const N = radar.length;
    const size = 220, cx = size/2, cy = size/2, maxR = 70;
    const pts = radar.map((r,i)=>{
      const a = -Math.PI/2 + i*2*Math.PI/N;
      const rad = maxR * Math.max(0, Math.min(100, r.value)) / 100;
      return {x: cx + Math.cos(a)*rad, y: cy + Math.sin(a)*rad,
              lx: cx + Math.cos(a)*(maxR+22), ly: cy + Math.sin(a)*(maxR+22)};
    });
    const poly = pts.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
    let svg = `<svg viewBox="0 0 ${size} ${size}" style="width:100%;max-width:220px;margin:0 auto;display:block">`;
    // 网格
    [1, 0.66, 0.33].forEach((lv,i)=>{
      const gp = radar.map((_,j)=>{
        const a = -Math.PI/2 + j*2*Math.PI/N;
        return `${(cx + Math.cos(a)*maxR*lv).toFixed(1)},${(cy + Math.sin(a)*maxR*lv).toFixed(1)}`;
      }).join(' ');
      svg += `<polygon points="${gp}" fill="${i%2===0?'rgba(79,140,255,0.06)':'rgba(255,255,255,0.03)'}" stroke="rgba(255,255,255,0.15)" stroke-width="0.5"/>`;
    });
    pts.forEach(p=>{
      svg += `<line x1="${cx}" y1="${cy}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}" stroke="rgba(255,255,255,0.2)" stroke-width="0.5"/>`;
    });
    svg += `<polygon points="${poly}" fill="rgba(79,140,255,0.4)" stroke="#4f8cff" stroke-width="2"/>`;
    pts.forEach(p=>{
      svg += `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="#4f8cff" stroke="#fff" stroke-width="1"/>`;
    });
    // 中心: sentiment score
    const ss = c.sentiment_score;
    if(ss !== null && ss !== undefined){
      svg += `<text x="${cx}" y="${cy-4}" text-anchor="middle" fill="#fff" font-size="22" font-weight="bold" font-family="monospace">${fmt(ss,1)}</text>`;
      svg += `<text x="${cx}" y="${cy+14}" text-anchor="middle" fill="#8b98b3" font-size="10">情绪分</text>`;
    }
    pts.forEach(p=>{
      svg += `<text x="${p.lx.toFixed(1)}" y="${p.ly.toFixed(1)+3}" text-anchor="middle" fill="#e6edf7" font-size="10">${esc(p.label||'')}</text>`;
    });
    svg += '</svg>';
    $('#mbRadar').innerHTML = svg + `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;justify-content:center">${radar.map(r=>`<span class="badge" style="font-size:10px">${esc(r.label)} ${fmt(r.value,0)}</span>`).join('')}</div>`;
  } else { $('#mbRadar').innerHTML = '<div class="muted">暂无市场情绪数据</div>'; }
  // 宽度
  if(c.kpi){
    const k = c.kpi;
    const tot = Math.max(1, k.universe || 0);
    const upW = (k.n_up||0)/tot*100, downW = (k.n_down||0)/tot*100, flatW = Math.max(0, 100-upW-downW);
    let html = `<div style="display:flex;height:32px;border-radius:8px;overflow:hidden;margin-bottom:8px">
      <div style="background:hsl(var(--bull));width:${upW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_up||0)}</div>
      <div style="background:hsl(var(--fg-muted));width:${flatW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_flat||0)}</div>
      <div style="background:hsl(var(--bear));width:${downW.toFixed(2)}%;display:flex;align-items:center;justify-content:center;font-size:11px">${(k.n_down||0)}</div>
    </div>`;
    html += `<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:6px">`;
    const blocks = [
      {label: '涨停', val: k.n_limit_up, tone: 'up'},
      {label: '跌停', val: k.n_limit_dn, tone: 'down'},
      {label: '总成交(亿)', val: fmt((k.total_amount||0)/1e8, 1), tone: 'neutral'},
      {label: '均价%', val: ((k.avg_change_pct||0)>=0?'+':'') + fmt(k.avg_change_pct, 3), tone: (k.avg_change_pct||0)>=0?'up':'down'},
      {label: '宽度比', val: fmt(k.breadth_ratio||0, 3), tone: (k.breadth_ratio||0)>=0?'up':'down'},
    ];
    blocks.forEach(b=>{
      html += `<div class="card" style="padding:8px"><div class="lbl">${b.label}</div><div class="val ${b.tone}" style="font-size:18px">${b.val}</div></div>`;
    });
    html += '</div>';
    $('#mbBreadth').innerHTML = html;
  }
  // 分布
  if(c.distribution){
    const dist = c.distribution;
    const mx = Math.max(...dist.map(d=>d.count), 1);
    let html = '<div style="display:grid;grid-template-columns:repeat(10,1fr);gap:4px;height:140px;align-items:end">';
    dist.forEach((d,i)=>{
      const h = Math.max(2, d.count/mx*100);
      const positive = i >= 5;
      html += `<div style="display:flex;flex-direction:column;align-items:center;justify-content:end;height:100%">
        <div style="font-size:10px;color:hsl(var(--fg-muted));font-family:monospace">${d.count}</div>
        <div style="width:80%;height:${h}%;background:${positive?'linear-gradient(180deg,hsl(var(--bull)),hsl(var(--bull)/.35))':'linear-gradient(180deg,hsl(var(--bear)),hsl(var(--bear)/.35))'};border-radius:3px 3px 0 0"></div>
        <div style="font-size:9px;color:hsl(var(--fg-muted));margin-top:2px;white-space:nowrap">${esc(d.label)}</div>
      </div>`;
    });
    html += '</div>';
    $('#mbDist').innerHTML = html;
  } else { $('#mbDist').innerHTML = '<div class="muted">暂无分布数据</div>'; }
  // 连板梯队
  if(c.ladder && c.ladder.length){
    let html = '';
    c.ladder.forEach(t=>{
      const barW = Math.min(100, t.count * 12);
      html += `<div style="display:grid;grid-template-columns:50px 1fr 40px;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid hsl(var(--border))">
        <span style="font-family:monospace;font-weight:bold;color:${t.boards>=5?'hsl(var(--bull))':(t.boards>=3?'hsl(var(--warn))':'hsl(var(--fg-muted))')}">${t.boards}板</span>
        <div style="height:8px;background:hsl(var(--elevated));border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:hsl(var(--bull));opacity:0.7"></div>
        </div>
        <span style="font-family:monospace;font-size:12px;color:hsl(var(--fg-primary))">${t.count}</span>
      </div>`;
      if(t.stocks && t.stocks.length){
        html += `<div style="padding-left:58px;font-size:10px;color:hsl(var(--fg-muted));margin-bottom:4px">${t.stocks.map(s=>`<span class="badge" style="margin-right:4px;font-size:9px">${esc(s.symbol)} ${(s.change_pct>=0?'+':'')}${fmt(s.change_pct,1)}%</span>`).join('')}</div>`;
      }
    });
    $('#mbLadder').innerHTML = html;
  } else { $('#mbLadder').innerHTML = '<div class="muted">今日无涨停股票</div>'; }
  // 北向 + 活跃 Top
  let activeHtml = '';
  if(c.northbound){
    const nb = c.northbound;
    activeHtml += `<div style="margin-bottom:8px"><b>北向资金</b> <span class="muted" style="font-size:10px">${esc(nb.trade_date||'-')}</span>`;
    if(nb.net_buy_amount !== undefined){
      const nb_cls = (nb.net_buy_amount||0) >= 0 ? 'up' : 'down';
      const sign = (nb.net_buy_amount||0) >= 0 ? '+' : '';
      activeHtml += `<div class="card" style="margin-top:6px;padding:8px"><div class="lbl">净买入(亿)</div><div class="val ${nb_cls}" style="font-size:20px">${sign}${fmt(nb.net_buy_amount/1e8, 2)}</div></div>`;
      if(nb.sh_index_change_pct !== undefined){
        activeHtml += `<div class="card" style="margin-top:6px;padding:8px"><div class="lbl">上证</div><div class="val ${(nb.sh_index_change_pct||0)>=0?'up':'down'}" style="font-size:16px">${(nb.sh_index_change_pct>=0?'+':'')}${fmt(nb.sh_index_change_pct,2)}%</div></div>`;
      }
    }
    activeHtml += '</div>';
  } else {
    activeHtml += '<div class="muted">无北向资金数据</div>';
  }
  if(c.active_top && c.active_top.length){
    activeHtml += '<h3 style="font-size:13px;margin:10px 0 6px;">成交额 Top 10</h3>';
    activeHtml += '<table class="tbl" style="font-size:11px"><thead><tr><th>代码</th><th>收盘</th><th>涨幅%</th><th>成交(亿)</th></tr></thead><tbody>';
    c.active_top.forEach(t=>{
      const cls = (t.change_pct||0)>=0?'up':'down';
      activeHtml += `<tr><td><code>${esc(t.symbol)}</code></td><td>${fmt(t.close,2)}</td><td class="${cls}">${(t.change_pct>=0?'+':'')}${fmt(t.change_pct,2)}%</td><td>${fmt((t.amount||0)/1e8,2)}</td></tr>`;
    });
    activeHtml += '</tbody></table>';
  }
  $('#mbActive').innerHTML = activeHtml;
}

async function loadMarket(){
  try{
    const r = await fetch('/api/market'); const d = await r.json();
    renderMarket(d);
  }catch(e){ $('#sentiBar').innerHTML='<span class="empty">市场数据加载失败: '+e+'</span>'; }
}
async function loadPerf(){
  try{
    const r = await fetch('/api/perf'); const d = await r.json();
    renderPerf(d);
  }catch(e){ $('#pfMetrics').innerHTML='<span class="empty">绩效数据加载失败: '+e+'</span>'; }
}

async function loadTargetPlan(){
  try{
    const r = await fetch('/api/target_plan'); const d = await r.json();
    renderTargetPlan(d);
  }catch(e){ $('#pfTargetPlan').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderTargetPlan(d){
  const box = $('#pfTargetPlan');
  if(!d.ok){ box.innerHTML = '<span class="empty">'+esc(d.error||'plan 不存在')+(d.hint?(' ('+d.hint+')'):'')+'</span>'; return; }
  const p = d.plan || {};
  const top = p.top_n || [];
  const w = p.weights_used || {};
  let html = `<div class="muted" style="font-size:12px;margin-bottom:8px">day=${esc(p.day)} · universe=${p.universe_size} · ${esc(p.method||'')} · 生成于 ${esc(p.generated_at||'')}</div>`;
  html += '<div style="margin-bottom:8px"><b>权重:</b> ';
  Object.keys(w).forEach(k=>{ html += `<span class="badge" style="margin-right:4px">${esc(k)}=${fmt(w[k],3)}</span>`; });
  html += '</div>';
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>#</th><th>canon</th><th>价</th><th>换手%</th><th>drl_score</th><th>权重</th><th>信号</th></tr></thead><tbody>';
  top.forEach((t,i)=>{
    html += `<tr><td>${i+1}</td><td>${esc(t.canon)}</td><td>${fmt(t.price,3)}</td><td>${fmt(t.turnover,2)}</td><td>${fmt(t.drl_score,4)}</td><td>${fmt(t.target_weight,3)}</td><td>${esc(t.source_signal||'')}</td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

async function loadHealth(){
  try{
    const r = await fetch('/api/health'); const d = await r.json();
    renderHealth(d);
  }catch(e){ $('#pfHealth').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderHealth(d){
  const box = $('#pfHealth');
  if(!d.ok && !d.level){ box.innerHTML = '<span class="empty">'+esc(d.error||'health 不存在')+'</span>'; return; }
  const lvl = d.level || (d.ok?'OK':'DEGRADED');
  const cls = lvl==='OK' ? 'up' : (lvl==='DEGRADED' ? 'warn' : 'down');
  let html = `<div style="margin-bottom:8px"><span class="badge ${cls}" style="font-size:13px;padding:6px 12px">${esc(lvl)}</span> <span class="muted">${esc(d.summary||'')} · ${esc(d.generated_at||'')}</span></div>`;
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>检查项</th><th>状态</th><th>耗时(ms)</th><th>详情</th></tr></thead><tbody>';
  (d.checks||[]).forEach(c=>{
    const c_cls = c.status==='OK' ? 'up' : (c.status==='WARN' ? 'warn' : 'down');
    let detail = '';
    if(typeof c.detail === 'object' && c.detail !== null){
      detail = JSON.stringify(c.detail, null, 0).slice(0, 240);
    } else { detail = String(c.detail||''); }
    html += `<tr><td>${esc(c.name)}</td><td><span class="badge ${c_cls}">${esc(c.status)}</span></td><td>${c.ms||0}</td><td><code style="font-size:10px">${esc(detail)}</code></td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

async function loadViews(){
  try{
    const r = await fetch('/api/views'); const d = await r.json();
    renderViews(d);
  }catch(e){ $('#pfViews').innerHTML='<span class="empty">加载失败: '+e+'</span>'; }
}
function renderViews(d){
  const box = $('#pfViews');
  if(!d.ok && !d.views){ box.innerHTML = '<span class="empty">'+esc(d.error||'views 不存在')+'</span>'; return; }
  let html = `<div class="muted" style="font-size:12px;margin-bottom:8px">${esc(d.duckdb_path||'')} · 生成于 ${esc(d.generated_at||'')}</div>`;
  html += '<table class="tbl" style="font-size:12px"><thead><tr><th>视图</th><th>状态</th><th>行数</th><th>Parquet</th><th>耗时(ms)</th></tr></thead><tbody>';
  const views = d.views || {};
  Object.keys(views).forEach(name=>{
    const v = views[name];
    const c_cls = v.ok ? 'up' : 'down';
    html += `<tr><td>${esc(name)}</td><td><span class="badge ${c_cls}">${v.ok?'OK':'FAIL'}</span></td><td>${v.rows||0}</td><td><code style="font-size:10px">${esc((v.parquet||'').replace(/^.*[\\/]/,''))}</code></td><td>${v.ms||0}</td></tr>`;
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

// 市场/绩效 tab 每30秒刷新一次(不重复加载首屏)
setInterval(()=>{ if(mkLoaded) loadMarket(); if(pfLoaded) { loadPerf(); loadTargetPlan(); loadHealth(); loadViews(); } }, 30000);
</script>
</body>
</html>
"""


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
        httpd = _ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    else:
        # 单线程 HTTPServer: 避免 Python 3.14 + DuckDB 在 Threading 下的 selectors GIL 冲突
        class _SingleThreadHTTPServer(HTTPServer):
            """BaseHTTPServer 默认就单线程处理, 这里只是显式标注."""
            allow_reuse_address = True
        httpd = _SingleThreadHTTPServer(("0.0.0.0", args.port), Handler)
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
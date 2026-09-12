# ============================================================
# db.py -- 本地 DuckDB 数据访问层 (全A日频)
# 统一内部 canonical 代码格式: "600519.SH" (带交易所后缀)
# DuckDB 表两种格式:
#   bars               : "600519.SH" (后缀)
#   daily_bars/symbols/valuation_snapshot : "600519" (纯6位)
# ============================================================

import os
import sys
from functools import lru_cache

import duckdb
import pandas as pd
from config import DUCKDB_PATH, POOL_FILTER

# [2026-09-05 迁移 m4] 读取侧数据源切换: h5i(新分析层, 默认) | duck(对照/临时回退).
# DuckDB 已退役删除; 仅当显式设 BAR_STORE=duck 且文件仍在时走 DuckDB 对照.
BAR_STORE = os.environ.get("BAR_STORE", "h5i").lower()
_SYM_PARQUET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "h5i", "static", "symbols.parquet")

# P7: 降级取数滞后告警的会话级去重计数器
_STALE_BAR_WARNED = [0]


def _canon_to_db(canon: str) -> str:
    """canonical '600519.SH' -> db 纯6位 '600519'"""
    return canon.split(".")[0]


def _db_to_canon(symbol6: str, market: str = None) -> str:
    """db 纯6位/symbols表的market -> canonical 带后缀"""
    if "." in symbol6:
        return symbol6
    mkt = (market or "").lower()
    if mkt in ("sh", "bj"):
        return f"{symbol6}.{'BSE' if mkt=='bj' else 'SH'}"
    return f"{symbol6}.SZ"


# A 股代码段白名单 (与 realtime_engine._A_SHARE_PREFIXES 一致).
# 剔除: 可转债(110/111/113 沪, 123/127/128 深), B股(200 深, 900 沪),
#       北交所(920/8xx/4xx), 及一切非 A 段.
_A_SHARE_PREFIXES = {
    "000", "001", "002", "003",
    "300", "301", "302",
    "600", "601", "603", "605",
    "688", "689",
}


def is_a_share_symbol(symbol: str) -> bool:
    """判断裸 6 位代码(或带 .SH/.SZ/.BJ 后缀)是否为 A 股."""
    if not symbol:
        return False
    code = str(symbol).split(".")[0]
    if len(code) < 3:
        return False
    return code[:3] in _A_SHARE_PREFIXES


def _h5i_enabled() -> bool:
    return BAR_STORE == "h5i"


def duck_available() -> bool:
    """DuckDB 主库文件是否存在/可读 (退役后恒 False, 供调用方快速判源)."""
    try:
        return os.path.exists(DUCKDB_PATH)
    except Exception:
        return False


def _h5i_store():
    if not _h5i_enabled():
        return None
    from h5i_bar_store import H5iBarStore  # noqa: 延迟导入避免循环
    return H5iBarStore()


# [m4 性能] h5i daily_bars 整窗预热缓存 (供 selector 等对全池逐 symbol 批量读取加速).
# 仅当 StockDB.prefetch_daily_bars 显式预热后, get_bars 命中同窗口时走内存切片;
# 未预热/窗口不符仍走逐条 SQL (语义/数值完全一致, 本缓存只做加速不做改写).
_H5I_BULK_SLOT: dict = {"key": None, "frame": None, "window_days": 0}
_H5I_BULK_BACK_DAYS = 400  # 覆盖 >=200 交易日的自然日缓冲


@lru_cache(maxsize=1)
def _h5i_symbols_df():
    import pyarrow.parquet as pq
    try:
        return pq.read_table(_SYM_PARQUET).to_pandas()
    except Exception:
        return pd.DataFrame()


def _universe_h5i(date=None) -> pd.DataFrame:
    """h5i 版 get_universe: 与 duck 语义一致(脏段防护+完整性+兜底最新)."""
    s = _h5i_store()
    seg = s._db.sql(
        "SELECT ts, COUNT(*) n, "
        "SUM(CASE WHEN float_mv>0 THEN 1 ELSE 0 END) fmv_ok, "
        "SUM(CASE WHEN symbol LIKE '6%' THEN 1 ELSE 0 END) sh_cnt "
        "FROM valuation_snapshot GROUP BY ts ORDER BY ts DESC").to_pandas()
    seg = seg[seg["n"] > 0].copy()
    seg["ratio"] = seg["fmv_ok"] / seg["n"]
    seg["complete"] = (seg["n"] >= 3000) & (seg["sh_cnt"] > 0)
    ok = seg[(seg["ratio"] >= 0.8) & seg["complete"]]
    snap_ts = str(ok.iloc[0]["ts"]) if not ok.empty else None

    def _build(dfv):
        dfv = dfv.rename(columns={"name": "name6"})
        sy = _h5i_symbols_df()
        sy = sy[sy["is_active"] == True]  # noqa: E712
        sy = sy[["symbol", "market", "list_date", "is_active"]]
        m = dfv.merge(sy, on="symbol", how="inner")
        if m.empty:
            return m
        keep = ["symbol", "market", "list_date", "name6", "price", "float_mv",
                "total_mv", "turnover", "pe_ttm", "pb", "amount", "float_shares"]
        m = m[keep]
        m["canon"] = m.apply(lambda r: _db_to_canon(r["symbol"], r["market"]), axis=1)
        return m

    if snap_ts:
        v = s._db.sql(
            f"SELECT symbol, name, price, float_mv, total_mv, turnover, pe_ttm, pb, "
            f"amount, float_shares FROM valuation_snapshot WHERE ts = TIMESTAMP '{snap_ts}'").to_pandas()
        df = _build(v)
        if not df.empty:
            return df
    v = s._db.sql(
        "SELECT v.symbol, v.name, v.price, v.float_mv, v.total_mv, v.turnover, "
        "v.pe_ttm, v.pb, v.amount, v.float_shares FROM valuation_snapshot v "
        "JOIN (SELECT symbol, MAX(ts) mt FROM valuation_snapshot GROUP BY symbol) m "
        "ON v.symbol=m.symbol AND v.ts=m.mt").to_pandas()
    return _build(v)


_PE_PATCH_CACHE: dict = {}


_PATCH_COMPAT_WARNED: set = set()


def _log_patch_compat(f: str) -> None:
    """旧格式补丁(缺 free_cap 列)兼容读取: 每次会话只提示一次."""
    if f in _PATCH_COMPAT_WARNED:
        return
    _PATCH_COMPAT_WARNED.add(f)
    print("[pit] 补丁为旧格式(无 free_cap 列), 已按兼容方式读取: %s"
          % os.path.basename(f), flush=True)


def _pe_patch_asof(as_of) -> pd.DataFrame:
    """读取 data/pit/pe_patch/*.parquet 的 pe_ttm 补丁, 返回每 symbol 的 as-of 取值.

    h5i valuation 主表无法回填历史(append 单调 + 无建表 API), 故补丁落 parquet,
    此处按 <= as_of 过滤后取"pe_ttm 非空优先 + ts 最新"的一行, 保证 PIT 无前视.
    """
    import glob
    from config import DATA_DIR as _DD
    files = sorted(glob.glob(os.path.join(_DD, "pit", "pe_patch", "*.parquet")))
    if not files:
        return pd.DataFrame(columns=["symbol", "pe_ttm"])
    key = (tuple(files), str(as_of)[:10])
    if key in _PE_PATCH_CACHE:
        return _PE_PATCH_CACHE[key]
    asd = pd.Timestamp(str(as_of)[:10])
    frames = []
    for f in files:
        try:
            t = pd.read_parquet(f, columns=["ts", "symbol", "pe_ttm", "free_cap"])
        except Exception:
            # 旧格式兼容: 早期补丁可能没有 free_cap 列。若整块 except 跳过,
            # 该文件的 pe_ttm 也会一起失效(2026-09-13 修正)。降级读取并记日志。
            try:
                t = pd.read_parquet(f, columns=["ts", "symbol", "pe_ttm"])
                t["free_cap"] = float("nan")
                _log_patch_compat(f)
            except Exception:
                continue
        t = t[t["ts"] <= asd]
        if len(t):
            frames.append(t)
    if frames:
        allp = pd.concat(frames, ignore_index=True)
        allp["_ok"] = allp["pe_ttm"].notna().astype(int)
        allp = (allp.sort_values(["symbol", "_ok", "ts"])
                .drop_duplicates("symbol", keep="last"))
        out = allp[["symbol", "pe_ttm", "free_cap"]]
    else:
        out = pd.DataFrame(columns=["symbol", "pe_ttm", "free_cap"])
    if len(_PE_PATCH_CACHE) > 32:
        _PE_PATCH_CACHE.clear()
    _PE_PATCH_CACHE[key] = out
    return out


def _valuation_asof_h5i(as_of) -> pd.DataFrame:
    """PIT(point-in-time) 估值: 取 valuation 表中 <= as_of 的每 symbol 最新一行.

    2026-09-12: 历史选股/回放此前只能读 valuation_snapshot(仅当日快照, 且
    MAX(ts) 属前视), 故 _universe_asof_h5i 干脆不提供估值列, 导致回测中
    governance_score 的 PB/PE 过滤与 filter_universe 的流通市值过滤**双双失效**,
    与实盘(读快照)特征不一致。valuation 表本身有 1993 起逐日 PIT 数据
    (近 3 年 730 个交易日, 日均覆盖 5399 只), 因此这里用窗口函数做严格 as-of 取值,
    在"只用 <= as_of 数据"的前提下把估值特征还给历史路径.

    单位: market_cap/free_cap 为"元", 统一转"亿"(total_mv/float_mv)对齐快照口径。
    """
    s = _h5i_store()
    asd = str(as_of)[:10]
    q = ("SELECT symbol, pe_ttm, pb, ps_ttm, float_shares, is_st, market_cap, free_cap "
         "FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol "
         "ORDER BY ts DESC, (pe_ttm IS NOT NULL) DESC) rn "
         "FROM valuation "
         f"WHERE CAST(ts AS DATE) <= DATE '{asd}' "
         f"AND CAST(ts AS DATE) >= DATE '{asd}' - INTERVAL 400 DAY) "
         "WHERE rn = 1")
    try:
        df = s._db.sql(q).to_pandas()
    except Exception:
        return pd.DataFrame(columns=["symbol", "pe_ttm", "pb", "ps_ttm",
                                     "float_shares", "is_st", "total_mv", "float_mv"])
    if df.empty:
        return df
    # 补丁合并(parquet as-of): 同时补齐 pe_ttm 与 free_cap(流通市值, 原为"元"),
    # 数据来源东财历史估值; 仍严格 <= as_of, 无前视.
    try:
        p = _pe_patch_asof(as_of)
        if not p.empty:
            df = df.merge(p.rename(columns={"pe_ttm": "_p_pe", "free_cap": "_p_fc"}),
                          on="symbol", how="left")
            df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce").fillna(
                pd.to_numeric(df.pop("_p_pe"), errors="coerce"))
            df["free_cap"] = pd.to_numeric(df["free_cap"], errors="coerce").fillna(
                pd.to_numeric(df.pop("_p_fc"), errors="coerce"))
    except Exception:
        pass
    mc = pd.to_numeric(df.pop("market_cap"), errors="coerce")
    fc = pd.to_numeric(df.pop("free_cap"), errors="coerce")
    df["float_mv"] = fc / 1e8                      # 流通市值(亿)
    # market_cap 历史覆盖≈0%(仅 2026-08 起少量), 缺失时以流通市值兜底(近似总市值)
    df["total_mv"] = (mc / 1e8).fillna(df["float_mv"])
    return df


def _universe_asof_h5i(as_of) -> pd.DataFrame:
    s = _h5i_store()
    asd = str(as_of)[:10]
    q = ("SELECT d.symbol, d.close AS price, d.amount FROM daily_bars d JOIN "
         "(SELECT symbol, MAX(CAST(ts AS DATE)) md FROM daily_bars "
         f"WHERE CAST(ts AS DATE) <= DATE '{asd}' AND close>0 GROUP BY symbol) m "
         f"ON d.symbol=m.symbol AND CAST(d.ts AS DATE)=m.md WHERE d.close>0")
    b = s._db.sql(q).to_pandas()
    if b.empty:
        return b
    sy = _h5i_symbols_df()
    sy = sy[sy["is_active"] == True]  # noqa: E712
    m = b.merge(sy[["symbol", "market", "list_date"]], on="symbol", how="inner")
    m = m[(m["list_date"].isna()) | (m["list_date"] <= as_of)]
    if m.empty:
        return m
    # PIT 估值合并 (无前视): pe_ttm/pb/ps_ttm/total_mv/float_mv/float_shares/is_st
    v = _valuation_asof_h5i(as_of)
    if not v.empty:
        m = m.merge(v, on="symbol", how="left")
        # 市值兜底(2026-09-12): valuation.free_cap 在 2019 ~ 2025-07 区间全缺(0%),
        # 但 float_shares 覆盖 99.9%, 故用 float_shares x price 复原 PIT 流通市值(亿),
        # 使早期历史窗口的市值过滤/选股可用(否则 filter_universe 会把整池滤空).
        if "float_shares" in m.columns:
            fs = pd.to_numeric(m["float_shares"], errors="coerce")
            px = pd.to_numeric(m["price"], errors="coerce")
            est = fs * px / 1e8
            m["float_mv"] = pd.to_numeric(m["float_mv"], errors="coerce").fillna(est)
            m["total_mv"] = pd.to_numeric(m["total_mv"], errors="coerce").fillna(m["float_mv"])
    m["canon"] = m.apply(lambda r: _db_to_canon(r["symbol"], r["market"]), axis=1)
    keep = ["symbol", "market", "list_date", "price", "amount", "canon",
            "pe_ttm", "pb", "ps_ttm", "total_mv", "float_mv", "float_shares", "is_st"]
    keep = [c for c in keep if c in m.columns]
    return m[keep]


class StockDB:
    """封装历史 DuckDB 数据湖的只读访问 (已退役, 仅当本机旧库仍存在时使用)"""

    def __init__(self, path: str = DUCKDB_PATH):
        self.path = path
        self._con = None

    def _conn(self):
        if self._con is None:
            if not os.path.exists(self.path):
                # DuckDB 已退役 (m4). 调用方应优先 h5i 分支; 走到这里说明是
                # 未迁移的残留读取点 -> 抛清晰错误, 由调用方捕获降级.
                raise FileNotFoundError(
                    f"DuckDB 已退役/不存在: {self.path} (请改用 BAR_STORE=h5i)")
            self._con = duckdb.connect(self.path, read_only=True)
        return self._con

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    # ---------- 全A 候选池 ----------
    def get_universe(self, date: "datetime|None" = None) -> pd.DataFrame:
        """返回全A活跃股票候选池(带后缀canonical + 估值/流动性字段)
        来源: valuation_snapshot + symbols(is_active) 交集
        返回列: canon, name6(name字段), price, float_mv, total_mv,
                turnover, pe_ttm, pb, amount, float_shares, list_date
        """
        if _h5i_enabled():
            try:
                return _universe_h5i(date)
            except Exception:
                return pd.DataFrame()
        con = self._conn()

        # —— 脏快照段防护 (重要): 某 fetch_time 段若 float_mv/float_shares 集体
        # 缺失(数据源列丢失, 实测 08-28 段 float_mv 全空), 则该段不可用.
        # 改为选取「最新且 float_mv 覆盖 >=80% 的全局快照段」, 避免全pool字段
        # 缺失导致市值过滤把池子抽干.
        #
        # P11 加固: 除覆盖率外, 还必须满足「段行数 >= 全市场下限」与「沪市 6*
        # 段存在」——否则残段(如引擎路径只写深市前缀 ["0*","3*"])会因 float_mv
        # 覆盖率虚高(实测 100%)而被误选, 导致 universe 只剩深市残池. 全A活跃
        # 股票约 5.5k, 取 3000 为保守下限; 6* 沪市缺失直接判不可用. ——
        try:
            seg = con.execute("""
                SELECT fetch_time,
                       count(*) n,
                       sum(CASE WHEN float_mv > 0 THEN 1 ELSE 0 END) fmv_ok,
                       sum(CASE WHEN symbol LIKE '6%' THEN 1 ELSE 0 END) sh_cnt
                FROM valuation_snapshot
                GROUP BY fetch_time
                ORDER BY fetch_time DESC
            """).fetchdf()
        except Exception:
            seg = None
        snap_ft = None
        if seg is not None and not seg.empty:
            seg = seg[seg["n"] > 0].copy()
            seg["ratio"] = seg["fmv_ok"] / seg["n"]
            # 完整性: 行数下限 + 沪市 6* 须存在 (残段防护)
            seg["complete"] = (seg["n"] >= 3000) & (seg["sh_cnt"] > 0)
            ok = seg[seg["ratio"] >= 0.8]
            ok = ok[ok["complete"]]
            if not ok.empty:
                snap_ft = ok.iloc[0]["fetch_time"]  # 最新且覆盖充分且完整的段
        q = """
        WITH snap AS (
            SELECT v.*
            FROM valuation_snapshot v
            WHERE ? IS NULL OR v.fetch_time = ?
        )
        SELECT
            s.symbol,
            s.market,
            s.list_date,
            v.name AS name6,
            v.price, v.float_mv, v.total_mv, v.turnover,
            v.pe_ttm, v.pb, v.amount, v.float_shares
        FROM snap v
        JOIN symbols s ON s.symbol = v.symbol AND s.is_active = TRUE
        """
        df = con.execute(q, [snap_ft, snap_ft]).fetchdf()
        if df.empty:
            # 兜底: 段防护失败时退化为每 symbol 最新 fetch_time(原逻辑)
            df = con.execute("""
                WITH snap AS (
                    SELECT v.*
                    FROM valuation_snapshot v
                    JOIN (SELECT symbol, max(fetch_time) mt
                          FROM valuation_snapshot GROUP BY symbol) m
                      ON v.symbol = m.symbol AND v.fetch_time = m.mt
                )
                SELECT s.symbol, s.market, s.list_date,
                       v.name AS name6, v.price, v.float_mv, v.total_mv,
                       v.turnover, v.pe_ttm, v.pb, v.amount, v.float_shares
                FROM snap v
                JOIN symbols s ON s.symbol = v.symbol AND s.is_active = TRUE
            """).fetchdf()
        if df.empty:
            return df
        df["canon"] = df.apply(
            lambda r: _db_to_canon(r["symbol"], r["market"]), axis=1
        )
        return df

    # P4: no-lookahead 候选池. 在指定历史日 as_of 上用当时已上市(symbols.list_date
    # <= as_of)且当日有日线(daily_bars 存在 <= as_of 的bar)的标的构建候选池,
    # price/amount 来自该 bar, 不触碰 valuation_snapshot 最新快照(内含未来估值
    # 与价格, 会引入前视偏差)。float_mv/turnover/pe_ttm/pb 等估值快照列无法
    # 严格按历史日对齐, 回测中留空, 由打分侧回退到 bars 可得的字段。
    def get_universe_asof(self, as_of) -> pd.DataFrame:
        if _h5i_enabled():
            try:
                return _universe_asof_h5i(as_of)
            except Exception:
                return pd.DataFrame()
        con = self._conn()
        try:
            df = con.execute(
                """
                SELECT s.symbol, s.market, s.list_date,
                       b.close AS price, b.amount
                FROM symbols s
                JOIN (
                    SELECT d.symbol,
                           MAX(d.date) AS bar_date
                    FROM daily_bars d
                    WHERE d.date <= ? AND d.close > 0
                    GROUP BY d.symbol
                ) m ON m.symbol = s.symbol
                JOIN daily_bars b
                  ON b.symbol = m.symbol AND b.date = m.bar_date
                WHERE s.is_active = TRUE
                  AND (s.list_date IS NULL OR s.list_date <= ?)
                """,
                [as_of, as_of],
            ).fetchdf()
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df
        df["canon"] = df.apply(
            lambda r: _db_to_canon(r["symbol"], r["market"]), axis=1
        )
        return df

    # P4: no-lookahead 最新一期财务. 只取 report_date <= as_of 的报告期, 避免
    # 用 as_of 之后才披露的财报预测当日前景(前视偏差)。无报告返回 {}。
    def get_financials_asof(self, canon: str, as_of) -> dict:
        if _h5i_enabled():
            try:
                return self._h5i_fin(canon, as_of=as_of)
            except Exception:
                return {}
        con = self._conn()
        symbol6 = _canon_to_db(canon)
        row = con.execute(
            """
            SELECT report_date, roe, roe_avg, liability, total_assets,
                   eps, profit_yoy, gross_margin, net_margin, operate_cf
            FROM financials
            WHERE symbol = ? AND report_date IS NOT NULL AND report_date <= ?
            ORDER BY report_date DESC LIMIT 1
            """,
            [symbol6, as_of],
        ).fetchone()
        if not row:
            return {}
        liab = row[3]
        assets = row[4]
        liab_ratio = (liab / assets) if (liab and assets and assets > 0) else None
        fin = {
            "report_date": str(row[0])[:10] if row[0] else None,
            "roe": row[1],
            "roe_avg": row[2],
            "liability_ratio": liab_ratio,
            "eps": row[5],
            "profit_yoy": row[6],
            "gross_margin": row[7],
            "net_margin": row[8],
            "operate_cf": row[9],
        }
        return {k: v for k, v in fin.items() if v is not None}

    # ---------- 批量预热 (m4 性能) ----------
    def prefetch_daily_bars(self, as_of=None, n: int = 200) -> bool:
        """预热 h5i daily_bars 到进程级内存窗 (selector 全池逐 symbol 读取加速).

        取 [最近 ~400 自然日 .. as_of/最新] 整窗日线 (含全部 symbol) 缓存;
        之后 get_bars 命中同窗口时改为内存切片, 把 5000+ 标的逐条全表扫描
        降到 ~1 次批量读取. 语义/数值与逐条 SQL 完全一致 (纯加速, 不改数据).
        """
        if not _h5i_enabled():
            return False
        if int(n) > 230:
            return False  # 超出缓存窗覆盖, 让 get_bars 走原逐条路径
        try:
            s = _h5i_store()
            if s is None:
                return False
            mdf = s._db.sql("SELECT MAX(CAST(ts AS DATE)) m FROM daily_bars").to_pandas()
            m = pd.Timestamp(mdf.iloc[0, 0]) if (mdf is not None and len(mdf)
                                                 and mdf.iloc[0, 0] is not None) else None
            if m is None:
                return False
            end = pd.Timestamp(as_of) if as_of is not None else m
            if end > m:
                end = m
            key = str(as_of)[:10] if as_of is not None else "latest"
            if _H5I_BULK_SLOT["key"] == key and _H5I_BULK_SLOT["frame"] is not None:
                return True
            lo = end - pd.Timedelta(days=_H5I_BULK_BACK_DAYS)
            df = s._db.sql(
                "SELECT symbol, CAST(ts AS DATE) AS d, open, high, low, close, "
                "volume, amount, change_pct, turnover FROM daily_bars "
                f"WHERE CAST(ts AS DATE) >= DATE '{lo:%Y-%m-%d}' "
                f"AND CAST(ts AS DATE) <= DATE '{end:%Y-%m-%d}'"
            ).to_pandas()
            if df is None or df.empty:
                return False
            _H5I_BULK_SLOT.update({"key": key, "frame": df, "window_days": _H5I_BULK_BACK_DAYS})
            return True
        except Exception:
            return False

    # ---------- 单标的历史日线 (用 daily_bars: 纯6位, 最新交易日) ----------
    def get_bars(self, canon: str, n: int = 200, as_of=None) -> pd.DataFrame:
        """返回单标的最近 n 根日线 (截至 as_of, 若指定), 列: date, open, high,
        low, close, volume, amount, change_pct; 按 date 升序。

        as_of: P4 支持 no-lookahead 回测——限定只取 date <= as_of 的K线，
               避免用 day 之后未来K线计算当日因子/信号造成前视偏差。
        """
        if _h5i_enabled():
            try:
                # 命中批量预热窗 -> 内存切片 (加速, 数值与逐条 SQL 一致)
                if int(n) <= 230 and _H5I_BULK_SLOT["frame"] is not None:
                    key = str(as_of)[:10] if as_of is not None else "latest"
                    if _H5I_BULK_SLOT["key"] == key:
                        g = _H5I_BULK_SLOT["frame"]
                        sub = g[g["symbol"] == _canon_to_db(canon)]
                        if sub is not None and not sub.empty:
                            df = sub.sort_values("d").tail(int(n)).rename(columns={"d": "date"})
                            keep = [c for c in ("date", "open", "high", "low", "close",
                                                "volume", "amount", "change_pct")
                                    if c in df.columns]
                            return df[keep].sort_values("date").reset_index(drop=True)
                s = _h5i_store()
                df = s.bars(_canon_to_db(canon), end=as_of)
                if df is not None and not df.empty:
                    df = df.tail(n).rename(columns={"d": "date"})
                    keep = [c for c in ("date", "open", "high", "low", "close",
                                        "volume", "amount", "change_pct") if c in df.columns]
                    return df[keep].sort_values("date").reset_index(drop=True)
            except Exception:
                pass
            # h5i 模式兜底: DuckDB 已退役时不再回退 duck 源 (降级为空)
            if not duck_available():
                return pd.DataFrame()
        con = self._conn()
        symbol6 = _canon_to_db(canon)
        params = [symbol6]
        date_where = ""
        if as_of is not None:
            date_where = " AND date <= ?"
            params.append(as_of)
        df = con.execute(
            f"""
            SELECT date, open, high, low, close, volume, amount, change_pct
            FROM daily_bars WHERE symbol = ? {date_where} ORDER BY date DESC LIMIT ?
            """,
            params + [n],
        ).fetchdf()
        if df.empty:
            # 降级: 尝试 bars 表(带后缀)兜底. bars 表可能滞后(实测 max(trade_date)
            # 落后 daily_bars 约9天), 取如此陈旧K线会把过期行情当现价/新鲜数据,
            # 污染信号与复权重建.
            df = self._get_bars_fallback(canon, n, as_of=as_of)
            if not df.empty:
                # 补齐 change_pct: 降级 bars 表缺该列, 而 P1 复权重建/build_adj_close
                # 依赖它. 用 close 相邻比复算, 供下游因子/复权使用.
                if "change_pct" not in df.columns:
                    close = df["close"].astype(float)
                    df["change_pct"] = (close.pct_change() * 100.0).round(4)
                # 计算相对"真最新" daily_bars 的滞后量(而非相对 bars 自身, 后者会
                # 自我比较得0, 漏掉真正滞后). daily_bars 才是新鲜主源.
                try:
                    fb_date = pd.Timestamp(df["date"].max())
                    dly_latest = pd.Timestamp(self.latest_daily_bar_date() or fb_date)
                    lag = max((dly_latest - fb_date).days, 0)
                    stale = lag > 3
                    if stale:
                        _STALE_BAR_WARNED[0] += 1
                        if _STALE_BAR_WARNED[0] <= 20:  # 会话去重, 避免刷屏
                            print(f"[db] WARN {canon}: daily_bars 无数据, "
                                  f"降级 bars 表(max={fb_date.date()}), "
                                  f"滞后 {lag}d 于 daily_bars({dly_latest.date()})",
                                  file=sys.stderr)
                    # 实盘(live, as_of=None): 陈旧K线当现价会污染信号, 直接放弃降级,
                    # 让调用方跳过该标的; as_of(回测)路径天然安全(数据本就早于 as_of),
                    # 保留并按降级处理。
                    if stale and as_of is None:
                        return pd.DataFrame()
                except Exception:
                    pass
        if df.empty:
            return df
        df = df.sort_values("date").reset_index(drop=True)
        return df

    def _get_bars_fallback(self, canon: str, n: int, as_of=None) -> pd.DataFrame:
        con = self._conn()
        params = [canon]
        date_where = ""
        if as_of is not None:
            date_where = " AND trade_date <= ?"
            params.append(as_of)
        df = con.execute(
            f"""
            SELECT trade_date AS date, open, high, low, close, volume, amount
            FROM bars WHERE symbol = ? {date_where} ORDER BY trade_date DESC LIMIT ?
            """,
            params + [n],
        ).fetchdf()
        return df

    def has_bars(self, canon: str) -> bool:
        if _h5i_enabled():
            try:
                return _h5i_store().has(_canon_to_db(canon))
            except Exception:
                return False
        con = self._conn()
        row = con.execute(
            "SELECT count(*) FROM daily_bars WHERE symbol = ?",
            [_canon_to_db(canon)],
        ).fetchone()
        return (row[0] or 0) > 0

    def get_list_date(self, canon: str):
        if _h5i_enabled():
            sy = _h5i_symbols_df()
            if not sy.empty and "list_date" in sy.columns:
                hit = sy[sy["symbol"] == _canon_to_db(canon)]
                if len(hit):
                    v = hit.iloc[0]["list_date"]
                    return None if v is None or (hasattr(v, "isna") and bool(pd.isna(v))) else v
            if not duck_available():
                return None
        con = self._conn()
        row = con.execute(
            "SELECT list_date FROM symbols WHERE symbol = ?",
            [_canon_to_db(canon)],
        ).fetchone()
        return row[0] if row else None

    # ---------- 真实财务数据 (financials 表, 治理分输入) ----------
    def get_financials(self, canon: str) -> dict:
        """返回某标的最新一期真实财务指标, 无数据返回 {}.
        """
        if _h5i_enabled():
            try:
                return self._h5i_fin(canon, as_of=None)
            except Exception:
                return {}
        con = self._conn()
        symbol6 = _canon_to_db(canon)
        # P11 容错: financials 表可能被第三方同步改写表结构(实测缺失 report_date/
        # roe 列), 结构不符时返回 {} 走快照占位, 不冒泡打断 select 全流程.
        try:
            row = con.execute(
                """
                SELECT 报告期, 净资产收益率, "净资产收益率-摊薄",
                       流动比率, 流动比率,
                       基本每股收益, 净利润同比增长率, 销售毛利率,
                       销售净利率, 每股经营现金流
                FROM financials
                WHERE symbol = ? AND 报告期 IS NOT NULL
                ORDER BY 报告期 DESC LIMIT 1
                """,
                [symbol6],
            ).fetchone()
        except Exception:
            return {}
        if not row:
            return {}
        # 列序号: 0 报告期, 1 roe, 2 roe_avg, 3 liability, 4 total_assets,
        #         5 eps, 6 profit_yoy, 7 gross_margin, 8 net_margin, 9 operate_cf
        liab = row[3]
        assets = row[4]
        liab_ratio = None
        fin = {
            "report_date": str(row[0])[:10] if row[0] else None,
            "roe": row[1],
            "roe_avg": row[2],
            "liability_ratio": liab_ratio,
            "eps": row[5],
            "profit_yoy": row[6],
            "gross_margin": row[7],
            "net_margin": row[8],
            "operate_cf": row[9],
        }
        return {k: v for k, v in fin.items() if v is not None}

    # ---------- 估值数据 (valuation 表, pb_rev 因子) ----------
    def _h5i_fin(self, canon: str, as_of=None) -> dict:
        """h5i.financials 最新一期(可截至 as_of). 列: ts, roe, roe_diluted, eps,
        np_yoy, gross_margin, net_margin, ocf_ps, debt_ratio, ..."""
        s = _h5i_store()
        sym = _canon_to_db(canon)
        cond = f"symbol='{sym}'"
        if as_of is not None:
            cond += f" AND ts <= TIMESTAMP '{as_of}'"
        df = s._db.sql(
            f"SELECT CAST(ts AS DATE) d, roe, roe_diluted, eps, np_yoy, "
            f"gross_margin, net_margin, ocf_ps, debt_ratio FROM financials "
            f"WHERE {cond} ORDER BY ts DESC LIMIT 1").to_pandas()
        if len(df) == 0:
            return {}
        x = df.iloc[0]
        def num(v):
            import math
            try:
                f = float(v)
                return f if not math.isnan(f) else None
            except Exception:
                return None
        fin = {
            "report_date": str(x["d"])[:10] if pd.notna(x["d"]) else None,
            "roe": num(x["roe"]),
            "roe_avg": num(x["roe_diluted"]),
            "eps": num(x["eps"]),
            "profit_yoy": num(x["np_yoy"]),
            "gross_margin": num(x["gross_margin"]),
            "net_margin": num(x["net_margin"]),
            "operate_cf": num(x["ocf_ps"]),
        }
        # 注: duck 原实现 liability_ratio 恒 None, 为迁移前后行为逐位一致暂不注入;
        # 负债率数据已就绪(debt_ratio), 后续因子重建时在 h5i 原生层启用.
        return {k: v for k, v in fin.items() if v is not None}

    def get_valuation(self, canon: str) -> dict:
        if _h5i_enabled():
            try:
                s = _h5i_store()
                df = s._db.sql(
                    f"SELECT CAST(CAST(ts AS DATE) AS VARCHAR) d, pe_ttm, pb, ps_ttm, "
                    f"market_cap FROM valuation WHERE symbol='{_canon_to_db(canon)}' "
                    f"ORDER BY ts DESC LIMIT 1").to_pandas()
                if len(df) == 0:
                    return {}
                x = df.iloc[0]
                out = {"trade_date": x["d"], "pe_ttm": x["pe_ttm"], "pb": x["pb"],
                       "ps_ttm": x["ps_ttm"], "market_cap": x["market_cap"]}
                return {k: (None if v is None or (hasattr(v, "isna") and bool(pd.isna(v))) else v)
                        for k, v in out.items()}
            except Exception:
                return {}
        con = self._conn()
        symbol6 = _canon_to_db(canon)
        try:
            row = con.execute(
                """SELECT trade_date, pe_ttm, pb, ps_ttm, market_cap
                   FROM valuation WHERE symbol = ? AND trade_date IS NOT NULL
                   ORDER BY trade_date DESC LIMIT 1""",
                [symbol6],
            ).fetchone()
        except Exception:
            return {}
        if not row:
            return {}
        return {
            "trade_date": str(row[0])[:10] if row[0] else None,
            "pe_ttm": row[1],
            "pb": row[2],
            "ps_ttm": row[3],
            "market_cap": row[4],
        }

    # ---------- 资金流数据 (money_flow 表, mf_net 因子) ----------
    def get_money_flow(self, canon: str) -> dict:
        # money_flow (盘中资金流) 未迁入 h5i; DuckDB 退役后返回 {} (mf_net 中性化)
        if not duck_available():
            return {}
        con = self._conn()
        symbol6 = _canon_to_db(canon)
        try:
            row = con.execute(
                """SELECT 最新价, 净额, 成交额, fetch_time
                   FROM money_flow WHERE symbol = ? AND fetch_time IS NOT NULL
                   ORDER BY fetch_time DESC LIMIT 1""",
                [symbol6],
            ).fetchone()
        except Exception:
            return {}
        if not row:
            return {}
        return {
            "price": row[0],
            "net_flow": row[1],
            "amount": row[2],
            "fetch_time": str(row[3])[:19] if row[3] else None,
        }

    # ---------- 辅助: 最新交易日/估值快照日期 ----------
    def latest_bar_date(self) -> "pd.Timestamp | None":
        if not duck_available():
            # DuckDB 已退役: bars 表不存在, 回退 h5i daily_bars 最新日
            return self.latest_daily_bar_date()
        con = self._conn()
        row = con.execute("SELECT max(trade_date) FROM bars").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def latest_daily_bar_date(self) -> "pd.Timestamp | None":
        """daily_bars(新鲜主源)的最新 bar 日期. 用于衡量 bars 表降级数据的滞后.
        P7: 旧实现用 bars 自身 max(trade_date) 作"最新", 做自我比较恒得0, 漏掉
        真正滞后; 应对比 daily_bars 的新鲜度."""
        if _h5i_enabled():
            try:
                days = _h5i_store().trading_days()
                return pd.Timestamp(days[-1]) if days else None
            except Exception:
                return None
        if not duck_available():
            return None
        con = self._conn()
        row = con.execute("SELECT max(date) FROM daily_bars").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    def latest_valuation_fetch(self) -> "pd.Timestamp | None":
        if _h5i_enabled():
            try:
                s = _h5i_store()
                df = s._db.sql(
                    "SELECT MAX(ts) m FROM valuation_snapshot").to_pandas()
                v = df.iloc[0, 0] if df is not None and len(df) else None
                return pd.Timestamp(v) if v is not None else None
            except Exception:
                return None
        if not duck_available():
            return None
        con = self._conn()
        row = con.execute("SELECT max(fetch_time) FROM valuation_snapshot").fetchone()
        return pd.Timestamp(row[0]) if row and row[0] else None

    # ---------- P2: 分红/除权事件 ----------
    def corporate_actions_due(self, canons: list, as_of: "date",
                              applied: set = (),
                              since_dates: dict | None = None) -> list:
        """返回持仓标的在 as_of 当日或之前除权、且尚未记账的送转/分红事件.

        P2: realtime_engine 每日在估值前调用, 把除权事件按真实 A 股规则入账
        (送转股平摊成本、现金分红按持有期计税入现金), 避免除权日账面市值
        因不复权原始价骤降而出现虚假亏损。

        since_dates: {symbol6: 'YYYY-MM-DD'} 各标的建仓日下限. 提供时仅返回
                     ex_date >= 建仓日的除权事件(只有持仓期间才应入账), 杜绝
                     把买入前几十年的历史送转/分红一次性入账导致持仓数量虚增、
                     账面收益虚假放大的记账bug。未提供的标的维持原行为(不设下限)。

        返回: [{symbol, ex_date, action_type, bonus_ratio, dividend_cash}]。
        """
        if not canons:
            return []
        if not duck_available():
            # corporate_actions 未迁入 h5i; DuckDB 退役后除权事件入账降级为空
            return []
        con = self._conn()
        syms = [_canon_to_db(c) for c in canons]
        since_clause = ""
        params: list = [syms, as_of]
        if since_dates:
            # 规范化 since_dates 的 key 为纯6位, 兼容调用方传 canon/纯码两种格式
            ndates = {_canon_to_db(k): v for k, v in since_dates.items()}
            conds = []
            for s in syms:
                d = ndates.get(s)
                if d:
                    conds.append("(symbol = ? AND ex_date >= ?)")
                    params.extend([s, str(d)])
            if conds:
                since_clause = " AND (" + " OR ".join(conds) + ")"
        rows = con.execute(
            f"""
            SELECT symbol, ex_date, action_type, bonus_ratio, dividend_cash
            FROM corporate_actions
            WHERE symbol = ANY(?) AND ex_date IS NOT NULL AND ex_date <= ?
              AND action_type = '分红送转'
              {since_clause}
            ORDER BY ex_date
            """,
            params,
        ).fetchall()
        out = []
        for r in rows:
            key = f"{r[0]}|{r[1]}|{r[3]}|{r[4]}"
            if key in applied:
                continue
            out.append({
                "symbol": r[0], "ex_date": r[1], "action_type": r[2],
                "bonus_ratio": float(r[3]) if r[3] else 0.0,
                "dividend_cash": float(r[4]) if r[4] else 0.0,
                "key": key,
            })
        return out


# ---------- 候选池过滤规则 (独立纯函数, 便于测试) ----------
def filter_universe(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """按 POOL_FILTER 过滤候选池, 返回带 canon 的子集

    as_of: P5/P4——可选历史日(YYYY-MM-DD 或可解析日期)。提供时次新 cutoff
           以 as_of 为基准(用该日往前推 exclude_new 天), 既避免硬编码日期,
           也保证 no-lookahead 回测不泄漏 as_of 之后才上市的标的。
           缺省用"今天"(与实盘实时选股一致)。
    """
    if df.empty:
        return df
    p = POOL_FILTER
    out = df.copy()

    # —— 源头硬化: 剔除一切非 A 股代码段 (可转债 110/111/113/123/127/128,
    # B股 200/900, 北交所 920/8xx/4xx) ——
    # 这是可转债污染 target_plan 的真正根因: valuation_snapshot/symbols
    # 混入的债券有完整 K 线, 可被正常打分入选. 原 filter_universe 只剔了
    # 920 一段, 债券段全部漏过.
    if "symbol" in out.columns:
        out["_is_a"] = out["symbol"].map(is_a_share_symbol)
        out = out[out["_is_a"]].drop(columns=["_is_a"])
        if out.empty:
            return out

    if p["exclude_new"] and p["exclude_920_bse"] and "symbol" in out.columns:
        # 防御性剔除920段(北交所代码段, 数据源偶发把 market 误标为 sh)。
        # 候选池来源 valuation_snapshot 通常不含920段; 此规则防止未来收录后误入。
        out = out[~out["symbol"].astype(str).str.startswith("920")]

    if p["exclude_st"] and "name6" in out.columns:
        # 排除 ST/*ST/退 (名称含 'ST'/'*ST'/'退')
        out = out[~out["name6"].astype(str).str.contains(r"ST|退", case=False)]

    # ST 过滤(历史路径补充, 2026-09-12): 历史宇宙无名称列(valuation 表无 name,
    # symbols parquet 亦无 name), 故用 PIT 的 is_st 标记剔除——该字段为当日状态,
    # 有值即当日 ST, 无偏且不引入后视; 当前覆盖率约 6%(有值即剔, 缺失不剔)。
    if p["exclude_st"] and "is_st" in out.columns:
        st = out["is_st"]
        if st.notna().any():
            out = out[st.ne(True)]      # NaN(未覆盖)保留, True(ST)剔除

    # 流通市值过滤 (float_mv 单位=亿)
    if "float_mv" in out.columns:
        fv = pd.to_numeric(out["float_mv"], errors="coerce")
        # P11 单位归一: 历史引擎路径曾写入"元"单位(实测 1.02e12 系中际旭创≈10210亿),
        # 而正常口径是"亿"(08-28 段 528.69). 若整体量级落在"元"区间(绝大多数 >1e6),
        # 统一除以 1e8 归一为亿, 避免市值过滤按"亿"口径把全池抽干.
        med = fv.median() if fv.notna().any() else 0.0
        if med > 1e6:  # 元量级 → 归一为亿 (分母 1e8)
            out = out.copy()
            out["float_mv"] = fv / 1e8
            fv = pd.to_numeric(out["float_mv"], errors="coerce")
        # 缺失值保留(2026-09-12): 历史区间市值可能整体缺失, 若按 NaN 剔除会
        # 把候选池清空(PIT 回退后曾出现"现场选股 0 只"), 故 NaN 不参与该过滤。
        out = out[fv.isna() | ((fv >= p["min_float_market_cap"] / 1e8) &
                               (fv <= p["max_float_market_cap"] / 1e8))]

    # 换手率过滤 (turnover 单位=%) -- px turnover在快照里是%
    if "turnover" in out.columns and p["min_avg_turnover"]:
        tv = pd.to_numeric(out["turnover"], errors="coerce")
        out = out[tv >= p["min_avg_turnover"]]

    # 剔除无价格/无流动性的
    if "amount" in out.columns:
        amt = pd.to_numeric(out["amount"], errors="coerce")
        out = out[amt > 0]

    # 次新股过滤 (上市不足 natural days)。list_date 可能缺失(None)时容错跳过
    if "list_date" in out.columns and p["exclude_new"]:
        ld = pd.to_datetime(out["list_date"], errors="coerce")
        known = ld.notna()
        if known.any():  # 有可靠上市日期的才过滤, 缺失的放过(由数据长度兜底)
            # P5: cutoff 基准 = as_of(历史日)否则今天; 由基准日往前推 exclude_new 天.
            # 不再使用任何硬编码日期, 避免日期失效/过严, 且 no-lookahead 回测
            # 用 as_of 保证不泄漏基准日之后才上市的标的同时刻看次新口径一致。
            base = pd.Timestamp(as_of) if as_of is not None else \
                pd.Timestamp(pd.Timestamp.today().date())
            cutoff = base - pd.Timedelta(days=int(p["exclude_new"]))
            out = out[~known | (ld <= cutoff)]

    out = out.reset_index(drop=True)
    return out
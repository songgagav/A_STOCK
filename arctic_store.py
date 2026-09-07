# ============================================================
# arctic_store.py -- ArcticDB 统一数据访问层
#
# 6 个库:
#   bars            全 A 时序 (按日复权, 供 vnpy 快速读取, 替代 DuckDB)
#   trade_records   vnpy + realtime 逐笔成交 (索引=ts)
#   daily_summary   盘后 daily_summary.json 完整快照 (索引=day)
#   perf_report     performance_report 完整快照 (索引=day)
#   factor_ic       因子 IC 序列 (索引=date, 每因子一个 symbol)
#   reward_curve    DRL reward 序列 (索引=step)
#
# 写入 API: append (append-only, 不覆盖)
# 读取 API: read_range / read_all
# 工具 API: list_symbols / read_latest
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Iterable

import pandas as pd

_LOG = logging.getLogger("arctic_store")

# 单例锁: Arctic 在多线程下应复用同一连接
_LOCK = threading.Lock()
_INSTANCE: "ArcticStore | None" = None


# ArcticDB URI (LMDB 本地嵌入库)
_ARCTIC_URI = os.environ.get("ARCTIC_URI")
ARCTIC_URI = _ARCTIC_URI or "lmdb://" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "arcticdb")

# 库名常量
LIB_BARS = "bars"
LIB_TRADES = "trade_records"
LIB_DAILY = "daily_summary"
LIB_PERF = "perf_report"
LIB_IC = "factor_ic"
LIB_REWARD = "reward_curve"

ALL_LIBRARIES = (LIB_BARS, LIB_TRADES, LIB_DAILY, LIB_PERF, LIB_IC, LIB_REWARD)


class ArcticStore:
    """ArcticDB 数据访问层 (LMDB 后端, 单进程单实例).

    设计原则:
    - 任何写入都 append-only, 同一 key 重复写入会覆盖 (ArcticDB 默认 update). 我们只在初次
      写入或明确 append 时使用. 逐笔成交使用 ts_ms 作为 index, 天然唯一.
    - 库不存在时自动创建 (lazily), 不要求预先 init.
    - 失败一律 log.warning, 不抛异常 (上游 run_daily 链路要求容错).
    """

    def __init__(self, uri: str = ARCTIC_URI):
        import arcticdb as adb
        # 创建本地 LMDB 路径
        local = uri.replace("lmdb://", "")
        try:
            os.makedirs(local, exist_ok=True)
        except Exception:
            pass
        self.uri = uri
        self._adb = adb
        self._ac = adb.Arctic(uri)
        self._lib_cache: dict[str, Any] = {}
        for lib in ALL_LIBRARIES:
            try:
                self._ac.get_library(lib)
            except Exception:
                try:
                    self._ac.create_library(lib)
                except Exception as e:
                    _LOG.warning(f"创建 ArcticDB 库 {lib} 失败: {e}")

    # ---------- 内部 ----------
    def _lib(self, name: str):
        if name not in self._lib_cache:
            try:
                self._lib_cache[name] = self._ac.get_library(name)
            except Exception as e:
                _LOG.warning(f"获取 ArcticDB 库 {name} 失败: {e}")
                return None
        return self._lib_cache[name]

    # ========================================================================
    # bars -- 全 A 时序, key=symbol (canon), index=date
    # ========================================================================
    def write_bars(self, symbol: str, df: pd.DataFrame) -> bool:
        """写入 bars[symbol]. df 必须含 date 列, 设为 index.
        重复写入会 update (按 date 主键覆盖)."""
        if df is None or df.empty:
            return False
        df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        elif not isinstance(df.index, pd.DatetimeIndex):
            return False
        lib = self._lib(LIB_BARS)
        if not lib:
            return False
        try:
            lib.write(symbol, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB write_bars({symbol}) 失败: {e}")
            return False

    def read_bars(self, symbol: str,
                  date_range: tuple | None = None) -> pd.DataFrame | None:
        lib = self._lib(LIB_BARS)
        if not lib:
            return None
        try:
            if date_range:
                q = lib.read(symbol, date_range=date_range)
            else:
                q = lib.read(symbol)
            return q.data
        except Exception as e:
            _LOG.warning(f"ArcticDB read_bars({symbol}) 失败: {e}")
            return None

    def list_bars_symbols(self) -> list[str]:
        lib = self._lib(LIB_BARS)
        if not lib:
            return []
        try:
            return sorted(lib.list_symbols())
        except Exception:
            return []

    # ========================================================================
    # trade_records -- 逐笔成交, key=day_symbol_ts, index=ts
    # ========================================================================
    def append_trade(self, trade: dict) -> bool:
        """trade: {
            'ts': datetime 或 ISO str, 'day': 'YYYY-MM-DD', 'symbol': 'canon',
            'direction': 'buy'/'sell', 'qty': int, 'price': float,
            'fee': float, 'pnl': float (可选), 'source': 'vnpy_backtest'/'paper_book',
        }
        每笔成交单独写入, 索引=ts (millisecond precision).
        """
        ts = trade.get("ts")
        if ts is None:
            ts = dt.datetime.now()
        if isinstance(ts, str):
            ts = dt.datetime.fromisoformat(ts)
        symbol = trade.get("symbol") or trade.get("canon") or "unknown"
        row = {k: v for k, v in trade.items() if k not in ("ts",)}
        row["ts"] = pd.Timestamp(ts)
        df = pd.DataFrame([row]).set_index("ts")
        sym_key = f"{trade.get('day', dt.date.today().strftime('%Y-%m-%d'))}_{symbol}_{int(pd.Timestamp(ts).timestamp() * 1000)}"
        lib = self._lib(LIB_TRADES)
        if not lib:
            return False
        try:
            lib.write(sym_key, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB append_trade 失败: {e}")
            return False

    def read_trades(self, day: str | None = None,
                    symbol: str | None = None,
                    date_range: tuple | None = None) -> pd.DataFrame | None:
        """读成交. day='2026-08-26' 或 None(全部)."""
        lib = self._lib(LIB_TRADES)
        if not lib:
            return None
        try:
            syms = lib.list_symbols()
            if day:
                syms = [s for s in syms if s.startswith(day.replace("-", ""))]
            if symbol:
                syms = [s for s in syms if symbol in s]
            if not syms:
                return pd.DataFrame()
            dfs = []
            for s in syms:
                try:
                    q = lib.read(s, date_range=date_range) if date_range else lib.read(s)
                    dfs.append(q.data)
                except Exception:
                    continue
            if not dfs:
                return pd.DataFrame()
            return pd.concat(dfs).sort_index()
        except Exception as e:
            _LOG.warning(f"ArcticDB read_trades 失败: {e}")
            return None

    # ========================================================================
    # daily_summary -- key=day
    # ========================================================================
    def write_daily_summary(self, day: str, summary: dict) -> bool:
        """写盘后 daily_summary. summary 为 dict, 序列化为单行 DataFrame."""
        lib = self._lib(LIB_DAILY)
        if not lib:
            return False
        # 序列化为 JSON 字符串保留复杂结构
        row = {"summary_json": json.dumps(summary, ensure_ascii=False, default=str),
               "equity": summary.get("summary", {}).get("equity") or summary.get("equity")}
        df = pd.DataFrame([row], index=pd.to_datetime([day]))
        df.index.name = "day"
        try:
            lib.write(day, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB write_daily_summary({day}) 失败: {e}")
            return False

    def read_daily_summaries(self, days: int = 60) -> pd.DataFrame | None:
        lib = self._lib(LIB_DAILY)
        if not lib:
            return None
        try:
            syms = sorted(lib.list_symbols(), reverse=True)[:days]
            if not syms:
                return pd.DataFrame()
            dfs = []
            for s in syms:
                try:
                    dfs.append(lib.read(s).data.assign(day=s))
                except Exception:
                    continue
            return pd.concat(dfs) if dfs else pd.DataFrame()
        except Exception as e:
            _LOG.warning(f"ArcticDB read_daily_summaries 失败: {e}")
            return None

    # ========================================================================
    # perf_report -- key=day
    # ========================================================================
    def write_perf_report(self, day: str, report: dict) -> bool:
        lib = self._lib(LIB_PERF)
        if not lib:
            return False
        # 提取关键指标为列 (供 SPC / 退化检测快速查询)
        m = (report or {}).get("metrics") or {}
        b = (report or {}).get("benchmark") or {}
        row = {
            "summary_json": json.dumps(report, ensure_ascii=False, default=str),
            "total_return": m.get("total_return"),
            "annual_return": m.get("annual_return"),
            "max_drawdown": m.get("max_drawdown"),
            "sharpe_annual": m.get("sharpe_annual"),
            "calmar": m.get("calmar"),
            "excess_total": b.get("excess_total"),
        }
        df = pd.DataFrame([row], index=pd.to_datetime([day]))
        df.index.name = "day"
        try:
            lib.write(day, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB write_perf_report({day}) 失败: {e}")
            return False

    def read_perf_reports(self, days: int = 60) -> pd.DataFrame | None:
        lib = self._lib(LIB_PERF)
        if not lib:
            return None
        try:
            syms = sorted(lib.list_symbols(), reverse=True)[:days]
            if not syms:
                return pd.DataFrame()
            dfs = []
            for s in syms:
                try:
                    d = lib.read(s).data
                    if not d.empty:
                        # 展平指标列
                        flat = {c: d[c].iloc[0] for c in d.columns if c != "summary_json"}
                        flat["day"] = s
                        dfs.append(flat)
                except Exception:
                    continue
            return pd.DataFrame(dfs).set_index("day").sort_index() if dfs else pd.DataFrame()
        except Exception as e:
            _LOG.warning(f"ArcticDB read_perf_reports 失败: {e}")
            return None

    # ========================================================================
    # factor_ic -- key=factor, index=date
    # ========================================================================
    def append_factor_ic(self, factor: str, day: str, ic: float,
                         n: int = 0, recent_mean20: float | None = None) -> bool:
        lib = self._lib(LIB_IC)
        if not lib:
            return False
        row = {"ic": float(ic), "n": int(n), "recent_mean20": recent_mean20}
        df = pd.DataFrame([row], index=pd.to_datetime([day]))
        df.index.name = "date"
        try:
            existing = lib.read(factor).data if factor in lib.list_symbols() else None
            if existing is not None and not existing.empty:
                # 重复日期更新, 不重复日期 append; 合并后必须严格升序去重
                df = pd.concat([existing, df])
                df = df[~df.index.duplicated(keep="last")]
                df = df.sort_index()
            else:
                df = df.sort_index()
            lib.write(factor, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB append_factor_ic 失败: {e}")
            return False

    def read_factor_ic(self, factor: str) -> pd.DataFrame | None:
        lib = self._lib(LIB_IC)
        if not lib or factor not in lib.list_symbols():
            return None
        try:
            return lib.read(factor).data
        except Exception as e:
            _LOG.warning(f"ArcticDB read_factor_ic({factor}) 失败: {e}")
            return None

    # ========================================================================
    # reward_curve -- key=day, index=step
    # ========================================================================
    def append_reward_curve(self, day: str, rewards: list[float],
                            weights_trace: list[list[float]] | None = None) -> bool:
        lib = self._lib(LIB_REWARD)
        if not lib:
            return False
        rows = [{"step": i, "reward": float(r)} for i, r in enumerate(rewards)]
        if weights_trace and len(weights_trace) == len(rewards):
            for i, w in enumerate(weights_trace):
                rows[i]["weights_json"] = json.dumps([float(x) for x in w])
        df = pd.DataFrame(rows)
        if df.empty:
            return False
        try:
            lib.write(day, df)
            return True
        except Exception as e:
            _LOG.warning(f"ArcticDB append_reward_curve({day}) 失败: {e}")
            return False

    def read_reward_curve(self, day: str) -> pd.DataFrame | None:
        lib = self._lib(LIB_REWARD)
        if not lib or day not in lib.list_symbols():
            return None
        try:
            return lib.read(day).data
        except Exception as e:
            _LOG.warning(f"ArcticDB read_reward_curve({day}) 失败: {e}")
            return None

    # ---------- 健康检查 ----------
    def health_check(self) -> dict:
        return {
            "uri": self.uri,
            "libraries": {
                lib: (lib in self._ac.list_libraries())
                for lib in ALL_LIBRARIES
            },
            "stats": {
                lib: len(self._lib(lib).list_symbols()) if self._lib(lib) else 0
                for lib in ALL_LIBRARIES
            },
            "read_probe": self.probe_reads(sample=3),
        }

    def probe_reads(self, sample: int = 3) -> dict:
        """真实读探测: 抽样每个库的 symbol 实际 read 回数据, 验证"能读到最新数据".

        背景: health_check 原只验证 list_libraries()+list_symbols() 计数, 测不出
        "库存在但 symbol 读取失败"(如 E_NO_SUCH_VERSION / 数据损坏). 本方法对每个库
        抽样 sample 个 symbol 做 lib.read(), 记录读取是否成功 + 返回行数 + 最新索引日期.
        这是"能连接"之外的"能读"校验, 供 premarket_healthcheck 断言.
        """
        probe: dict[str, Any] = {}
        for libname in ALL_LIBRARIES:
            lib = self._lib(libname)
            if lib is None:
                probe[libname] = {"ok": False, "error": "库不可用"}
                continue
            try:
                syms = lib.list_symbols()
            except Exception as e:
                probe[libname] = {"ok": False, "error": f"list_symbols 失败: {type(e).__name__}: {e}"}
                continue
            if not syms:
                probe[libname] = {"ok": True, "symbols": 0, "note": "空库(0 symbol, 允许)"}
                continue
            # 抽样: 取最前面的 sample 个 + 中间均匀抽 1 个
            chosen = syms[:sample]
            if len(syms) > sample:
                chosen.append(syms[len(syms) // 2])
            read_fail = []
            latest_ts = None
            for s in chosen:
                try:
                    q = lib.read(s)
                    df = q.data
                    if df is None or len(df) == 0:
                        read_fail.append(f"{s}: 空")
                        continue
                    # 取索引或最后一列的最后非空作为"最新时间"
                    ts = self._last_ts(df)
                    if ts is not None:
                        ts_s = pd.Timestamp(ts).strftime("%Y-%m-%d")
                        if latest_ts is None or ts_s > latest_ts:
                            latest_ts = ts_s
                except Exception as e:
                    read_fail.append(f"{s}: {type(e).__name__}")
            probe[libname] = {
                "ok": not read_fail,
                "symbols": len(syms),
                "sampled": len(chosen),
                "read_fail": read_fail if read_fail else None,
                "latest_date": latest_ts,
            }
        return probe

    @staticmethod
    def _last_ts(df: pd.DataFrame):
        """取 DataFrame 最后一行时间索引或 'last_*' 口径, 失败返回 None."""
        try:
            if isinstance(df.index, pd.DatetimeIndex):
                return df.index.max()
            # 有 date/day 列则取其 max
            for col in ("date", "day", "ts"):
                if col in df.columns:
                    c = df[col].dropna()
                    if len(c):
                        return c.iloc[-1]
            return None
        except Exception:
            return None


    def probe_rw(self) -> dict:
        """读写总探测: 对每个库写入一个临时 symbol 再读回, 验证写入链路健康.

        与 probe_reads 不同: probe_reads 只读既有 symbol 验证"能读"; 本方法额外
        写一个临时 symbol 再读出并删除, 验证"能写 + 能读"双向, 供健康检查做
        数据存储层读写健壮性断言. 临时 symbol 用 uuid 命名, 探后即删, 不污染数据.
        """
        import uuid
        probe: dict[str, Any] = {}
        for libname in ALL_LIBRARIES:
            lib = self._lib(libname)
            if lib is None:
                probe[libname] = {"ok": False, "error": "库不可用"}
                continue
            temp_sym = f"__probe_{uuid.uuid4().hex}"
            try:
                # 各库索引类型不一: 用最通用 datetime index 兼容 bars/daily/reward 等
                df = pd.DataFrame({"v": [1.0]},
                                  index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")], name="ts"))
                lib.write(temp_sym, df)
                q = lib.read(temp_sym)
                df_r = q.data if q is not None else None
                ok = df_r is not None and len(df_r) == 1
                probe[libname] = {"ok": bool(ok),
                                  "write_read": True,
                                  "read_rows": int(len(df_r)) if df_r is not None else 0}
                try:
                    lib.delete(temp_sym)
                except Exception:
                    pass
            except Exception as e:
                probe[libname] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                try:
                    lib.delete(temp_sym)
                except Exception:
                    pass
        return probe


def get_store() -> ArcticStore:
    """线程安全的单例."""
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = ArcticStore()
    return _INSTANCE


if __name__ == "__main__":
    s = get_store()
    h = s.health_check()
    print(json.dumps(h, ensure_ascii=False, indent=2))
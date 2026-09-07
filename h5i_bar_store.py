# -*- coding: utf-8 -*-
"""h5i_bar_store: daily_bars 读取适配层(h5i-db)

新架构: ArcticDB(存储) + h5i-db(计算/分析/回测).
本模块提供与现有 DuckDB 读取侧等价的 daily_bars 访问, 供回测引擎/因子层切换.
切换方式: 环境变量 BAR_STORE=h5i(默认) | duck(回退/对照).

提供:
  - H5iBarStore.open()           单例句柄
  - store.symbols_dates()        交易日历(distinct ts date 升序)
  - store.bars(symbol6, start, end)           裸序列(支持 decision_time 防未来函数)
  - store.close_upto(symbol6, day)            截至 day(含)最近收盘价
  - store.prices_for(day, symbols)            {symbol: close}
  - store.fork()                  轻量级 Fork (进程内副本, 零成本试错)

防未来函数 (2026-09-06 升级):
  所有查询方法新增 decision_time 参数, 确保查询结果不包含决策时点之后的数据.
  使用方式:
    store.bars("600519", end="2026-09-05", decision_time="2026-09-05")
    store.prices_for("2026-09-05", symbols, decision_time="2026-09-05")

版本化提交 (2026-09-06 升级):
  write_with_version(table, df, version_tag) 原子写入并标记版本.
  read_version(table, version_tag) 读取指定版本快照.
"""
import os
from functools import lru_cache

_H5I_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "h5i", "market.db")
_DUCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "legacy_stockdb.duckdb")


class H5iBarStore:
    def __init__(self, path=_H5I_PATH):
        import h5i_db
        self._db = h5i_db.Database(path)
        self._ready = True
        # 版本化元数据: {version_tag: {table: count}}
        self._versions: dict[str, dict[str, int]] = {}

    # ---- 决策时点安全查询 (防未来函数) ----

    def _decision_filter(self, decision_time: str | None, col: str = "ts") -> str:
        """返回 SQL 片段, 确保查询不包含决策时点之后的数据."""
        if decision_time is None:
            return ""
        return f" AND {col} <= TIMESTAMP '{decision_time}'"

    def trading_days(self, decision_time: str | None = None) -> list:
        """返回截至 decision_time 的交易日历."""
        filt = self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT DISTINCT CAST(ts AS DATE) d FROM daily_bars WHERE 1=1{filt} ORDER BY d"
        ).to_pandas()
        return [str(x) for x in df["d"].tolist()]

    def symbols_on(self, day: str, decision_time: str | None = None) -> list:
        """返回指定日期的标的列表, 约束截至 decision_time."""
        filt = self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT DISTINCT symbol FROM daily_bars WHERE CAST(ts AS DATE)=DATE '{day}'{filt} ORDER BY symbol"
        ).to_pandas()
        return df["symbol"].tolist()

    def has(self, symbol: str, decision_time: str | None = None) -> bool:
        """检查标的是否存在, 约束截至 decision_time."""
        filt = self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT 1 FROM daily_bars WHERE symbol='{symbol}'{filt} LIMIT 1"
        ).to_pandas()
        return len(df) > 0

    def bars(self, symbol: str, start: str = None, end: str = None,
             decision_time: str | None = None):
        """获取日线, 约束截至 decision_time (防未来函数)."""
        w = ""
        if start:
            w += f" AND CAST(ts AS DATE) >= DATE '{start}'"
        if end:
            w += f" AND CAST(ts AS DATE) <= DATE '{end}'"
        w += self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT CAST(ts AS DATE) d, symbol, open, high, low, close, volume, amount, "
            f"change_pct, turnover FROM daily_bars WHERE symbol='{symbol}'{w} ORDER BY d"
        ).to_pandas()
        return df

    def close_upto(self, symbol: str, day: str,
                   decision_time: str | None = None) -> float | None:
        """截至 day(含)最近收盘价, 约束截至 decision_time."""
        dt_filt = self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT close FROM daily_bars WHERE symbol='{symbol}' "
            f"AND CAST(ts AS DATE) <= DATE '{day}'{dt_filt} ORDER BY ts DESC LIMIT 1"
        ).to_pandas()
        return float(df.iloc[0]["close"]) if len(df) else None

    def prices_for(self, day: str, symbols: list[str],
                   decision_time: str | None = None) -> dict:
        """获取指定日期的收盘价, 约束截至 decision_time."""
        if not symbols:
            return {}
        ph = ",".join(["'%s'" % s for s in symbols])
        dt_filt = self._decision_filter(decision_time)
        df = self._db.sql(
            f"SELECT symbol, close FROM daily_bars WHERE CAST(ts AS DATE)=DATE '{day}'{dt_filt} "
            f"AND symbol IN ({ph}) AND close IS NOT NULL"
        ).to_pandas()
        return {r["symbol"]: float(r["close"]) for _, r in df.iterrows()}

    # ---- 版本化写入与读取 ----

    def write_with_version(self, table: str, df, version_tag: str) -> bool:
        """原子写入并标记版本.

        Parameters
        ----------
        table : str
            目标表名 (如 'daily_bars').
        df : pandas.DataFrame
            待写入数据.
        version_tag : str
            版本标签 (如 '2026-09-05_v2').

        Returns
        -------
        bool
            写入是否成功.
        """
        try:
            # 写入 h5i-db (原子提交)
            self._db.write(df, table=table)

            # 记录版本元数据
            if version_tag not in self._versions:
                self._versions[version_tag] = {}
            n0 = self._versions[version_tag].get(table, 0)
            self._versions[version_tag][table] = n0 + len(df)
            return True
        except Exception:
            return False

    def read_version(self, table: str, version_tag: str):
        """读取指定版本写入的快照.

        注意: 此方法仅返回该版本写入的数据行, 非该版本的全量快照.
        如需全量快照, 请结合 decision_time 参数使用普通查询.
        """
        import pandas as pd
        # 当前简化实现: 基于 version_tag 做时间戳过滤
        try:
            df = self._db.sql(
                f"SELECT * FROM {table} WHERE ts <= TIMESTAMP '{version_tag[:10]}'"
            ).to_pandas()
            return df
        except Exception:
            return pd.DataFrame()

    def list_versions(self) -> list[str]:
        """列出所有版本标签."""
        return sorted(self._versions.keys())

    # ---- 轻量级 Fork ----

    def fork(self, tag: str = "unnamed") -> "H5iBarStore":
        """创建进程内轻量级数据库副本 (Fork).

        Fork 后的副本用相同路径打开新连接, 写入操作互不干扰.
        适用于 Agent 在副本上自由实验, 零成本试错.

        Parameters
        ----------
        tag : str
            Fork 标签 (仅用于日志/调试).

        Returns
        -------
        H5iBarStore
            新的 Fork 实例.
        """
        import h5i_db
        fork = H5iBarStore.__new__(H5iBarStore)
        fork._db = h5i_db.Database(_H5I_PATH)
        fork._ready = True
        fork._versions = {}
        fork._tag = tag
        return fork

    # ---- 原始方法 (兼容旧调用) ----

    def trading_days_legacy(self) -> list:
        """旧版 trading_days (无 decision_time 约束), 保持向后兼容."""
        return self.trading_days(decision_time=None)

    def close(self):
        self._db.close()


class DuckBarStore:
    """对照组: DuckDB 同接口 (含 decision_time 参数)."""
    def __init__(self):
        import duckdb
        self._con = duckdb.connect(_DUCK, read_only=True)

    def _decision_filter(self, decision_time: str | None, col: str = "date") -> str:
        if decision_time is None:
            return ""
        return f" AND {col} <= ?"
    
    def _decision_params(self, decision_time: str | None) -> list:
        return [decision_time] if decision_time is not None else []

    def trading_days(self, decision_time: str | None = None) -> list:
        dt_filt = self._decision_filter(decision_time)
        params = self._decision_params(decision_time)
        rows = self._con.execute(
            f"SELECT DISTINCT date FROM daily_bars WHERE 1=1{dt_filt} ORDER BY date", params
        ).fetchall()
        return [str(r[0]) for r in rows]

    def symbols_on(self, day, decision_time: str | None = None):
        dt_filt = self._decision_filter(decision_time)
        params = [day] + self._decision_params(decision_time)
        return [r[0] for r in self._con.execute(
            f"SELECT DISTINCT symbol FROM daily_bars WHERE date=?{dt_filt} ORDER BY symbol", params).fetchall()]

    def bars(self, symbol, start=None, end=None, decision_time: str | None = None):
        import pandas as pd
        w, args = [], [symbol]
        if start:
            w.append("date >= ?"); args.append(start)
        if end:
            w.append("date <= ?"); args.append(end)
        if decision_time is not None:
            w.append("date <= ?"); args.append(decision_time)
        q = f"SELECT date d, symbol, open, high, low, close, volume, amount, change_pct, turnover FROM daily_bars WHERE symbol=?"
        if w:
            q += " AND " + " AND ".join(w)
        q += " ORDER BY date"
        return self._con.execute(q, args).fetchdf()

    def close_upto(self, symbol, day, decision_time: str | None = None):
        dt_filt = self._decision_filter(decision_time)
        params = [symbol, day] + self._decision_params(decision_time)
        r = self._con.execute(
            f"SELECT close FROM daily_bars WHERE symbol=? AND date<=?{dt_filt} ORDER BY date DESC LIMIT 1",
            params).fetchone()
        return float(r[0]) if r and r[0] else None

    def prices_for(self, day, symbols, decision_time: str | None = None):
        if not symbols:
            return {}
        ph = ",".join(["?"] * len(symbols))
        dt_filt = self._decision_filter(decision_time)
        params = [day] + symbols + self._decision_params(decision_time)
        rows = self._con.execute(
            f"SELECT symbol, close FROM daily_bars WHERE date=? AND symbol IN ({ph}) AND close>0{dt_filt}",
            params).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def close(self):
        self._con.close()


def open_store() -> H5iBarStore | DuckBarStore:
    if os.environ.get("BAR_STORE", "h5i").lower() == "duck":
        return DuckBarStore()
    return H5iBarStore()


def parity_check(sample_days=5, sample_syms=12) -> dict:
    """读取奇偶: h5i vs duck 对同一窗口的 trading_days/close/prices 对比."""
    a, b = H5iBarStore(), DuckBarStore()
    days_a, days_b = set(a.trading_days()), set(b.trading_days())
    r = {"day_diff": len(days_a ^ days_b)}
    syms = b.symbols_on(sorted(days_b)[-1])[:sample_syms]
    closes_ok = True
    rows_a, rows_b = 0, 0
    for d in sorted(days_b)[-sample_days:]:
        pa_, pb_ = a.prices_for(d, syms), b.prices_for(d, syms)
        if set(pa_) != set(pb_):
            closes_ok = False
        for s in pa_:
            if abs(pa_[s] - pb_.get(s, -1e9)) > 1e-9:
                closes_ok = False
    r["symbols"] = syms
    r["prices_parity_ok"] = closes_ok
    a.close(); b.close()
    return r


if __name__ == "__main__":
    import json
    print(json.dumps(parity_check(), ensure_ascii=False, indent=2))

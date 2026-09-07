# -*- coding: utf-8 -*-
"""
rebuild_symbols.py  — 重灌 symbols 主数据表 (第3步治本)

背景: 现 symbols 表 6186 行, name 100% 为 NULL, 且混入:
  - 可转债 11/12 段 403 只 (非股票, 污染 source)
  - B股 20/90 段 90 只
  - 老三板 832 段 1 只
  - 92 段北交所被误标 market='sh' (应为 'bj')
  - 停牌/退市残留 227 只 (不在交易所官方 code 名单)

权威源: akshare stock_info_a_code_name (沪深北全 A, 5551 只, 含科创板/北交所, 全部带 name)
        实测可连通, 段覆盖 = 官方 SH/SZ/BJ 三源并集 + 科创板 688, 无转债/B股。
本地分片 kline_parts 5548 只全部被覆盖, 无遗漏。

重灌动作(事务内):
  1. 备份现 symbols -> symbols_bak_YYYYMMDD
  2. DELETE 现 symbols
  3. 以权威 5551 只重建: symbol PK / name / market(按段修正) / is_active=TRUE
  4. list_date、float_shares 保留 (源无则 NULL)
以下游依赖(db.py get_universe / drl_train canon / paper_book / update_db._list_active_symbols)
均只需 symbol/market/name/is_active 四列, 重建后全部可正常使用。
"""
import sys, os, datetime as _dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duckdb
import akshare as ak
import config as C

# ---- 段 -> market 权威判定 ----
SH_PREFIX = ("600", "601", "603", "605", "688", "689", "900")  # 沪主板/科创/B股
SZ_PREFIX = ("000", "001", "002", "003", "200", "300", "301", "302")  # 深A/B
BJ_PREFIX = ("4", "8", "92")  # 北交所 (92, 8xx, 4xx)


def _market_of(code: str) -> str:
    """按交易所真实归属判定 market (修复 _get_symbols 的 920->sh 误判)."""
    if code.startswith("92") or code.startswith("4") or code.startswith("8"):
        return "bj"
    if code.startswith(SH_PREFIX):
        return "sh"
    if code.startswith(SZ_PREFIX):
        return "sz"
    return "sz"  # 未知段保守归 sz (仅理论, 权威源不含未知段)


def load_authoritative():
    """拉取权威全A code+name. 失败则抛异常(重灌必须权威, 不可降级)."""
    df = ak.stock_info_a_code_name()
    rows = []
    for _, r in df.iterrows():
        code = str(r["code"]).strip().zfill(6)
        name = str(r["name"]).strip()
        if len(code) != 6 or not code.isdigit():
            continue
        rows.append((code, name, _market_of(code)))
    # 去重 + 排序
    rows = sorted(set(rows), key=lambda x: x[0])
    return rows


def main():
    if not os.path.exists(C.DUCKDB_PATH):
        # [m4] DuckDB 已退役: symbols 已由 parquet 承载, 重建工具直接退出
        print("DuckDB 已退役/不存在, rebuild_symbols 不再适用 (symbols 见 data/h5i/static/symbols.parquet)")
        return 0
    con = duckdb.connect(C.DUCKDB_PATH)
    try:
        bk = "symbols_bak_" + _dt.date.today().strftime("%Y%m%d")
        con.execute(f"CREATE TABLE IF NOT EXISTS {bk} AS SELECT * FROM symbols")
        n_bak = con.execute(f"SELECT count(*) FROM {bk}").fetchone()[0]

        rows = load_authoritative()
        n_new = len(rows)
        print(f"权威源加载: {n_new} 只, 备份 {n_bak} 只 -> {bk}")
        # 安全闸: 权威源不足 5000 只视为拉取失败, 中止重灌 (防误删)
        if n_new < 5000:
            raise RuntimeError(f"权威源仅 {n_new} 只 (<5000), 判定拉取失败, 中止重灌")
        if n_bak < 5000:
            raise RuntimeError(f"现库备份仅 {n_bak} 只 (<5000), 判定库异常, 中止重灌")

        # 事务内重建
        con.execute("BEGIN TRANSACTION")
        con.execute("DELETE FROM symbols")
        con.executemany(
            "INSERT INTO symbols (symbol, name, market) VALUES (?, ?, ?)",
            [(r[0], r[1], r[2]) for r in rows],
        )
        # is_active 依赖表默认值 TRUE (建表时 DEFAULT CAST('t' AS BOOLEAN))
        con.execute("COMMIT")

        # 校验
        n_final = con.execute("SELECT count(*) FROM symbols").fetchone()[0]
        null_name = con.execute(
            "SELECT count(*) FROM symbols WHERE name IS NULL OR name=''").fetchone()[0]
        market_dist = con.execute(
            "SELECT market, count(*) FROM symbols GROUP BY market ORDER BY count(*) DESC").fetchall()
        seg = con.execute(
            "SELECT substr(symbol,1,2) s, count(*) FROM symbols GROUP BY s ORDER BY count(*) DESC"
        ).fetchall()
        a_ok = con.execute("SELECT count(*) FROM symbols WHERE is_active").fetchone()[0]

        print(f"重灌完成: 现 {n_final} 只, name 空 {null_name}, active {a_ok}")
        print("market 分布:", market_dist)
        print("段分布:", seg)
    finally:
        con.close()


if __name__ == "__main__":
    main()
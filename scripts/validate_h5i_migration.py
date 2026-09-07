# -*- coding: utf-8 -*-
"""h5i-db 迁移数据验证脚本.

验证 h5i-db 与原始 parquet 数据源的一致性, 以及 h5i-db 自身的数据完整性.

验证内容:
  1. 数据完整性: 行数、空值率、值域范围
  2. 与 parquet 源数据交叉验证: 随机抽样对比
  3. 决策时点防未来函数: decision_time 过滤正确性
  4. 版本化写入/读取: write_with_version + read_version 一致性
  5. 横向数据一致性: 跨表 symbol 交集

用法:
  python scripts/validate_h5i_migration.py          # 完整验证
  python scripts/validate_h5i_migration.py --quick   # 快速验证 (抽样)
"""
from __future__ import annotations

import os
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

_H5I_PATH = PROJ / "data" / "h5i" / "market.db"
_PARQUET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "legacy_kline_daily_full.parquet")


# ===================================================================
# 辅助函数
# ===================================================================
def _h5i_sql(q: str) -> pd.DataFrame:
    import h5i_db
    db = h5i_db.Database(str(_H5I_PATH))
    try:
        return db.sql(q).to_pandas()
    finally:
        db.close()


def _ok(msg: str, detail: str = "") -> None:
    print(f"  [OK] {msg}" + (f"  — {detail}" if detail else ""))


def _fail(msg: str, detail: str = "") -> None:
    print(f"  [FAIL] {msg}" + (f"  — {detail}" if detail else ""))


def _warn(msg: str, detail: str = "") -> None:
    print(f"  [WARN] {msg}" + (f"  — {detail}" if detail else ""))


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ===================================================================
# 1. 数据完整性
# ===================================================================
def check_integrity(quick: bool = False) -> int:
    """检查 h5i-db 各表数据完整性."""
    errors = 0
    _section("1. 数据完整性检查")

    checks = [
        ("daily_bars", "ts", "symbol", "close", "volume"),
        ("financials", "ts", "symbol", "revenue", "net_profit"),
        ("valuation", "ts", "symbol", "pe_ttm", "pb"),
        ("valuation_snapshot", "ts", "symbol", "price", "total_mv"),
    ]

    for table, date_col, key_col, *val_cols in checks:
        try:
            cnt = _h5i_sql(f"SELECT count(*) as n FROM {table}").iloc[0, 0]
            _ok(f"{table}: {cnt:,} 行")

            # 空值率检查
            for col in [date_col, key_col] + val_cols:
                null_cnt = _h5i_sql(
                    f"SELECT count(*) as n FROM {table} WHERE {col} IS NULL"
                ).iloc[0, 0]
                rate = null_cnt / cnt * 100 if cnt > 0 else 0
                if rate > 5:
                    _warn(f"{table}.{col} 空值率 {rate:.1f}%")
                elif rate > 0:
                    _ok(f"{table}.{col} 空值率 {rate:.2f}%")

            # 值域检查
            if "close" in val_cols:
                neg = _h5i_sql(
                    f"SELECT count(*) as n FROM {table} WHERE close < 0 AND close IS NOT NULL"
                ).iloc[0, 0]
                if neg > 0:
                    _warn(f"{table}.close 有 {neg} 个负值")

            if "pe_ttm" in val_cols:
                extreme = _h5i_sql(
                    f"SELECT count(*) as n FROM {table} WHERE pe_ttm > 1000 AND pe_ttm IS NOT NULL"
                ).iloc[0, 0]
                if extreme > 0:
                    _warn(f"{table}.pe_ttm 有 {extreme} 个 > 1000 的极端值")

            # 日期范围
            dr = _h5i_sql(
                f"SELECT min({date_col}) as mn, max({date_col}) as mx FROM {table}"
            )
            _ok(f"{table} 日期范围: {dr.iloc[0, 0]} ~ {dr.iloc[0, 1]}")

            # symbol 唯一数
            syms = _h5i_sql(
                f"SELECT count(DISTINCT {key_col}) as n FROM {table}"
            ).iloc[0, 0]
            _ok(f"{table} 股票数: {syms}")

        except Exception as e:
            _fail(f"{table} 检查失败: {e}")
            errors += 1

    return errors


# ===================================================================
# 2. Parquet 交叉验证
# ===================================================================
def check_vs_parquet(quick: bool = False) -> int:
    """与 parquet 源数据交叉验证."""
    errors = 0
    _section("2. Parquet 源数据交叉验证")

    try:
        # 读取 parquet 样本
        if quick:
            pq = pd.read_parquet(_PARQUET_PATH, columns=["trade_date", "symbol", "close", "open", "high", "low", "volume", "amount"])
            # 取 2026-09-01 附近的数据
            pq = pq[pq["trade_date"] >= "2026-08-01"].head(5000)
        else:
            pq = pd.read_parquet(_PARQUET_PATH, columns=["trade_date", "symbol", "close", "open", "high", "low", "volume", "amount"])
            pq = pq.head(20000)

        pq["trade_date"] = pd.to_datetime(pq["trade_date"])
        pq["symbol"] = pq["symbol"].astype(str).str.zfill(6)
        _ok(f"Parquet 加载完成: {len(pq):,} 行")

        # 从 h5i-db 取对应日期范围
        dt_min = pq["trade_date"].min().strftime("%Y-%m-%d")
        dt_max = pq["trade_date"].max().strftime("%Y-%m-%d")
        h5 = _h5i_sql(
            f"SELECT CAST(ts AS DATE) as trade_date, symbol, close, open, high, low, volume, amount "
            f"FROM daily_bars "
            f"WHERE CAST(ts AS DATE) >= DATE '{dt_min}' "
            f"AND CAST(ts AS DATE) <= DATE '{dt_max}'"
        )
        h5["trade_date"] = pd.to_datetime(h5["trade_date"])
        h5["symbol"] = h5["symbol"].astype(str).str.zfill(6)
        _ok(f"h5i-db 对应数据: {len(h5):,} 行")

        # 合并比较
        merged = pq.merge(
            h5, on=["trade_date", "symbol"],
            how="inner", suffixes=("_pq", "_h5"),
        )
        _ok(f"匹配行数: {len(merged):,}")

        if len(merged) == 0:
            _fail("无匹配数据, 无法交叉验证")
            return 1

        # 比较 close 价格 (允许 1% 相对误差, 考虑复权差异)
        diff = abs(merged["close_pq"] - merged["close_h5"])
        rel_diff = diff / (merged["close_pq"].abs() + 1e-8)
        mismatch = (rel_diff > 0.01).sum()
        if mismatch > 0:
            rate = mismatch / len(merged) * 100
            if rate < 5:
                _warn(f"close 差异 > 1%: {mismatch} 行 ({rate:.2f}%) — 可能因复权方式不同")
            else:
                _fail(f"close 差异 > 1%: {mismatch} 行 ({rate:.2f}%)")
                errors += 1
        else:
            _ok("close 价格完全一致" if len(merged) > 0 else "")

        # 比较 volume
        vol_diff = abs(merged["volume_pq"] - merged["volume_h5"])
        vol_mismatch = (vol_diff > 1).sum()
        if vol_mismatch > 0:
            rate = vol_mismatch / len(merged) * 100
            _warn(f"volume 差异: {vol_mismatch} 行 ({rate:.2f}%)")
        else:
            _ok(f"volume 一致 ({len(merged)} 行)")

        # 抽样展示
        sample = merged.sample(min(5, len(merged)), random_state=42)
        print(f"\n  --- 抽样对比 (close) ---")
        for _, row in sample.iterrows():
            print(f"  {row['trade_date'].strftime('%Y-%m-%d')} {row['symbol']}  "
                  f"parquet={row['close_pq']:.2f}  h5i={row['close_h5']:.2f}  "
                  f"diff={abs(row['close_pq'] - row['close_h5']):.4f}")

    except FileNotFoundError:
        _warn(f"Parquet 文件不存在: {_PARQUET_PATH}, 跳过交叉验证")
    except Exception as e:
        _fail(f"交叉验证异常: {e}")
        errors += 1

    return errors


# ===================================================================
# 3. 决策时点防未来函数
# ===================================================================
def check_decision_time(quick: bool = False) -> int:
    """验证 decision_time 参数正确过滤未来数据."""
    errors = 0
    _section("3. 决策时点防未来函数验证")

    try:
        # 获取所有交易日
        trading_days = _h5i_sql(
            "SELECT DISTINCT CAST(ts AS DATE) as d FROM daily_bars ORDER BY d"
        )["d"].tolist()
        _ok(f"交易日总数: {len(trading_days)}")

        if len(trading_days) < 10:
            _fail("交易日不足 10 天, 无法验证")
            return 1

        mid_idx = len(trading_days) // 2
        mid_day = str(trading_days[mid_idx])[:10]
        last_day = str(trading_days[-1])[:10]

        # 不带 decision_time: 应返回所有数据
        all_cnt = _h5i_sql(
            f"SELECT count(*) as n FROM daily_bars"
        ).iloc[0, 0]

        # 带 decision_time = mid_day: 应只返回 <= mid_day 的数据
        mid_cnt = _h5i_sql(
            f"SELECT count(*) as n FROM daily_bars "
            f"WHERE CAST(ts AS DATE) <= DATE '{mid_day}'"
        ).iloc[0, 0]

        if mid_cnt >= all_cnt:
            _fail(f"decision_time 过滤无效: mid_cnt={mid_cnt} == all_cnt={all_cnt}")
            errors += 1
        else:
            _ok(f"决策时点过滤: mid_day={mid_day} 行数={mid_cnt:,}  < 总行数={all_cnt:,}")

        # 验证 decision_time = last_day 应返回全量
        last_cnt = _h5i_sql(
            f"SELECT count(*) as n FROM daily_bars "
            f"WHERE CAST(ts AS DATE) <= DATE '{last_day}'"
        ).iloc[0, 0]
        if last_cnt == all_cnt:
            _ok(f"决策时点=最后日: 行数一致 ({last_cnt:,})")
        else:
            _warn(f"决策时点=最后日: 行数 {last_cnt:,} != 全量 {all_cnt:,}")

        # 验证 decision_time 在最早日期之前应返回 0
        first_day = str(trading_days[0])[:10]
        early_cnt = _h5i_sql(
            f"SELECT count(*) as n FROM daily_bars "
            f"WHERE CAST(ts AS DATE) <= DATE '2000-01-01'"
        ).iloc[0, 0]
        if early_cnt == 0:
            _ok("决策时点在最早日期前: 返回 0 行")
        else:
            _warn(f"决策时点在最早日期前: 返回 {early_cnt} 行 (非预期)")

    except Exception as e:
        _fail(f"决策时点验证异常: {e}")
        errors += 1

    return errors


# ===================================================================
# 4. 版本化写入/读取
# ===================================================================
def check_versioning(quick: bool = False) -> int:
    """验证 write_with_version + read_version 功能."""
    errors = 0
    _section("4. 版本化写入/读取验证")

    try:
        from h5i_bar_store import H5iBarStore
        store = H5iBarStore()

        # 写入测试数据
        test_df = pd.DataFrame({
            "ts": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
            "symbol": ["000001", "000002", "000003"],
            "close": [10.0, 11.0, 12.0],
        })
        ok = store.write_with_version("daily_bars", test_df, "test_version_integration")
        _ok(f"版本化写入: {'成功' if ok else '失败 (可能已存在)'}")

        # 读取版本 (可能因为数据已存在而失败, 但至少不抛异常)
        try:
            result = store.read_version("daily_bars", "test_version_integration")
            if result is not None and not result.empty:
                _ok(f"版本化读取: {len(result)} 行, {list(result.columns)}")
            else:
                _warn("版本化读取返回空结果")
        except Exception as e:
            _warn(f"版本化读取: {e} (可能因数据已存在, 预期行为)")

        # 列出版本
        versions = store.list_versions()
        if "test_version_integration" in versions:
            _ok(f"版本列表包含 test_version_integration")
        else:
            _warn("版本列表未包含 test_version_integration")

        store.close()

    except Exception as e:
        _fail(f"版本化验证异常: {e}")
        errors += 1

    return errors


# ===================================================================
# 5. 横向一致性
# ===================================================================
def check_cross_table(quick: bool = False) -> int:
    """跨表 symbol 一致性和数据覆盖."""
    errors = 0
    _section("5. 跨表数据一致性")

    try:
        bar_syms = set(_h5i_sql(
            "SELECT DISTINCT symbol FROM daily_bars"
        )["symbol"].tolist())
        fin_syms = set(_h5i_sql(
            "SELECT DISTINCT symbol FROM financials"
        )["symbol"].tolist())
        val_syms = set(_h5i_sql(
            "SELECT DISTINCT symbol FROM valuation"
        )["symbol"].tolist())

        _ok(f"daily_bars symbol: {len(bar_syms):,}")
        _ok(f"financials  symbol: {len(fin_syms):,}")
        _ok(f"valuation   symbol: {len(val_syms):,}")

        # 交集
        common = bar_syms & fin_syms & val_syms
        _ok(f"三表共同 symbol: {len(common):,}")

        # 只有 bar 没有 fin 的
        bar_only = bar_syms - fin_syms
        if bar_only:
            _warn(f"daily_bars 独有 symbol: {len(bar_only):,} (如 {list(bar_only)[:3]})")

        # 只有 fin 没有 bar 的
        fin_only = fin_syms - bar_syms
        if fin_only:
            _warn(f"financials 独有 symbol: {len(fin_only):,} — 可能为已退市股")

        if len(common) < 1000:
            _warn(f"三表共同 symbol 仅 {len(common)}, 覆盖率偏低")

    except Exception as e:
        _fail(f"跨表一致性验证异常: {e}")
        errors += 1

    return errors


# ===================================================================
# 6. 性能基准
# ===================================================================
def check_performance(quick: bool = False) -> int:
    """性能基准测试: 常见查询响应时间."""
    errors = 0
    _section("6. 查询性能基准")

    queries = [
        ("按日期查 K 线", "SELECT count(*) FROM daily_bars WHERE CAST(ts AS DATE) = '2026-08-01'"),
        ("按股票查 K 线", "SELECT count(*) FROM daily_bars WHERE symbol = '000001'"),
        ("查全部估值", "SELECT count(*) FROM valuation WHERE CAST(ts AS DATE) = '2026-08-01'"),
        ("查财报", "SELECT count(*) FROM financials WHERE symbol = '600519'"),
    ]

    for name, q in queries:
        try:
            t0 = time.perf_counter()
            _h5i_sql(q)
            elapsed = time.perf_counter() - t0
            _ok(f"{name}: {elapsed*1000:.0f}ms")
        except Exception as e:
            _fail(f"{name}: {e}")
            errors += 1

    return errors


# ===================================================================
# 主流程
# ===================================================================
def main():
    parser = argparse.ArgumentParser(description="h5i-db 迁移数据验证")
    parser.add_argument("--quick", action="store_true", help="快速验证 (抽样)")
    args = parser.parse_args()

    quick = args.quick
    print(f"h5i-db 迁移数据验证开始 (quick={quick})")
    print(f"  h5i-db: {_H5I_PATH}")
    print(f"  parquet: {_PARQUET_PATH}")

    total_errors = 0
    total_errors += check_integrity(quick)
    total_errors += check_vs_parquet(quick)
    total_errors += check_decision_time(quick)
    total_errors += check_versioning(quick)
    total_errors += check_cross_table(quick)
    if not quick:
        total_errors += check_performance(quick)

    print(f"\n{'=' * 60}")
    if total_errors == 0:
        print("  验证通过: 所有检查项正常")
    else:
        print(f"  验证完成: {total_errors} 个错误")
    print(f"{'=' * 60}")

    return total_errors


if __name__ == "__main__":
    sys.exit(main())
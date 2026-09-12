# -*- coding: utf-8 -*-
"""PIT 估值链路回归测试 (2026-09-13).

覆盖本轮修复的 5 条:
  1. PE 补丁旧格式兼容(parquet 缺 free_cap 列时不得整体失效)
  2. 百度备用源回退(东财接口异常时)
  3. 主源缺日期列(no_date_col)同样走备用源
  4. free_cap PIT 合并(补丁携带 free_cap)
  5. is_st 的 True / False / NaN 语义(未知不得当作"明确非 ST")

全部用例不依赖网络与真实数据: akshare 以假模块注入, 补丁写入 tmp_path.
"""
from __future__ import annotations

import os
import sys
import types

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _fake_akshare(em_func, baidu_func):
    m = types.ModuleType("akshare")
    m.stock_value_em = em_func
    m.stock_zh_valuation_baidu = baidu_func
    return m


# --------------------------------------------------------------------------
# 1 & 4: 补丁读取 (旧格式兼容 / free_cap 合并)
# --------------------------------------------------------------------------
def _write_patch(dst_dir, df: pd.DataFrame, name="patch.parquet"):
    os.makedirs(dst_dir, exist_ok=True)
    df.to_parquet(os.path.join(dst_dir, name), index=False)


def test_patch_old_format_without_free_cap(tmp_path, monkeypatch):
    """旧格式补丁(仅 ts/symbol/pe_ttm)仍应读出 pe_ttm, 且 free_cap 补 NaN."""
    import config
    import db as dbmod

    _write_patch(os.path.join(str(tmp_path), "pit", "pe_patch"), pd.DataFrame({
        "ts": pd.to_datetime(["2026-08-14"]),
        "symbol": ["600519"],
        "pe_ttm": [19.5],
    }))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    dbmod._PE_PATCH_CACHE.clear()

    out = dbmod._pe_patch_asof("2026-08-14")
    assert len(out) == 1, "旧格式补丁被整体跳过 → pe_ttm 失效"
    assert abs(float(out.iloc[0]["pe_ttm"]) - 19.5) < 1e-6
    assert "free_cap" in out.columns
    assert pd.isna(out.iloc[0]["free_cap"])


def test_patch_free_cap_merged(tmp_path, monkeypatch):
    """新格式补丁应携带 free_cap(用于 PIT 流通市值合并)。"""
    import config
    import db as dbmod

    _write_patch(os.path.join(str(tmp_path), "pit", "pe_patch"), pd.DataFrame({
        "ts": pd.to_datetime(["2026-08-14", "2026-08-15"]),
        "symbol": ["600519", "600519"],
        "pe_ttm": [19.5, 20.1],
        "free_cap": [1.6e12, 1.65e12],
    }))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    dbmod._PE_PATCH_CACHE.clear()

    out = dbmod._pe_patch_asof("2026-08-15")
    row = out[out["symbol"] == "600519"].iloc[0]
    assert abs(float(row["pe_ttm"]) - 20.1) < 1e-6     # as-of 取最新
    assert float(row["free_cap"]) == 1.65e12


def test_patch_asof_no_lookahead(tmp_path, monkeypatch):
    """as-of 不得取到未来日期的补丁行(无前视)。"""
    import config
    import db as dbmod

    _write_patch(os.path.join(str(tmp_path), "pit", "pe_patch"), pd.DataFrame({
        "ts": pd.to_datetime(["2026-08-14", "2026-09-01"]),
        "symbol": ["600519", "600519"],
        "pe_ttm": [19.5, 30.0],
        "free_cap": [1.6e12, 1.7e12],
    }))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    dbmod._PE_PATCH_CACHE.clear()

    out = dbmod._pe_patch_asof("2026-08-14")
    assert abs(float(out.iloc[0]["pe_ttm"]) - 19.5) < 1e-6, "as-of 取到了未来数据"


# --------------------------------------------------------------------------
# 2 & 3: 数据源回退
# --------------------------------------------------------------------------
def test_baidu_fallback_when_em_raises(monkeypatch):
    """东财接口异常(TypeError)时应回退百度源, 并标注 source=baidu_hist。"""
    import backfill_pe_ttm as bf

    def em(**kw):
        raise TypeError("'NoneType' object is not subscriptable")

    def baidu(**kw):
        return pd.DataFrame({"date": ["2026-08-14"], "value": [19.5]})

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(em, baidu))
    df, err = bf.fetch_one("600519", {"2026-08-14"}, retry=1)
    assert err is None and df is not None and len(df) == 1
    assert df.iloc[0]["source"] == "baidu_hist"
    assert abs(float(df.iloc[0]["pe_ttm"]) - 19.5) < 1e-6
    assert pd.isna(df.iloc[0]["is_st"]), "备用源未提供 ST, 应为 NaN(未知)"


def test_no_date_col_falls_back_to_baidu(monkeypatch):
    """主源返回结果缺少日期列时, 也应进入百度回退(不再直接 no_date_col)。"""
    import backfill_pe_ttm as bf

    def em(**kw):
        return pd.DataFrame({"无关列": [1, 2]})      # 缺 数据日期

    def baidu(**kw):
        return pd.DataFrame({"date": ["2026-08-14"], "value": [7.7]})

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(em, baidu))
    df, err = bf.fetch_one("000627", {"2026-08-14"}, retry=1)
    assert err is None and df is not None and len(df) == 1
    assert df.iloc[0]["source"] == "baidu_hist"
    assert abs(float(df.iloc[0]["pe_ttm"]) - 7.7) < 1e-6


def test_both_sources_fail_reports_both(monkeypatch):
    """主源与备用源均失败时, 错误信息应同时包含两者(便于诊断)。"""
    import backfill_pe_ttm as bf

    def em(**kw):
        raise ValueError("em boom")

    def baidu(**kw):
        raise RuntimeError("baidu boom")

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(em, baidu))
    df, err = bf.fetch_one("600519", {"2026-08-14"}, retry=1)
    assert df is None
    assert "em:" in err and "baidu:" in err


def test_em_success_is_st_is_nan(monkeypatch):
    """东财源不提供 ST 状态 → 写 NaN(未知), 不得写成 False。"""
    import backfill_pe_ttm as bf

    def em(**kw):
        return pd.DataFrame({
            "数据日期": ["2026-08-14"], "PE(TTM)": [19.5], "市净率": [6.2],
            "市销率": [9.0], "总市值": [1.6e12], "流通市值": [1.6e12],
            "流通股本": [1.25e9],
        })

    def baidu(**kw):  # pragma: no cover - 不应被调用
        raise AssertionError("EM 成功时不应调用百度源")

    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(em, baidu))
    df, err = bf.fetch_one("600519", {"2026-08-14"}, retry=1)
    assert err is None and len(df) == 1
    assert df.iloc[0]["source"] == "em_hist"
    assert pd.isna(df.iloc[0]["is_st"])


# --------------------------------------------------------------------------
# 5: is_st 三态语义 (filter_universe)
# --------------------------------------------------------------------------
def _universe(**over):
    base = {
        "symbol": ["600519", "600520", "600521"],
        "name6": ["贵州茅台", "普通样本", "普通样本2"],
        "price": [1500.0, 20.0, 18.0],
        "amount": [1e8, 1e7, 1e7],
        "float_mv": [20000.0, 8000.0, 9000.0],
        "is_st": [False, True, float("nan")],
    }
    base.update(over)
    return pd.DataFrame(base)


def test_is_st_semantics(monkeypatch):
    """is_st: True 剔除; False 保留; NaN(未知) 保留。"""
    import db as dbmod

    monkeypatch.setattr(dbmod, "POOL_FILTER", {
        "exclude_new": False, "exclude_920_bse": False, "exclude_st": True,
        "min_float_market_cap": 1e8, "max_float_market_cap": 1e13,
        "min_avg_turnover": 0, "exclude_new_days": 60,
    }, raising=False)

    out = dbmod.filter_universe(_universe(), as_of="2026-08-14")
    kept = set(out["symbol"].tolist())
    assert "600520" not in kept, "is_st=True 未被剔除"
    assert "600519" in kept, "is_st=False 被误剔"
    assert "600521" in kept, "is_st=NaN(未知) 被误剔 → 把未知当成了 ST"


def test_is_st_all_nan_keeps_universe(monkeypatch):
    """is_st 全为 NaN(未覆盖)时不应清空候选池。"""
    import db as dbmod

    monkeypatch.setattr(dbmod, "POOL_FILTER", {
        "exclude_new": False, "exclude_920_bse": False, "exclude_st": True,
        "min_float_market_cap": 1e8, "max_float_market_cap": 1e13,
        "min_avg_turnover": 0, "exclude_new_days": 60,
    }, raising=False)

    df = _universe(is_st=[float("nan")] * 3)
    out = dbmod.filter_universe(df, as_of="2026-08-14")
    assert len(out) == 3, "is_st 全 NaN 时候选池被清空"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))

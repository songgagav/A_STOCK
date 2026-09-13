# -*- coding: utf-8 -*-
"""item 9 回归: 标的代码形态 (canon 带后缀 / 纯6位) 全链路约定.

根因: `canon` 同名指代两种形态, 跨表 join 会静默返回空(曾致 IC 仅对齐 60/1563)。
约定见 docs/symbols.md。
"""
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

import pytest  # noqa: E402

from db import _canon_to_db, _db_to_canon, to_sym6  # noqa: E402


def test_canon_to_db_strips_suffix():
    assert _canon_to_db("600519.SH") == "600519"
    assert _canon_to_db("000001.SZ") == "000001"
    assert _canon_to_db("600519") == "600519"


def test_to_sym6_handles_all_forms():
    assert to_sym6("600519.SH") == "600519"
    assert to_sym6("000001.SZ") == "000001"
    assert to_sym6("600519") == "600519"
    assert to_sym6("1") == "000001"        # 补零
    assert to_sym6(1) == "000001"          # 非 str 输入
    assert to_sym6("") == ""
    assert to_sym6(None) == ""


def test_db_to_canon_market_mapping():
    assert _db_to_canon("600519", "sh") == "600519.SH"
    assert _db_to_canon("600519") == "600519.SZ"      # 默认深市
    assert _db_to_canon("430047", "bj") == "430047.BSE"
    assert _db_to_canon("000001", "sz") == "000001.SZ"
    # 已带后缀则原样返回
    assert _db_to_canon("600519.SH", "sz") == "600519.SH"


def test_round_trip_sym6():
    for s6, mkt in (("600519", "sh"), ("000001", "sz"), ("430047", "bj")):
        assert to_sym6(_db_to_canon(s6, mkt)) == s6


def test_join_requires_normalization():
    """复现 bug 形态: 带后缀 canon 与纯 6 位 symbol 直接 join -> 空; 归一后 -> 命中."""
    pd = pytest.importorskip("pandas")
    left = pd.DataFrame({"canon": ["600519.SH", "000001.SZ"]})
    right = pd.DataFrame({"symbol": ["600519", "000001"], "r": [0.1, 0.2]})
    bad = left.merge(right, left_on="canon", right_on="symbol", how="inner")
    assert bad.empty, "该用例用于固定'直接 join 会静默为空'这一事实"
    left["sym6"] = left["canon"].map(to_sym6)
    good = left.merge(right, left_on="sym6", right_on="symbol", how="inner")
    assert len(good) == 2


def test_views_canon_is_bare_six_digit():
    """build_factor_views 写入的 h5i views, canon 必须是纯 6 位(否则 join 全空)."""
    pd = pytest.importorskip("pandas")
    p = os.path.join(_BASE, "data", "h5i", "views", "v_factor_scores_daily.parquet")
    if not os.path.exists(p):
        pytest.skip("views 产物不存在")
    t = pd.read_parquet(p, columns=["canon"])
    suffixed = float(t["canon"].astype(str).str.contains(r"\.").mean())
    assert suffixed == 0.0, f"views.canon 出现带后缀值 (占比 {suffixed:.1%})"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])

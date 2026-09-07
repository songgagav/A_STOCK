# -*- coding: utf-8 -*-
"""快速验证: Almgren-Chriss 滑点模型 + PaperBook 向后兼容."""
from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from slippage_model import almglen_chriss_slippage, estimate_daily_volume, estimate_volatility
from paper_book import PaperBook


def test_ac_slippage_small_order():
    """小单高流动性 → 滑点接近最低值万1."""
    s = almglen_chriss_slippage(10000, 5e8, 0.02)
    assert abs(s - 0.0001) < 1e-6, f"小单高流动滑点应为万1, 实际 {s:.6f}"
    print("1. 小单高流动滑点: OK")


def test_ac_slippage_large_order():
    """大单高参与率 → 滑点显著上升."""
    s = almglen_chriss_slippage(500000, 5e6, 0.02)
    assert s > 0.005, f"大单低流动滑点应 > 0.5%, 实际 {s:.4%}"
    print("2. 大单低流动滑点: OK")


def test_ac_slippage_high_vol():
    """高波动行情 → 滑点高于低波动."""
    low_vol = almglen_chriss_slippage(100000, 1e7, 0.01)
    high_vol = almglen_chriss_slippage(100000, 1e7, 0.05)
    assert high_vol > low_vol, "高波动滑点应高于低波动"
    print("3. 高波动 vs 低波动: OK")


def test_ac_slippage_max_clip():
    """满仓参与极端情况 → 滑点封顶2%."""
    s = almglen_chriss_slippage(1e7, 1e7, 0.02)
    assert abs(s - 0.02) < 1e-6, f"满仓参与滑点应为 2%, 实际 {s:.4%}"
    print("4. 极端满仓滑点封顶: OK")


def test_ac_slippage_zero_volume():
    """零成交额 → 回退最低滑点."""
    s = almglen_chriss_slippage(10000, 0, 0.02)
    assert s == 0.0001, f"零成交额应回退万1, 实际 {s}"
    print("5. 零成交额回退: OK")


def test_estimate_daily_volume():
    """日均成交额估算."""
    v = estimate_daily_volume([1e7, 2e7, 3e7])
    assert abs(v - 2e7) < 1, f"日均成交额应为 2e7, 实际 {v}"
    # 空序列回退
    v2 = estimate_daily_volume(None)
    assert v2 == 5e7, f"空序列应回退 5e7, 实际 {v2}"
    print("6. 日均成交额估算: OK")


def test_estimate_volatility():
    """波动率估算."""
    prices = [10.0, 10.2, 10.1, 10.3, 10.0, 10.4]
    vol = estimate_volatility(prices)
    assert 0.01 < vol < 0.05, f"波动率应在合理范围, 实际 {vol:.6f}"
    # 不足 5 个点回退
    v2 = estimate_volatility([10.0, 10.1])
    assert v2 == 0.025, f"不足 5 个点应回退 0.025, 实际 {v2}"
    print("7. 波动率估算: OK")


def test_paperbook_backward_compat():
    """PaperBook buy/sell 不传新参数时回退固定费率."""
    pb = PaperBook(100000)
    r = pb.buy("000001.SZ", 100, 10.0)
    assert r is not None, "buy 应成功 (无 avg_daily_volume)"
    # 不走动态滑点, 成交价 = 10 * (1 + 0.0005 + 0.0002) = 10.007
    expected_price = 10.0 * (1 + 0.0005 + 0.0002)
    assert abs(r["price"] - expected_price) < 0.001, \
        f"买入价应 {expected_price:.4f}, 实际 {r['price']:.4f}"
    print("8. PaperBook 向后兼容(旧调用): OK")


def test_paperbook_ac_model():
    """PaperBook buy/sell 传入新参数时走 AC 动态滑点."""
    pb = PaperBook(100000)
    # 小单高流动 -> AC 滑点 = 万1
    r = pb.buy("000001.SZ", 100, 10.0, avg_daily_volume=5e8, volatility=0.02)
    assert r is not None, "buy 应成功 (有 avg_daily_volume)"
    # 动态滑点成交价 = 10 * (1 + 0.0001) = 10.001
    expected_price = 10.0 * (1 + 0.0001)
    assert abs(r["price"] - expected_price) < 0.001, \
        f"AC 买入价应 {expected_price:.4f}, 实际 {r['price']:.4f}"
    # 模拟次日卖出: 把 trade_date 改为昨日, 解锁 T+1 限制
    pb.trade_date = "2026-09-05"
    r2 = pb.sell("000001.SZ", 100, 10.5, avg_daily_volume=5e8, volatility=0.02)
    assert r2 is not None, "sell 应成功"
    expected_sell = 10.5 * (1 - 0.0001)
    assert abs(r2["price"] - expected_sell) < 0.001, \
        f"AC 卖出价应 {expected_sell:.4f}, 实际 {r2['price']:.4f}"
    print("9. PaperBook AC 动态滑点: OK")


if __name__ == "__main__":
    test_ac_slippage_small_order()
    test_ac_slippage_large_order()
    test_ac_slippage_high_vol()
    test_ac_slippage_max_clip()
    test_ac_slippage_zero_volume()
    test_estimate_daily_volume()
    test_estimate_volatility()
    test_paperbook_backward_compat()
    test_paperbook_ac_model()
    print("\n所有 9 个滑点模型测试通过!")
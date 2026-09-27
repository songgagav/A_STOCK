# -*- coding: utf-8 -*-
"""守卫: 「回测持有期 ≠ 生产持有期」必须写进回测解读协议, 且标注要求是**强制**的。

## 为什么单独锁这个 (2026-09-27)

实测发现两条口径差**约 4~5 倍**:

| 维度 | 回测 | 生产 |
|---|---|---|
| 成交时点 | 当日**收盘价** | **开盘瞬间**(09:30:06~09:30:21) |
| 隐含持有期 | 约 4.1 / 4.9 天 | **中位 1 天** |

**危害**: 引用回测的均值/中位/正窗口/Sharpe 时, 若不带这条标注, 读者会把
"回测节奏下的结论"当成"实盘节奏下的结论" —— 而两者**不是同一件事**。
这与本仓「回测池与虚拟盘池分叉」同族(名字相同、跑的不是同一件事)。

故锁三件事: ① 协议里有该小节; ② 必报项里有一条**强制标注**; ③ 关键数字在位
(1 天 / 4.9 天 / 09:30 / 收盘 / 不显著)。
"""
from __future__ import annotations

import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOC = os.path.join(_REPO, "docs", "hist-window-protocol.md")


class TestBacktestHoldingPeriodMustBeDisclosed:
    def _src(self):
        if not os.path.exists(_DOC):
            pytest.skip("无 docs/hist-window-protocol.md")
        return open(_DOC, encoding="utf-8").read()

    def test_section_exists(self):
        src = self._src()
        assert "回测口径 ≠ 生产口径" in src, (
            "缺「回测口径 ≠ 生产口径」小节 —— 回测解读协议必须写明这条差异")

    def test_is_a_mandatory_disclosure_item(self):
        """必须是**必报项**里的一条, 而不是正文里的一段说明。"""
        src = self._src()
        i = src.find("## 3. 必报项")
        j = src.find("### 3b")
        assert i > 0 and j > i, "找不到必报项小节或 3b 小节"
        block = src[i:j]
        assert "回测持有期 ≠ 生产持有期" in block, (
            "必报项里没有这一条 —— 那它就不会被强制标注")

    def test_holds_the_key_measured_numbers(self):
        """锁**实测数字**, 防止被改成模糊表述。"""
        src = self._src()
        i = src.find("### 3b.")
        assert i > 0
        blk = src[i:i + 4200]
        for kw in ("09:30", "收盘", "中位 1 天", "4.9 天", "484.10%"):
            assert kw in blk, f"§3b 缺关键实测值: {kw}"

    def test_states_the_timing_gap_is_not_significant(self):
        """**关键**: 必须写明"方向明确但幅度**不显著**"。

        若只写"+0.5652pp/往返"而不写 t=+1.16, 读者会把它当成已确立的偏差量 ——
        而该均值被 3 个离群点(18/11/6pp)拉动。**这正是"证据无判别力"要防的**。
        """
        src = self._src()
        i = src.find("### 3b.")
        blk = src[i:i + 4200]
        assert "t=+1.16" in blk or "t = +1.16" in blk, "缺配对 t 值"
        assert "不显著" in blk, "必须写明该差异**不显著**"
        assert "+0.5652pp" in blk and "+0.3277pp" in blk, (
            "均值与中位都要给 —— 只给均值会掩盖离群点主导")

    def test_explains_why_fixing_it_needs_data_work(self):
        """必须说明"改成开盘成交"要先补数据管道(不是改一行代码)。"""
        src = self._src()
        i = src.find("### 3b.")
        blk = src[i:i + 4200]
        assert "h5i 不存 OHLC" in blk or "没有 `open`" in blk, (
            "必须点明 h5i 没存 OHLC 这一硬约束")
        assert "fetch_day" in blk, "必须指出厂商引擎**有** OHLC(源可得, 只是没落库)"

    def test_documents_the_engine_day_boundary(self):
        """必须写明时点对比只在引擎覆盖的日期上可做。"""
        src = self._src()
        i = src.find("### 3b.")
        blk = src[i:i + 4200]
        assert "2026-09-22" in blk, "必须给出引擎可用末日(结论的边界)"

    def test_the_real_measurement_tool_exists(self):
        """反向验证: 文档点名的度量工具必须真的存在。"""
        fp = os.path.join(_REPO, "_tools", "timing_gap.py")
        assert os.path.exists(fp), "文档引用了 _tools/timing_gap.py, 但它不存在"
        src = open(fp, encoding="utf-8").read()
        assert "fetch_day" in src, "工具应通过厂商引擎取 OHLC"
        assert "stdev" in src, "工具应做配对统计(否则无法判断显著性)"

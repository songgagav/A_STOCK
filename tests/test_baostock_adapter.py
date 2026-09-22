# -*- coding: utf-8 -*-
"""Baostock 适配器三契约 + 交叉校验判据的守卫 (2026-09-22)。

## 锁的三件事(用户指定)

**契约一 限流**: 默认 200ms、限流后退避 1.5s、恢复后回 200ms。
**契约二 标的分类**: 用 h5i `symbols.parquet`, **不调 `query_stock_basic`**;
  北交所**必须显式报为不可得**。
**契约三 缺口分类**: `status ∈ {appended, partial, unfillable_gap, failed}`,
  且逐只接口下 **`partial` 是常态** —— 当成功会吞缺口, 当失败会弃整天。

## 为什么这些必须用可注入 fetcher 测

Baostock 是**逐 symbol** 接口, 全市场 5481 次请求。真实跑一次要十几分钟且有网络
不确定性 —— 而这三个契约恰恰是"平时不出错、出错就静默"的地方。
故全部用注入的假 fetcher 测透, 真实链路只做少量抽样(见 `_real_` 用例)。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import baostock_adapter as BA  # noqa: E402
import cross_validate as CV  # noqa: E402


class _FakeClock:
    """可注入的假时钟: 让限流测试**不真的等 1.5 秒**。"""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def monotonic(self):
        return self.now


class TestContract1RateLimit:
    """契约一: 默认 200ms; 限流自动退避到 1.5s; 恢复后回落。"""

    def test_defaults_match_the_spec(self):
        assert BA.DEFAULT_INTERVAL_MS == 200
        assert BA.BACKOFF_INTERVAL_MS == 1500

    def test_backoff_on_repeated_throttle(self):
        clk = _FakeClock()
        lim = BA.RateLimiter(sleep=clk.sleep)
        assert lim.interval_ms == 200
        # 单次限流**不**退避(与数据源门禁同思路: 偶发抖动不改策略)
        lim.note(False, throttled=True)
        assert lim.interval_ms == 200, "单次抖动就退避 —— 会被偶发失败牵着走"
        # 连续第 2 次 -> 退避
        lim.note(False, throttled=True)
        assert lim.interval_ms == 1500
        assert lim.stats["backoffs"] == 1

    def test_empty_data_is_not_treated_as_throttle(self):
        """**空数据不是限流** —— 停牌/退市/无成交都是正常空。

        误判成限流会白白退避, 把整批从 13.7 分钟拖到 2.28 小时。
        """
        clk = _FakeClock()
        lim = BA.RateLimiter(sleep=clk.sleep)
        for _ in range(5):
            lim.note(False, throttled=False)
        assert lim.interval_ms == 200, "空数据触发了退避"
        assert lim.stats["throttle_signals"] == 0

    def test_recovers_after_enough_oks(self):
        clk = _FakeClock()
        lim = BA.RateLimiter(sleep=clk.sleep)
        lim.note(False, throttled=True)
        lim.note(False, throttled=True)
        assert lim.interval_ms == 1500
        for _ in range(BA.RECOVER_AFTER_OKS):
            lim.note(True)
        assert lim.interval_ms == 200, "长时间正常后没有回落 —— 会永久慢下去"

    def test_wait_enforces_the_interval(self):
        clk = _FakeClock()
        lim = BA.RateLimiter(sleep=clk.sleep)
        lim.wait()
        lim.wait()
        # 容差 1e-3: 假时钟是浮点累加, 用 1e-6 会因浮点漂移(实测 1.4e-6)假红。
        # 断言的是"间隔被施加了", 不是"浮点精度完美"。
        assert clk.slept and abs(sum(clk.slept) - 0.2) < 1e-3, clk.slept

    def test_throttle_detection_is_conservative(self):
        assert BA.looks_throttled("Too many requests")
        assert BA.looks_throttled("请求过于频繁")
        assert BA.looks_throttled("HTTP 429")
        # 普通错误不得被当成限流
        assert not BA.looks_throttled("no data for this symbol")
        assert not BA.looks_throttled("NullPointerException")
        assert not BA.looks_throttled("")


class TestContract2TargetClassification:
    """契约二: 用 symbols.parquet 分类, 北交所显式不可得。"""

    def test_classifies_by_market_column(self):
        import pandas as pd
        df = pd.DataFrame({"symbol": ["600000", "000001", "920000"],
                           "market": ["sh", "sz", "bj"]})
        cls = BA.classify_targets(df)
        assert cls["by_market"] == {"sh": ["600000"], "sz": ["000001"], "bj": ["920000"]}
        assert cls["fetchable"] == ["000001", "600000"]
        assert cls["unfetchable"]["bj"]["count"] == 1
        assert "北交所" in cls["unfetchable"]["bj"]["reason"]
        assert "运维口径" in cls["unfetchable"]["bj"]["action"]

    def test_empty_input_is_not_an_error(self):
        cls = BA.classify_targets(None)
        assert cls["fetchable"] == [] and cls["unfetchable"] == {}

    def test_falls_back_to_prefix_when_market_missing(self):
        import pandas as pd
        df = pd.DataFrame({"symbol": ["600000", "000001", "920000"]})
        cls = BA.classify_targets(df)
        assert set(cls["by_market"]) == {"sh", "sz", "bj"}

    def test_real_symbols_parquet_has_bj_and_they_are_unfetchable(self):
        """**真实数据**: h5i symbols.parquet 里确有 bj 标的, 且必须被标为不可得。"""
        try:
            from db import _h5i_symbols_df
            df = _h5i_symbols_df()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"取不到 symbols.parquet: {type(e).__name__}: {e}")
        if df is None or len(df) == 0:
            pytest.skip("symbols.parquet 为空")
        cls = BA.classify_targets(df)
        assert cls["fetchable"], "沪深的标的应当非空"
        if "bj" in cls["by_market"]:
            assert cls["unfetchable"].get("bj", {}).get("count") == len(cls["by_market"]["bj"])
            # 北交所不得混进可取的集合里(否则会被当成"取数失败"而非"不可得")
            assert not (set(cls["fetchable"]) & set(cls["by_market"]["bj"]))

    def test_code_conversion(self):
        assert BA.to_baostock_code("600000") == "sh.600000"
        assert BA.to_baostock_code("000001") == "sz.000001"
        assert BA.to_baostock_code("300750") == "sz.300750"
        assert BA.to_baostock_code("920000") == "bj.920000"
        assert BA.to_baostock_code("600000", "sh") == "sh.600000"
        # 已是 baostock 形式则原样(小写化)
        assert BA.to_baostock_code("SH.600000") == "sh.600000"


class TestContract3OutcomeClassification:
    """契约三: `{appended, partial, unfillable_gap, failed}`。"""

    def test_all_fetched_is_appended(self):
        r = BA.classify_outcome(requested=100, got=100)
        assert r["status"] == BA.APPENDED

    def test_nothing_fetched_is_failed(self):
        r = BA.classify_outcome(requested=100, got=0, error="引擎拒绝")
        assert r["status"] == BA.FAILED
        assert "引擎拒绝" in r["reason"]

    def test_partial_is_its_own_status_with_detail(self):
        """**核心**: 逐只接口下"部分成功"是常态, 必须单列一档并带缺口明细。

        当成功 ⇒ 缺口被静默吞掉; 当失败 ⇒ 因个别标的弃掉整天数据。两者都错。
        """
        r = BA.classify_outcome(requested=100, got=97)
        assert r["status"] == BA.PARTIAL
        assert r["missing"] == 3
        assert "97/100" in r["reason"]

    def test_gap_entirely_from_unfetchable_is_unfillable(self):
        """缺口**全部**来自"原理上取不到"的标的(北交所) -> `unfillable_gap`。

        这不是本次故障 —— 必须是独立档位, 否则每天都会因北交所报"失败"。
        """
        unf = {"bj": {"symbols": ["920000", "920001"], "count": 2}}
        r = BA.classify_outcome(requested=100, got=98, unfetchable=unf)
        assert r["status"] == BA.UNFILLABLE
        assert "原理上取不到" in r["reason"]

    def test_partial_beyond_unfetchable_stays_partial(self):
        """缺口**超出**不可得部分 -> 仍是 `partial`(那部分是真故障)。"""
        unf = {"bj": {"symbols": ["920000"], "count": 1}}
        r = BA.classify_outcome(requested=100, got=95, unfetchable=unf)
        assert r["status"] == BA.PARTIAL
        assert r["missing"] == 5
        assert "1 只属原理上不可得" in r["reason"]

    def test_status_values_match_the_specified_enum(self):
        assert {BA.APPENDED, BA.PARTIAL, BA.UNFILLABLE, BA.FAILED} == {
            "appended", "partial", "unfillable_gap", "failed"}


class TestFetchBatch:
    """逐只取数的批处理: 逐只失败被**收集**而非中断整批。"""

    def test_collects_failures_without_aborting(self):
        clk = _FakeClock()

        def fetch(code):
            if code == "bad":
                raise RuntimeError("boom")
            return [{"x": 1}]

        r = BA.fetch_batch(["a", "bad", "b"], fetch,
                           limiter=BA.RateLimiter(sleep=clk.sleep))
        assert set(r["rows"]) == {"a", "b"}
        assert "bad" in r["failed"] and "boom" in r["failed"]["bad"]

    def test_empty_result_counted_as_failure_not_throttle(self):
        clk = _FakeClock()

        def fetch(code):
            return []

        r = BA.fetch_batch(["a", "b", "c"], fetch,
                           limiter=BA.RateLimiter(sleep=clk.sleep))
        assert r["rows"] == {} and len(r["failed"]) == 3
        assert r["interval_ms"] == 200, "空数据不该触发退避"

    def test_throttle_error_triggers_backoff(self):
        clk = _FakeClock()

        def fetch(code):
            raise RuntimeError("Too many requests")

        r = BA.fetch_batch(["a", "b"], fetch,
                           limiter=BA.RateLimiter(sleep=clk.sleep))
        assert r["interval_ms"] == 1500
        assert r["limiter"]["throttle_signals"] == 2


class TestCrossValidateCriteria:
    """交叉校验: 判据是**写死的预期等式**, 不是"看着接近"。"""

    def test_expected_ratios_are_declared(self):
        assert CV.EXPECTED_VOLUME_RATIO["baostock"] == 1.0
        assert CV.EXPECTED_VOLUME_RATIO["akshare"] == 100.0
        assert CV.EXPECTED_VOLUME_RATIO["stockdb_sdk"] == 1.0

    @staticmethod
    def _pair(**kw):
        e = {"open": 9.03, "high": 9.07, "low": 8.97, "close": 9.04,
             "volume": 53266728.0, "change_pct": 0.33}
        o = dict(e)
        o.update(kw)
        return {("600000", "20260922"): e}, {("600000", "20260922"): o}

    def test_identical_passes(self):
        e, o = self._pair()
        r = CV.cross_validate(e, o, source="baostock")
        assert r["ok"] is True and r["n_compared"] == 1
        assert abs(r["volume_ratio"] - 1.0) < 1e-9

    def test_volume_off_by_100x_fails_loudly(self):
        """**核心**: 若某源悄悄改了单位(股->手), 比值会跳到 100 而**等式不成立**。

        这正是"写死预期"的价值 —— 否则差 100 倍也可能被当成"看着还行"。
        """
        e, o = self._pair(volume=532667.28)      # 差 100 倍
        r = CV.cross_validate(e, o, source="baostock")
        assert r["ok"] is False
        names = [m["name"] for m in r["mismatches"]]
        assert any("volume 比值" in n for n in names)

    def test_akshare_expects_100x(self):
        """akshare 的预期是 **100.0** —— 同一套判据对不同源给不同预期。"""
        e, o = self._pair(volume=53266728.0 / 100.0)
        r = CV.cross_validate(e, o, source="akshare")
        assert abs(r["volume_ratio"] - 100.0) < 1.0
        assert r["ok"] is True, r["mismatches"]

    def test_price_mismatch_fails(self):
        e, o = self._pair(close=9.99)
        r = CV.cross_validate(e, o, source="baostock")
        assert r["ok"] is False
        assert any("OHLC" in m["name"] for m in r["mismatches"])

    def test_pct_mismatch_fails(self):
        """除权日的关键: change_pct 必须逐值一致, 自算会错。"""
        e, o = self._pair(change_pct=-1.93)      # 自算(未调整前收)得到的错值
        r = CV.cross_validate(e, o, source="baostock")
        assert r["ok"] is False
        assert any("change_pct" in m["name"] for m in r["mismatches"])

    def test_no_overlap_is_not_silent_success(self):
        e, _ = self._pair()
        r = CV.cross_validate(e, {("999999", "20260922"): {}}, source="baostock")
        assert r["ok"] is False and r["n_compared"] == 0

    def test_summarize_is_one_line(self):
        e, o = self._pair()
        s = CV.summarize(CV.cross_validate(e, o, source="baostock"))
        assert "baostock" in s and "\n" not in s

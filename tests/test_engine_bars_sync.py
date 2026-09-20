# -*- coding: utf-8 -*-
"""engine_bars_sync 的回归测试 (2026-09-20, 生产摄入换源 SDK 直连).

CI 里**没有厂商引擎**(无 stockdb.exe、无 127.0.0.1:7899), 所以本测试全部通过
`rd=` 注入假引擎。这同时验证了模块的一条设计约束: 逻辑必须与"引擎是否在线"解耦,
否则它就只能在生产机上被测 —— 那等于没测。

重点覆盖**响亮失败**: 本项目反复吃过的亏是"看起来只是今天没数据"(静默 0 行),
故此处逐一构造: 引擎不可达 / 返回空 / 返回残截面, 三者都必须**抛异常**,
而不是返回一个空 DataFrame 让下游当成"平淡的一天"。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import engine_bars_sync as E  # noqa: E402


# --------------------------------------------------------------------------
# 假引擎
# --------------------------------------------------------------------------
def _row(code, day8="20260918", **kw):
    """造一行引擎返回。**严格**: 未知字段立即报错。

    初版这里用 `r.update(kw)` 静默接受一切, 结果测试里手误写成 `vol=1.0` 时
    并没有报错, 而是悄悄多出一个没人读的键、`volume` 保持默认值 —— 断言于是
    以"实现有问题"的面目失败。测试替身同样不能有静默接受面。
    """
    r = {"date": int(day8), "code": code, "name": "X",
         "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
         "pre_close": 10.0, "volume": 1000000.0, "amount": 10500000.0,
         "turnover": 0.42, "pct_chg": 5.0}
    unknown = set(kw) - set(r)
    if unknown:
        raise TypeError(f"_row 收到未知字段: {sorted(unknown)} (可用: {sorted(r)})")
    r.update(kw)
    return r


class FakeRd:
    """可控假引擎。同时扮演两个角色:
      · 按前缀取某交易日 (fetch_day 用)     -> per_prefix
      · 取某只股票的全历史   (交易日历/探针) -> history  (day == "*")
    """

    def __init__(self, per_prefix=None, fail=False, empty=False, history=None):
        self.per_prefix = per_prefix if per_prefix is not None else {"0*": 1500, "3*": 1400, "6*": 1300, "9*": 300}
        self.fail = fail
        self.empty = empty
        self.history = history if history is not None else ["20260917", "20260918"]
        self.calls = []

    def vals(self, table, key, day):
        self.calls.append((table, key, day))
        if self.fail:
            raise TimeoutError("Connect timeout")
        if day == "*":                      # 全历史查询(交易日历/健康探针)
            return [] if self.empty else [{"date": int(d)} for d in self.history]
        if self.empty:
            return []
        n = self.per_prefix.get(key, 0)
        # 用前缀+序号造唯一代码, 保证 6 位
        base = {"0*": 1000, "3*": 300000, "6*": 600000, "9*": 920000}.get(key, 0)
        return [_row(str(base + i).zfill(6), day8=day) for i in range(n)]


# --------------------------------------------------------------------------
# 1) 字段/口径映射
# --------------------------------------------------------------------------
class TestFieldMapping:
    def test_returns_exact_h5i_column_order(self):
        df, meta = E.fetch_day("2026-09-18", rd=FakeRd())
        assert list(df.columns) == E._H5I_COLS

    def test_pct_chg_maps_to_change_pct_and_turnover_passthrough(self):
        """引擎 pct_chg(%)=5.0 必须落到 change_pct; turnover 直通不换算。"""
        df, _ = E.fetch_day("2026-09-18", rd=FakeRd())
        assert (df["change_pct"] == 5.0).all()
        assert (df["turnover"] == 0.42).all()

    def test_volume_amount_passthrough_no_conversion(self):
        """volume=股 / amount=元 直通; 任何 100 倍换算都属于引入新口径。"""
        df, _ = E.fetch_day("2026-09-18", rd=FakeRd())
        assert (df["volume"] == 1000000.0).all()
        assert (df["amount"] == 10500000.0).all()

    def test_symbol_zero_padded_and_date_parsed(self):
        df, _ = E.fetch_day("2026-09-18", rd=FakeRd())
        assert df["symbol"].str.len().eq(6).all()
        assert str(df["date"].iloc[0].date()) == "2026-09-18"

    def test_accepts_dashed_day(self):
        df, meta = E.fetch_day("2026-09-18", rd=FakeRd())
        assert meta["day"] == "20260918"
        df2, meta2 = E.fetch_day("20260918", rd=FakeRd())
        assert meta2["day"] == "20260918"
        assert len(df) == len(df2)

    def test_dedups_same_symbol_keeping_last(self):
        class DupRd(FakeRd):
            def vals(self, table, prefix, day):
                if prefix != "0*":
                    return []
                return [_row("000001", volume=1.0, open=1.0, high=1.0, low=1.0, close=1.0),
                        _row("000001", volume=2.0, open=2.0, high=2.0, low=2.0, close=2.0)] + \
                       [_row(str(1000 + i).zfill(6)) for i in range(4200)]
        df, meta = E.fetch_day("2026-09-18", rd=DupRd())
        sub = df[df["symbol"] == "000001"]
        assert len(sub) == 1, "同一 symbol 必须只保留一行"
        assert float(sub["volume"].iloc[0]) == 2.0, "应保留最后一条"

    def test_numeric_columns_are_float64_to_match_h5i_schema(self):
        """h5i daily_bars 的数值列建表类型是 Float64。

        引擎把 volume/amount 返回成 Python int; 若原样透传, h5i 会以
        `schema mismatch: expected field volume Float64, got volume Int64` 拒绝 ——
        首次 --apply 实测即如此。这条用例把该约束钉在 CI 里。
        """
        class IntRd(FakeRd):
            def vals(self, table, prefix, day):
                # 刻意给**纯 int** 值, 模拟引擎真实返回
                return [_row(str(1000 + i).zfill(6), day8=day,
                             volume=1000000, amount=10500000) for i in range(4200)]

        df, _ = E.fetch_day("2026-09-18", rd=IntRd())
        for c in ["open", "high", "low", "close", "volume", "amount",
                  "change_pct", "turnover"]:
            assert df[c].dtype == "float64", f"{c} 必须是 float64, 实为 {df[c].dtype}"

    def test_no_object_dtype_leaks_into_numeric_columns(self):
        df, _ = E.fetch_day("2026-09-18", rd=FakeRd())
        for c in E._H5I_COLS:
            assert df[c].dtype != object or c == "symbol", f"{c} 不应是 object"

    def test_meta_reports_prefix_counts_and_rows(self):
        df, meta = E.fetch_day("2026-09-18", rd=FakeRd())
        assert meta["prefix_counts"] == {"0*": 1500, "3*": 1400, "6*": 1300, "9*": 300}
        assert meta["rows"] == 4500 and meta["raw_rows"] == 4500
        assert meta["endpoint"] == E.ENGINE_ENDPOINT


# --------------------------------------------------------------------------
# 2) 响亮失败（本模块存在的理由）
# --------------------------------------------------------------------------
class TestLoudFailure:
    def test_engine_down_raises_not_empty(self):
        with pytest.raises(E.EngineUnavailable) as ei:
            E.fetch_day("2026-09-18", rd=FakeRd(fail=True))
        assert "Connect timeout" in str(ei.value)
        assert E.ENGINE_ENDPOINT in str(ei.value)

    def test_empty_result_raises_not_empty_df(self):
        """非交易日 vs 引擎无数据**无法区分** —— 必须报错, 不能当成功。"""
        with pytest.raises(E.EngineUnavailable) as ei:
            E.fetch_day("2026-09-18", rd=FakeRd(empty=True))
        assert "0 行" in str(ei.value)

    def test_residual_cross_section_below_threshold_raises(self):
        """残截面(如引擎只回了沪市)绝不许写库 —— 会静默污染因子与权重。"""
        with pytest.raises(E.EngineUnavailable) as ei:
            E.fetch_day("2026-09-18", rd=FakeRd(per_prefix={"0*": 10, "3*": 5, "6*": 3, "9*": 1}))
        assert "阈值" in str(ei.value)

    def test_threshold_boundary_is_enforced(self):
        n = E.MIN_ROWS_PER_DAY
        with pytest.raises(E.EngineUnavailable):
            E.fetch_day("2026-09-18", rd=FakeRd(per_prefix={"0*": n - 1}))
        df, meta = E.fetch_day("2026-09-18", rd=FakeRd(per_prefix={"0*": n}))
        assert meta["rows"] == n

    def test_bad_date_rejected(self):
        for bad in ["2026-13", "abc", "2026091", ""]:
            with pytest.raises(ValueError):
                E.fetch_day(bad, rd=FakeRd())


# --------------------------------------------------------------------------
# 3) 健康探针
# --------------------------------------------------------------------------
class TestEngineProbe:
    def test_probe_ok_with_working_engine(self):
        p = E.engine_available(rd=FakeRd())
        assert p["ok"] is True
        assert p["day"] == "20260918", "探针必须报出数据到哪一天"
        assert p["trading_days"] == 2
        assert p["endpoint"] == E.ENGINE_ENDPOINT

    def test_probe_result_does_not_depend_on_today(self):
        """**长假安全**: 春节/国庆连休 8-9 天。

        初版探针用『从昨天回溯 N 天找有行的日子』, 长假里必然整段回溯皆空 ⇒
        **完全正常**的引擎被判不健康, 启动闸门会误拒。改为基于参考股票**全历史**后,
        结果与『今天是否交易日』无关: 历史很旧时仍报 ok, 只是 day 显示它有多旧
        （该不该放行由 ops/start_daemon.ps1 拿交易日历做**新鲜度比对**决定, 职责分离）。
        """
        p = E.engine_available(rd=FakeRd(history=["20260105"]))
        assert p["ok"] is True and p["day"] == "20260105"

    def test_probe_unhealthy_when_history_empty(self):
        """引擎连得上但返回空历史 —— 闸门必须判不健康, 否则又是静默 0 行。"""
        p = E.engine_available(rd=FakeRd(empty=True))
        assert p["ok"] is False and p["error"] and "无数据" in p["error"]

    def test_probe_reports_endpoint_and_reason_on_failure(self):
        p = E.engine_available(rd=FakeRd(fail=True))
        assert p["ok"] is False
        assert "Connect timeout" in p["error"] and E.ENGINE_ENDPOINT in p["error"]

    def test_probe_reports_missing_sdk(self, monkeypatch):
        def _boom():
            raise E.EngineUnavailable("厂商 SDK 不可用: ModuleNotFoundError")
        monkeypatch.setattr(E, "load_rd", _boom)
        p = E.engine_available()
        assert p["ok"] is False and "SDK" in p["error"]

    def test_probe_never_raises(self, monkeypatch):
        """探针是启动闸门的判据, 它自己绝不能抛 —— 否则闸门变成崩溃。"""
        monkeypatch.setattr(E, "load_rd", lambda: object())  # 没有 vals 方法
        p = E.engine_available()
        assert p["ok"] is False and p["error"]


# --------------------------------------------------------------------------
# 4) sync_days 编排（h5i 侧打桩, 保证任何机器上都不真写库）
# --------------------------------------------------------------------------
@pytest.fixture
def stubbed_h5i(monkeypatch):
    import h5i_sync
    seen = {"appended": [], "max": dt.date(2026, 9, 8)}

    monkeypatch.setattr(h5i_sync, "max_bar_date",
                        lambda force=False: seen["max"], raising=False)
    monkeypatch.setattr(h5i_sync, "_reset_cache", lambda: None, raising=False)

    def _append(df):
        seen["appended"].append(df.copy())
        return {"enabled": True, "appended": len(df), "skipped_rows": 0,
                "dates": sorted({str(d.date()) for d in df["date"]}), "ok": True}
    monkeypatch.setattr(h5i_sync, "append_daily_bars", _append, raising=False)
    return seen


class TestSyncDays:
    def test_dry_run_does_not_write(self, stubbed_h5i):
        res = E.sync_days(["2026-09-09"], rd=FakeRd(), dry_run=True)
        assert res["ok"] is True
        assert stubbed_h5i["appended"] == [], "预演绝不写库"
        assert res["days"][0]["h5i_write"] == "skipped (dry-run)"

    def test_apply_writes_duck_style_df(self, stubbed_h5i):
        res = E.sync_days(["2026-09-09"], rd=FakeRd(), dry_run=False)
        assert res["ok"] is True
        assert len(stubbed_h5i["appended"]) == 1
        df = stubbed_h5i["appended"][0]
        assert list(df.columns) == E._H5I_COLS
        assert res["appended"] == 4500
        assert res["h5i_max_before"] == "2026-09-08"

    def test_engine_probe_failure_aborts_before_fetching(self):
        rd = FakeRd(fail=True)
        res = E.sync_days(["2026-09-09", "2026-09-10"], rd=rd, dry_run=False)
        assert res["ok"] is False
        assert res["errors"] and "Connect timeout" in res["errors"][0]
        assert res["days"] == [], "探针失败就不该再逐日去撞引擎"
        assert rd.calls, "探针本身应该问过引擎"

    def test_one_bad_day_marks_overall_failure_but_keeps_others(self, stubbed_h5i):
        class HalfBad(FakeRd):
            def vals(self, table, prefix, day):
                if day == "20260910":
                    return []
                return super().vals(table, prefix, day)
        res = E.sync_days(["2026-09-09", "2026-09-10"], rd=HalfBad(), dry_run=False)
        assert res["ok"] is False, "有一天失败, 整体就不能报 ok"
        assert len(res["errors"]) == 1 and "20260910" in res["errors"][0]
        ok_days = [d for d in res["days"] if d.get("ok")]
        assert len(ok_days) == 1 and ok_days[0]["day"] == "20260909"

    def test_idempotent_repeat_is_skipped_by_h5i(self, monkeypatch, stubbed_h5i):
        """h5i 只追加 date > 现有最大; 重复跑必须被跳过而不是重复写入。"""
        import h5i_sync
        monkeypatch.setattr(h5i_sync, "max_bar_date",
                            lambda force=False: dt.date(2026, 9, 18), raising=False)

        def _append(df):
            return {"enabled": True, "appended": 0, "skipped_rows": len(df),
                    "dates": [], "ok": True}
        monkeypatch.setattr(h5i_sync, "append_daily_bars", _append, raising=False)
        res = E.sync_days(["2026-09-18"], rd=FakeRd(), dry_run=False)
        assert res["appended"] == 0 and res["skipped_rows"] == 4500


class TestTradingDays:
    """权威交易日来自引擎自身（本地 trade_calendar 是数据派生的, 追不上缺口）。"""

    class Rd:
        def __init__(self, per_sym, fail=False):
            self.per_sym, self.fail = per_sym, fail

        def vals(self, table, sym, day):
            assert day == "*", "交易日导出只能查全历史"
            if self.fail:
                raise TimeoutError("Connect timeout")
            return [{"date": int(d)} for d in self.per_sym.get(sym, [])]

    def test_union_over_refs_sorted_and_deduped(self):
        rd = self.Rd({"000001": ["20260909", "20260910"],
                      "600000": ["20260910", "20260911"]})
        assert E.engine_trading_days(refs=("000001", "600000"), rd=rd) == \
            ["20260909", "20260910", "20260911"]

    def test_single_ref_gap_is_filled_by_another_ref(self):
        """某只参考股停牌不该在交易日历上留洞。"""
        rd = self.Rd({"000001": ["20260909", "20260911"],   # 09-10 停牌
                      "600000": ["20260909", "20260910", "20260911"]})
        got = E.engine_trading_days(refs=("000001", "600000"), rd=rd)
        assert "20260910" in got

    def test_failure_raises_with_endpoint(self):
        with pytest.raises(E.EngineUnavailable) as ei:
            E.engine_trading_days(rd=self.Rd({}, fail=True))
        assert E.ENGINE_ENDPOINT in str(ei.value)

    def test_empty_history_raises(self):
        with pytest.raises(E.EngineUnavailable) as ei:
            E.engine_trading_days(rd=self.Rd({"000001": []}))
        assert "无数据" in str(ei.value)

    def test_calendar_days_inclusive(self):
        got = E._calendar_days("2026-09-09", "2026-09-13")
        assert got == ["20260909", "20260910", "20260911", "20260912", "20260913"]

    def test_weekend_is_classified_non_trading_not_failed(self):
        """周末必须被归为"非交易日(跳过)", 而不是让整批报失败。"""
        rd = self.Rd({"000001": ["20260911", "20260914"]})
        cand = E._calendar_days("2026-09-11", "2026-09-14")
        td = set(E.engine_trading_days(rd=rd))
        assert [d for d in cand if d in td] == ["20260911", "20260914"]
        assert [d for d in cand if d not in td] == ["20260912", "20260913"]


class TestSyncToLatest:
    """日常 pipeline 的入口: 水线由 h5i 给, 交易日由引擎给。"""

    class Rd:
        def __init__(self, days, fail_dates=()):
            self.days, self.fail_dates = days, set(fail_dates)

        def vals(self, table, key, day):
            if day == "*":                       # 交易日历查询
                return [{"date": int(d)} for d in self.days]
            if day in self.fail_dates:           # 某天取不回
                return []
            if day not in self.days:             # 非交易日
                return []
            base = 1000
            return [_row(str(base + i).zfill(6), day8=day) for i in range(E.MIN_ROWS_PER_DAY)]

    def _stub(self, monkeypatch, h5i_max):
        import h5i_sync
        seen = {"appended": []}
        monkeypatch.setattr(h5i_sync, "max_bar_date",
                            lambda force=False: h5i_max, raising=False)
        monkeypatch.setattr(h5i_sync, "_reset_cache", lambda: None, raising=False)

        def _append(df):
            seen["appended"].append(df.copy())
            return {"enabled": True, "appended": len(df), "skipped_rows": 0,
                    "dates": sorted({str(d.date()) for d in df["date"]}), "ok": True}
        monkeypatch.setattr(h5i_sync, "append_daily_bars", _append, raising=False)
        return seen

    def test_only_days_after_h5i_waterline_are_fetched(self, monkeypatch):
        import datetime as _dt
        seen = self._stub(monkeypatch, _dt.date(2026, 9, 11))
        rd = self.Rd(["20260909", "20260910", "20260911", "20260914", "20260915"])
        res = E.sync_to_latest(rd=rd, apply=True)
        assert res["ok"] is True and res["used"] == "engine"
        assert res["planned_days"] == ["20260914", "20260915"], "水线(09-11)之前的不该再取"
        assert len(seen["appended"]) == 2

    def test_already_caught_up_writes_nothing(self, monkeypatch):
        import datetime as _dt
        seen = self._stub(monkeypatch, _dt.date(2026, 9, 18))
        res = E.sync_to_latest(rd=self.Rd(["20260917", "20260918"]), apply=True)
        assert res["ok"] is True and res["appended"] == 0
        assert seen["appended"] == [] and "已追平" in res["note"]

    def test_truncation_is_reported_not_hidden(self, monkeypatch):
        """落后很多时必须**明说被截断**, 不能悄悄只补一部分。"""
        import datetime as _dt
        self._stub(monkeypatch, _dt.date(2026, 1, 1))
        days = [f"2026{m:02d}{d:02d}" for m in range(1, 4) for d in (5, 6, 7)]
        res = E.sync_to_latest(rd=self.Rd(days), apply=False, max_days=5)
        assert res["truncated_days"] == len(days) - 5
        assert res["missing_trading_days"] == len(days)
        assert len(res["planned_days"]) == 5

    def test_partial_failure_propagates_not_ok(self, monkeypatch):
        """某一天取不回 ⇒ 整体 ok=False, 且**错误必须指向那一天**。

        (初版这里断言太弱: 探针先失败也能让它通过, 于是它其实没测到目标分支 ——
         典型的假通过。现在探针必然成功, 失败只可能来自那一天的取数。)
        """
        import datetime as _dt
        self._stub(monkeypatch, _dt.date(2026, 9, 8))
        rd = self.Rd(["20260909", "20260910"], fail_dates=("20260910",))
        res = E.sync_to_latest(rd=rd, apply=True)
        assert res["engine_probe"]["ok"] is True, "探针必须成功, 否则本用例测不到目标分支"
        assert res["ok"] is False
        assert any("20260910" in e for e in res["errors"]), res["errors"]

    def test_engine_down_raises_before_any_write(self, monkeypatch):
        import datetime as _dt
        seen = self._stub(monkeypatch, _dt.date(2026, 9, 8))
        with pytest.raises(E.EngineUnavailable):
            E.sync_to_latest(rd=FakeRd(fail=True), apply=True)
        assert seen["appended"] == [], "引擎故障时绝不能已经写过库"


# --------------------------------------------------------------------------
# 5) 与生产约定的耦合关系（回退时会自动报警）
# --------------------------------------------------------------------------
class TestContractCoupling:
    def test_min_rows_threshold_is_below_real_market_size(self):
        """实测全市场 5480±10; 阈值必须**明显低于**它, 否则真交易日会被误拒。"""
        assert 3000 <= E.MIN_ROWS_PER_DAY <= 5000

    def test_prefixes_cover_all_four_markets(self):
        assert set(E.PREFIXES) == {"0*", "3*", "6*", "9*"}, \
            "北交所在两端都是 92xxxx(9*); 43/83/87 实测皆空"

    def test_columns_match_h5i_sync_contract(self):
        """与 h5i_sync 的实际消费列对齐 —— 列名漂移必须在这里被抓住。"""
        import h5i_sync
        assert set(E._H5I_COLS) == set(h5i_sync._DAILY_COLS)

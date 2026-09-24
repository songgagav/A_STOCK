# -*- coding: utf-8 -*-
"""Baostock 按需回填的守卫 (2026-09-25, 用户决策「做, 但限定为按需回填」)。

## 为什么这些守卫是必要的

补数会**改动生产行情库**。三个真实风险:

1. **写错手段**: `append` 对水位之前的日期抛 `sort_order_violation`; `write()` 是
   **整表替换**(实测行数从 4 掉回 2) —— 用它等于删掉全市场历史。故必须用
   `plan_replace_range`。这条在源码层面锁住。
2. **半截写入**: 逐只接口下"部分成功"是常态。若某天只取到 300 只就写进去,
   那一天就变成**残截面**, 而残截面会静默污染下游(本仓最忌讳)。
   故必须整日过残截面阈值, 不过就**整日拒写**。
3. **来源不可辨**: `daily_bars` **没有 `source` 列**, 混入的行从表内无法区分来源。
   故必须有 provenance 留痕, 否则就是口径漂移。

本文件用**注入的假 fetcher**验证这三条, 不联网、不碰生产库。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import baostock_backfill as BF  # noqa: E402


def _rec(code, day, close=10.0, vol=100000):
    """造一条 baostock 形状的原始记录(字段名与真实接口一致)。"""
    return {
        "date": day, "code": code, "open": f"{close:.4f}", "high": f"{close*1.01:.4f}",
        "low": f"{close*0.99:.4f}", "close": f"{close:.4f}", "preclose": f"{close:.4f}",
        "volume": str(vol), "amount": f"{vol*close:.4f}", "adjustflag": "3",
        "turn": "0.1500", "tradestatus": "1", "pctChg": "0.0000",
    }


def _frame(recs):
    import pandas as pd
    return pd.DataFrame(recs)


def _fake_fetch_range(n_symbols, days, *, not_requested=(), fail_n=0):
    """造一个 fetch_range, 通过 `on_row` 流式回调交付(与真实实现同路径)。"""
    def _f(symbols, *, start, end, symbols_df=None, on_row=None, **kw):
        rows, failed = {}, {}
        n = min(n_symbols, len(symbols))
        for i, sym in enumerate(symbols[:n]):
            if i < fail_n:
                failed[sym] = "空数据(停牌/退市/该日无成交)"
                continue
            recs = [_rec(f"x.{sym}", d, close=10.0 + i * 0.01) for d in days]
            rows[sym] = _frame(recs)
            if on_row is not None:
                on_row(sym, rows[sym])       # 与真实实现一样带裸代码回调
        return {"status": "appended", "requested": len(symbols), "rows": rows,
                "failed": failed, "not_requested": sorted(not_requested),
                "limiter": {"calls": n}, "start": start, "end": end}
    return _f


class _Symbols:
    def __init__(self, syms):
        import pandas as pd
        self._df = pd.DataFrame({"symbol": syms,
                                 "market": ["sh" if s.startswith("6") else "sz"
                                            for s in syms]})

    def __getitem__(self, k):
        return self._df[k]

    @property
    def columns(self):
        return self._df.columns


class TestMissingDaysIsPureAndCorrect:
    def test_reports_gap_before_the_watermark_too(self):
        """缺口可能落在**水位之前** —— 只比"水位之后"会漏掉中间空洞。"""
        assert BF.missing_days(["2026-09-20", "2026-09-24"],
                               ["2026-09-22", "2026-09-24"]) == ["2026-09-20"]

    def test_empty_inputs(self):
        assert BF.missing_days([], ["2026-09-22"]) == []
        assert BF.missing_days(["2026-09-23"], []) == ["2026-09-23"]

    def test_normalizes_timestamp_like_values(self):
        """传入的 present 可能是 timestamp/ 带时分秒 —— 必须只比日期部分。"""
        assert BF.missing_days(["2026-09-23"], ["2026-09-23 00:00:00"]) == []


class TestWriteUsesRangeReplaceNotAppendOrWrite:
    """**核心**: 写入必须走 `plan_replace_range`。

    这条用源码断言而不是行为断言, 因为行为断言需要真库; 而这两个错误手段
    都在真库上**不可逆**(`write` 会清空全表)。
    """

    def test_source_uses_plan_replace_range(self):
        import inspect
        src = inspect.getsource(BF._write_day)
        assert "plan_replace_range" in src, "写入没用 plan_replace_range"
        assert "append(" not in src, (
            "用了 append —— 它对**水位之前**的日期抛 sort_order_violation, "
            "而缺口恰恰可能落在水位之前")
        assert "db.write(" not in src, (
            "用了 write() —— 实测它是**整表替换**(行数从 4 掉回 2), 会删掉全市场历史")

    def test_range_is_exactly_one_day(self):
        """替换区间必须是 [该日 00:00, 次日 00:00), 不能多覆盖一天。"""
        a = BF._us("2026-09-23")
        b = BF._us("2026-09-24") - 1
        assert b - a == 86_400 * 1_000_000 - 1, "区间不等于一整天"
        assert dt.datetime.fromtimestamp(a / 1e6).date() == dt.date(2026, 9, 23)


class TestWholeDayRejection:
    def test_too_few_rows_rejects_the_whole_day(self, monkeypatch):
        """残截面(行数低于阈值)**整日拒写** —— 不写半截。

        这是补数最危险的失败模式: 某天只取到少量标的却写进去, 那一天就是残截面,
        而下游分不出"这天真只有 300 只成交"与"补数补了一半"。
        """
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))
        syms = [f"6{i:05d}" for i in range(50)]        # 远低于 1000 阈值
        res = BF.backfill_days(["2026-09-23"], write=False,
                               symbols_df=_Symbols(syms),
                               fetch_range=_fake_fetch_range(len(syms), ["2026-09-23"]))
        assert res["days"]["2026-09-23"]["status"] == "normalize_failed", res["days"]
        assert "残截面" in res["days"]["2026-09-23"].get("error", "")
        assert res["written"] == []

    def test_enough_rows_passes(self, monkeypatch):
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))
        syms = [f"6{i:05d}" for i in range(1200)]      # 高于 1000 阈值
        res = BF.backfill_days(["2026-09-23"], write=False,
                               symbols_df=_Symbols(syms),
                               fetch_range=_fake_fetch_range(len(syms), ["2026-09-23"]))
        assert res["days"]["2026-09-23"]["status"] == "ready", res["days"]
        assert res["written"] == [], "dry-run 不得写入"


class TestSuspendedRowsAreDroppedNotFatal:
    """停牌股量额为空是 **A 股每日常态**, 不得让它废掉整日。

    2026-09-25 全市场 dry-run 实测: 5212 只全部取到、**0 次限流退避**, 而两天都被
    `data_quality_guard` **整日拒绝** —— 原因是 **12 只**的 `volume`/`amount` 为空 ⇒
    `core_nan` 命中。查原始记录, 这 12 只**全部** `tradestatus=0`(停牌)。

    即 **12 只停牌股废掉 5200 只有效行** —— 与本仓已修过的 `db_update` 12/12
    误熔断是**同一形状**: 不重要的东西坏了, 导致重要的事停摆。
    """

    def test_drop_suspended_keeps_only_trading_rows(self):
        recs = [
            {"code": "sh.600000", "tradestatus": "1"},
            {"code": "sz.000016", "tradestatus": "0"},      # 停牌
            {"code": "sz.000002", "tradestatus": "1"},
            {"code": "sz.000003", "tradestatus": ""},        # 空 => 不可信, 一并剔
        ]
        keep, n, sample = BF.drop_suspended(recs)
        assert n == 2 and len(keep) == 2
        assert [r["code"] for r in keep] == ["sh.600000", "sz.000002"]
        assert "sz.000016" in sample

    def test_empty_input(self):
        assert BF.drop_suspended([]) == ([], 0, [])
        assert BF.drop_suspended(None) == ([], 0, [])

    def test_suspension_is_not_a_defect_but_missing_data_is(self):
        """**反证**: 剔停牌之后, `tradestatus=1` 却缺量额的行**必须仍被守卫拒**。

        为什么这条必须有: 如果只图方便"把 NaN 行丢掉", 就会把**真缺陷**也一起放过 ——
        那样补数会静默产出缺数据。故判据只能按 `tradestatus` 这个**显式声明**,
        不能按 NaN。
        """
        import data_quality_guard as DQ
        import bars_ingest as BI
        good = {"date": "2026-09-23", "code": "sh.600000", "open": "9.0000",
                "high": "9.1000", "low": "8.9000", "close": "9.0500",
                "preclose": "9.0000", "volume": "1000", "amount": "9050",
                "adjustflag": "3", "turn": "0.1", "tradestatus": "1", "pctChg": "0.5"}
        # 停牌行: 量额为空 -> 剔掉后守卫应通过
        susp = dict(good, code="sz.000016", tradestatus="0", volume="", amount="")
        keep, n, _ = BF.drop_suspended([good, susp])
        assert n == 1 and len(keep) == 1
        norm, _ = BI.normalize(keep, "baostock", min_rows_per_day=0)
        assert DQ.validate_daily_bars(norm)["ok"] is True
        # 但"在交易"却缺量额 -> 必须被拒(这是真缺陷, 不能靠剔停牌掩盖)
        broken = dict(good, volume="", amount="", tradestatus="1")
        norm2, _ = BI.normalize([broken], "baostock", min_rows_per_day=0)
        g2 = DQ.validate_daily_bars(norm2)
        assert g2["ok"] is False and g2["checks"]["core_nan"] >= 1, g2

    def test_backfill_records_how_many_were_dropped(self, monkeypatch):
        """剔了多少只**必须记进结果** —— 否则"这个日少了几只"无从解释。"""
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))
        syms = [f"6{i:05d}" for i in range(1200)]
        # 让 fetcher 把前 12 只标成停牌
        base = _fake_fetch_range(len(syms), ["2026-09-23"])

        def _f(symbols, *, start, end, symbols_df=None, on_row=None, **kw):
            out = base(symbols, start=start, end=end, symbols_df=symbols_df)
            if on_row:
                for i, (sym, df) in enumerate(list(out["rows"].items())):
                    if i < 12:
                        df = df.copy()
                        df["tradestatus"] = "0"
                        df["volume"] = ""
                        df["amount"] = ""
                    on_row(sym, df)
            return out

        res = BF.backfill_days(["2026-09-23"], write=False,
                               symbols_df=_Symbols(syms), fetch_range=_f)
        info = res["days"]["2026-09-23"]
        assert info["suspended_dropped"] == 12, info
        assert info["status"] == "ready", info
        assert info["normalize"]["rows"] == 1188, info


class TestDryRunWritesNothing:
    def test_no_write_flag_means_no_db_call(self, monkeypatch):
        """`write=False` 时**绝不能**碰到数据库。"""
        called = []
        monkeypatch.setattr(BF, "_write_day",
                            lambda *a, **k: called.append(a) or {})
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))
        syms = [f"6{i:05d}" for i in range(1200)]
        BF.backfill_days(["2026-09-23"], write=False, symbols_df=_Symbols(syms),
                         fetch_range=_fake_fetch_range(len(syms), ["2026-09-23"]))
        assert not called, "dry-run 竟然调用了写入"

    def test_write_flag_does_call_and_leaves_provenance(self, monkeypatch, tmp_path):
        """`write=True` 必须写库 **且** 留 provenance。"""
        import tempfile
        prov = os.path.join(tempfile.mkdtemp(), "p.jsonl")
        monkeypatch.setattr(BF, "PROVENANCE_FP", prov)
        wrote = []
        monkeypatch.setattr(BF, "_write_day",
                            lambda d, frame, **k: wrote.append((d, len(frame))) or {"rows": len(frame)})
        syms = [f"6{i:05d}" for i in range(1200)]
        res = BF.backfill_days(["2026-09-23"], write=True, symbols_df=_Symbols(syms),
                               fetch_range=_fake_fetch_range(len(syms), ["2026-09-23"]))
        assert wrote and wrote[0][0] == "2026-09-23", wrote
        assert res["written"] == ["2026-09-23"]
        # provenance: daily_bars 没有 source 列, 只能靠这个文件区分来源
        assert os.path.isfile(prov), "没有 provenance 留痕 —— 来源将无法区分"
        rec = json.loads(open(prov, encoding="utf-8").read().strip().splitlines()[-1])
        assert rec["day"] == "2026-09-23" and rec["source"] == "baostock"
        assert rec["rows"] > 900


class TestBeijingIsExplicitNotSilent:
    def test_bj_symbols_are_reported_not_silently_dropped(self, monkeypatch):
        """北交所必须**显式报出**, 不得静默少写、也不得当成失败。"""
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))
        syms = [f"6{i:05d}" for i in range(1200)]
        bj = ["920001", "920002"]
        res = BF.backfill_days(["2026-09-23"], write=False,
                               symbols_df=_Symbols(syms),
                               fetch_range=_fake_fetch_range(
                                   len(syms), ["2026-09-23"], not_requested=bj))
        assert res["bj_not_requested"] == sorted(bj)

    def test_no_rows_at_all_is_failure_not_success(self, monkeypatch):
        """一行都没取到 -> `ok=False` 且带 error。**不得**当成补好了。"""
        import tempfile
        monkeypatch.setattr(BF, "PROVENANCE_FP",
                            os.path.join(tempfile.mkdtemp(), "p.jsonl"))

        def _empty(symbols, *, start, end, symbols_df=None, on_row=None, **kw):
            return {"status": "failed", "rows": {}, "failed": {"600000": "连不上"},
                    "not_requested": [], "limiter": {}, "start": start, "end": end}

        res = BF.backfill_days(["2026-09-23"], write=True,
                               symbols_df=_Symbols(["600000"]), fetch_range=_empty)
        assert res["ok"] is False and res["error"], res
        assert res["written"] == []

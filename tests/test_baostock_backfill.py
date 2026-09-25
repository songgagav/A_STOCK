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


class TestProvenanceCarriesTheSemanticClarification:
    """留痕必须记清「回填**不改变**选股口径」(用户 2026-09-25 指出的语义澄清)。

    为什么这条是必需的: 回填改的是 **h5i 的水位**, 而**引擎探针读的是厂商引擎**。
    若不显式记下这一点, 下次看到 `target_plan.data_lag_days = 3` 的人会
    **误以为回填失败** —— 而实际是"回填本来就不改选股口径, 只能等厂商发布"。
    """

    def test_record_has_the_clarification_fields(self, monkeypatch):
        import tempfile
        import io as _io
        fp = os.path.join(tempfile.mkdtemp(), "p.jsonl")
        monkeypatch.setattr(BF, "PROVENANCE_FP", fp)
        BF._append_provenance("2026-09-23", 5200, {"bj_not_requested": [None] * 339})
        rec = json.loads(_io.open(fp, encoding="utf-8").read().strip())
        assert rec["action"] == "backfill"
        assert rec["affects_selection"] is False, (
            "必须**明文**写 false —— 这是给未来读日志的人看的结论, "
            "不能靠他自己推断")
        assert rec["engine_probe_unchanged"] is True
        assert "h5i_watermark_after" in rec
        assert "engine_day_at_backfill" in rec
        assert "不改变选股口径" in rec["note"], rec["note"][:120]
        assert rec["bj_not_requested"] == 339

    def test_helpers_never_invent_a_date(self, monkeypatch):
        """取不到引擎日/水位时返回 **None**, 不得猜一个日期。

        猜错的日期比 None 危险得多: 它会被当成事实写进留痕, 而留痕正是
        事后追溯的唯一依据。
        """
        import engine_bars_sync as E
        monkeypatch.setattr(E, "engine_available", lambda: {"ok": False, "error": "x"})
        assert BF._engine_day() is None
        monkeypatch.setattr(E, "engine_available", lambda: {"ok": True, "day": "bad"})
        assert BF._engine_day() is None
        monkeypatch.setattr(E, "engine_available", lambda: {"ok": True, "day": "20260922"})
        assert BF._engine_day() == "2026-09-22"

    def test_helpers_tolerate_exceptions(self, monkeypatch):
        """辅助函数抛错也必须返回 None —— 留痕不能把主流程拖垮。"""
        import engine_bars_sync as E

        def boom():
            raise RuntimeError("engine down")
        monkeypatch.setattr(E, "engine_available", boom)
        assert BF._engine_day() is None

    def test_unfillable_symbols_are_recorded_with_a_machine_readable_reason(
            self, monkeypatch):
        """[用户清单第 3 项] 补不到的标的必须**逐只**记下 + 机器可读的原因。

        ## 为什么"记名单"而不是"只记计数"

        北交所 339 只是 **Baostock 原理上的覆盖缺口**(它对 `bj.*` 返回空),
        不是本次故障。只记 `bj_not_requested: 339` 时, 读的人**拿不到具体是哪些标的** ——
        想核对"我关心的那只补上了吗"就得自己重算一遍。记下名单才能逐只核对。

        ## 为什么还要 `unfillable_reason`

        只有中文说明的话, 下游**没法按原因归类统计**。`baostock_no_bj_data` 是稳定标识,
        将来若出现第二种"补不到"的原因(如某源不支持 ST), 可以分开计数而不必解析文本。

        **验收要求**: `backfill_provenance.jsonl` 中包含 `unfillable_symbols`。
        """
        import tempfile
        import io as _io
        fp = os.path.join(tempfile.mkdtemp(), "p.jsonl")
        monkeypatch.setattr(BF, "PROVENANCE_FP", fp)
        bj = [f"92{i:04d}" for i in range(339)]
        BF._append_provenance("2026-09-23", 5200, {"bj_not_requested": bj})
        rec = json.loads(_io.open(fp, encoding="utf-8").read().strip())
        assert "unfillable_symbols" in rec, "验收要求: 留痕里必须有 unfillable_symbols"
        assert rec["unfillable_symbols"] == sorted(bj)
        assert rec["unfillable_count"] == 339
        assert rec["unfillable_reason"] == "baostock_no_bj_data", (
            "原因必须是**机器可读的稳定标识**, 不能只有中文说明")
        assert "北交所" in rec["note"], "note 应向人说明这 339 只为什么补不到"

    def test_no_unfillable_means_none_not_a_placeholder(self, monkeypatch):
        """没有补不到的标的时, `unfillable_reason` 应为 **None**, 不得编造占位符。

        与 DISC-1「留痕宁可 None, 不猜」同一条: 写 `"none"` / `"-"` / `""` 这类占位符,
        下游就得学会识别多种"空"的写法 —— 那是自找的解析歧义。
        """
        import tempfile
        import io as _io
        fp = os.path.join(tempfile.mkdtemp(), "p.jsonl")
        monkeypatch.setattr(BF, "PROVENANCE_FP", fp)
        BF._append_provenance("2026-09-24", 5200, {})
        rec = json.loads(_io.open(fp, encoding="utf-8").read().strip())
        assert rec["unfillable_symbols"] == []
        assert rec["unfillable_count"] == 0
        assert rec["unfillable_reason"] is None, rec["unfillable_reason"]


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


class TestWritePathAgainstRealH5i:
    """**真实写入路径**的守卫 —— 跑在**隔离临时库**上, 用**生产同款 schema**。

    为什么必须这么测(而不是只做源码断言):
    `_write_day` 的第一个版本自己 `pa.Table.from_pandas(frame)` —— 而归一化后的帧
    **只有 `date`, 没有 `ts`**, h5i 的 schema 却是 `ts: timestamp[us] not null`。
    那样会写出**一列 null ts**(或被 schema 拒), 而**源码断言看不出这个**。
    实测确认: 必须先经 `h5i_sync._df_to_h5i_table` 生成 `ts`。
    """

    @pytest.fixture()
    def temp_db(self):
        h5i_db = pytest.importorskip("h5i_db", reason="只有生产解释器/venv310 有 h5i_db")
        import tempfile
        prod_fp = os.path.join(_REPO, "data", "h5i", "market.db")
        td = tempfile.mkdtemp()
        db = h5i_db.Database(os.path.join(td, "market.db"), create=True)
        # 从生产库**只读**取 schema, 保证与生产结构逐字段一致
        if os.path.exists(prod_fp):
            prod = h5i_db.Database(prod_fp)
            sch = prod.schema("daily_bars")
            prod.close()
        else:
            import pyarrow as pa
            sch = pa.schema([pa.field("ts", pa.timestamp("us")), ("symbol", pa.string())] +
                            [(c, pa.float64) for c in
                             ("open", "high", "low", "close", "volume", "amount",
                              "change_pct", "turnover")])
        db.create_table("daily_bars", sch, time_column="ts")
        yield db
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    def _frame(self, n=1200, day="2026-09-23"):
        import bars_ingest as BI
        recs = []
        for i in range(n):
            px = 10.0 + i * 0.01
            recs.append({"date": day, "code": "sh.6%05d" % i,
                         "open": f"{px}", "high": f"{px*1.01}", "low": f"{px*0.99}",
                         "close": f"{px}", "preclose": f"{px}", "volume": "100000",
                         "amount": f"{px*100000}", "adjustflag": "3", "turn": "0.15",
                         "tradestatus": "1", "pctChg": "0.0"})
        frame, _meta = BI.normalize(recs, "baostock")
        return frame

    def test_ts_is_populated_and_not_null(self, temp_db):
        """**核心**: 写进去的行必须有非空 `ts`, 且恰好是那一天。"""
        frame = self._frame()
        assert "ts" not in frame.columns, (
            "前置: 归一化后的帧本就没有 ts —— 这正是必须先过 _df_to_h5i_table 的原因")
        BF._write_day("2026-09-23", frame, db=temp_db)
        df = temp_db.read("daily_bars")
        assert df.num_rows == 1200
        ts = df.column("ts").to_pylist()
        assert not any(x is None for x in ts), "写出了 null ts —— schema 要求 not null"
        assert sorted({str(x)[:10] for x in ts}) == ["2026-09-23"]

    def test_rewrite_same_day_is_idempotent(self, temp_db):
        """同一天重复写**不得翻倍** —— 补数可能被重跑, 幂等是它的基本要求。"""
        frame = self._frame()
        BF._write_day("2026-09-23", frame, db=temp_db)
        n1 = temp_db.read("daily_bars").num_rows
        BF._write_day("2026-09-23", frame, db=temp_db)
        n2 = temp_db.read("daily_bars").num_rows
        assert n1 == n2 == 1200, (n1, n2)

    def test_replace_does_not_touch_other_days(self, temp_db):
        """替换区间**只覆盖那一天** —— 相邻日期必须原样保留。

        这条是"用 `write()` 会删全表"那个风险的正面验证: 先写相邻日, 再替换中间日,
        相邻日必须还在。
        """
        BF._write_day("2026-09-22", self._frame(day="2026-09-22"), db=temp_db)
        BF._write_day("2026-09-24", self._frame(day="2026-09-24"), db=temp_db)
        before = sorted({str(x)[:10] for x in temp_db.read("daily_bars").column("ts").to_pylist()})
        assert before == ["2026-09-22", "2026-09-24"], before
        BF._write_day("2026-09-23", self._frame(day="2026-09-23"), db=temp_db)
        after = sorted({str(x)[:10] for x in temp_db.read("daily_bars").column("ts").to_pylist()})
        assert after == ["2026-09-22", "2026-09-23", "2026-09-24"], after


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

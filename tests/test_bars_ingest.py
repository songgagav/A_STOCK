# -*- coding: utf-8 -*-
"""`bars_ingest` 阶段一重构的**逐位等价**守卫 (2026-09-22)。

## 验收标准(用户给定)

> 重构后, 用同一批数据, `bars_ingest.py` 的写入结果与现有 `engine_bars_sync.py`
> 逐位一致。

## 本文件怎么做到"逐位"

把重构**前**的归一化逻辑(`_legacy_normalize`, 从原 `engine_bars_sync.fetch_day`
逐行抄录)与重构**后**的 `bars_ingest.normalize(df, "stockdb_sdk")` 放在一起,
对同一批原始记录跑, 用 `assert_frame_equal(check_exact=True)` 比对。

**保留旧实现副本**是刻意的: 若只断言"新实现输出符合某个手写期望", 那期望本身
可能抄错; 而拿"线上跑过的旧实现"当基准, 才真正锁住"行为未变"。

另外还锁三件事(都是重构最容易弄丢的):
  1. **列顺序** == `H5I_COLS`(顺序变了 h5i 写入会错位);
  2. **dtype 落成 float64**(不是 int —— 否则 h5i 报 schema mismatch);
  3. 两道闸门仍在(`data_quality_guard` 结构校验 + 单调追加)。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import bars_ingest as BI  # noqa: E402
import engine_bars_sync as E  # noqa: E402

_H5I_COLS = ["symbol", "date", "open", "high", "low", "close",
             "volume", "amount", "change_pct", "turnover"]

_FIELD_MAP = {"open": "open", "high": "high", "low": "low", "close": "close",
              "volume": "volume", "amount": "amount",
              "pct_chg": "change_pct", "turnover": "turnover"}


def _legacy_normalize(records):
    """**重构前** `engine_bars_sync.fetch_day` 的归一化逻辑(逐行抄录, 勿改)。

    它的唯一用途是当基准。若哪天它被"顺手更新"成新实现, 这个测试就失去意义 ——
    故此处刻意不 import 任何新代码。
    """
    df = pd.DataFrame(records)
    for src in _FIELD_MAP:
        if src not in df.columns:
            df[src] = None
    out = pd.DataFrame({
        "symbol": df["code"].astype(str).str.zfill(6),
        "date": pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce"),
    })
    for src, dst in _FIELD_MAP.items():
        out[dst] = pd.to_numeric(df[src], errors="coerce").astype("float64")
    out = out.dropna(subset=["date"]).drop_duplicates("symbol", keep="last")
    out = out[_H5I_COLS].sort_values("symbol").reset_index(drop=True)
    return out


def _rec(code, date, **kw):
    r = {"code": code, "date": date, "open": 10.0, "high": 11.0, "low": 9.0,
         "close": 10.5, "volume": 12345, "amount": 67890,
         "pct_chg": 0.5, "turnover": 1.2}
    r.update(kw)
    return r


#: 刻意糅进各种边界: 前导零 / 已零填充 / 重复 symbol / 非法日期 / 缺字段 /
#: int 型量(必须落成 float64) / NaN
_SAMPLE = [
    _rec("1", "20260922"),                 # 需零填充 -> 000001
    _rec("2", "20260922", volume=999),     # int 型 volume
    _rec("000003", "20260922", amount=None),
    _rec("4", "20260922", pct_chg=-1.25, turnover=None),
    _rec("1", "20260922", close=99.0),     # 重复 symbol -> keep="last"
    _rec("5", "not-a-date"),               # 非法日期 -> dropna
    _rec("6", "20260922", open=float("nan")),
]


class TestBitForBitEquivalence:
    """**核心验收**: 新旧归一化逐位一致。"""

    def test_records_normalize_identically(self):
        old = _legacy_normalize(_SAMPLE)
        new, _meta = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        assert_frame_equal(new, old, check_exact=True, check_dtype=True)

    def test_column_order_matches_contract(self):
        new, _ = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        assert list(new.columns) == _H5I_COLS

    def test_numeric_columns_are_float64(self):
        """**必须是 float64** —— int 会让 h5i 报 schema mismatch。"""
        new, _ = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        for c in ("open", "high", "low", "close", "volume", "amount",
                  "change_pct", "turnover"):
            assert new[c].dtype == "float64", f"{c} 落成了 {new[c].dtype}, 应为 float64"

    def test_leading_zero_padding(self):
        new, _ = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        assert "000001" in set(new["symbol"])
        assert "000003" in set(new["symbol"])

    def test_duplicate_symbol_keeps_last(self):
        new, _ = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        row = new[new["symbol"] == "000001"]
        assert len(row) == 1
        assert float(row.iloc[0]["close"]) == 99.0, "去重策略不是 keep='last'"

    def test_invalid_date_dropped(self):
        new, _ = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        assert "000005" not in set(new["symbol"])

    def test_engine_path_equals_bars_ingest_path(self):
        """走 `engine_bars_sync.fetch_day` 的产物, 与直调 `bars_ingest` 一致。

        这条锁的是**接线**: 若 `fetch_day` 忘了委托给 `bars_ingest`,
        两条路径就会各自演化。
        """
        import inspect
        src = inspect.getsource(E.fetch_day)
        assert "bars_ingest" in src, "fetch_day 没有委托给 bars_ingest —— 重构没接上"
        assert "_BI.normalize" in src or "bars_ingest.normalize" in src


class TestRealEngineEquivalence:
    """用**真实引擎数据**再验一遍逐位等价(合成样例之外的独立证据)。

    需要 stockdb 引擎在线; 不在线则跳过 —— 跳过不等于通过, 故本类只在
    `.venv310`/生产解释器下会真跑, CI 里通常会 skip。
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def engine_df():
        try:
            df, meta = E.fetch_day("20260922")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"引擎不可用, 跳过真实数据等价性验证: {type(e).__name__}: {e}")
        return df, meta

    def test_real_data_matches_contract(self, engine_df):
        df, meta = engine_df
        assert list(df.columns) == _H5I_COLS
        assert len(df) == meta["rows"] > 1000
        assert str(df["volume"].dtype) == "float64"
        assert str(df["amount"].dtype) == "float64"
        assert bool((df["symbol"].str.len() == 6).all()), "symbol 未零填充到 6 位"
        assert df["symbol"].is_unique, "去重没生效"

    def test_real_data_equals_legacy_logic(self, engine_df):
        """拿真实引擎的原始记录, 新旧归一化必须逐位一致。

        这条比合成样例更强: 它覆盖真实的缺失字段组合、真实的 int/float 混用、
        真实的停牌/退市缺口。
        """
        import pandas as pd
        from stock_sdk import rd
        records = []
        for pfx in E.PREFIXES:
            records.extend(list(rd.vals("日k", pfx, "20260922")))
        if not records:
            pytest.skip("引擎未返回记录")
        old = _legacy_normalize(records)
        new, _ = BI.normalize(pd.DataFrame(records), "stockdb_sdk", min_rows_per_day=0)
        # 断言比对**非平凡**: 行数够多才算真比过(否则"两边都是空表"也会通过)
        assert len(new) == len(old) > 1000, f"比对样本太小: old={len(old)} new={len(new)}"
        assert_frame_equal(new, old, check_exact=True, check_dtype=True)
        # 再确认"确有差异可被发现": 故意改一个值, 断言比对会失败
        tampered = old.copy()
        tampered.loc[0, "close"] = float(tampered.loc[0, "close"]) + 1.0
        with pytest.raises(AssertionError):
            assert_frame_equal(new, tampered, check_exact=True)


class TestResidualGuardStillFires:
    """残截面闸门必须仍在(重构最容易弄丢的判据)。"""

    def test_below_threshold_raises(self):
        with pytest.raises(ValueError, match="残截面"):
            BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=1000)

    def test_engine_error_message_kept(self):
        """`fetch_day` 的原有措辞必须保留 —— 既有测试与告警文本依赖它。"""
        import inspect
        src = inspect.getsource(E.fetch_day)
        assert "残截面会静默污染下游, 拒绝写入" in src


class TestSourceRegistryIsExplicit:
    """**未登记的源必须拒绝** —— 否则未验证的源能悄悄走完写入链路。"""

    def test_unknown_source_raises_with_guidance(self):
        with pytest.raises(ValueError) as ei:
            BI.normalize(_SAMPLE, "some_random_source")
        msg = str(ei.value)
        assert "未登记" in msg and "stockdb_sdk" in msg
        assert "复权口径" in msg, "错误信息要告诉人该怎么登记, 而不只是拒绝"

    def test_known_sources_present(self):
        assert set(BI.known_sources()) == {"stockdb_sdk", "akshare"}

    def test_akshare_volume_multiplier_is_declared(self):
        """akshare 的成交量单位是**手** ⇒ 规格里必须显式 ×100。

        实测依据: 引擎/akshare 的 volume 比值 mean = **100.0001**(见
        docs/stockdb-source-status.md)。若这里被改成 1.0, 落库的量会**少 100 倍**
        且**没有任何报错** —— 正是"数据看起来正常但差 100 倍"那类潜伏错误。
        """
        assert BI.SOURCE_SPECS["akshare"]["volume_mult"] == 100.0
        assert BI.SOURCE_SPECS["stockdb_sdk"]["volume_mult"] == 1.0

    def test_akshare_normalize_applies_multiplier(self):
        raw = [{"股票代码": "000001", "日期": "2026-09-22", "开盘": 10.0, "最高": 11.0,
                "最低": 9.0, "收盘": 10.5, "成交量": 1000, "成交额": 5000,
                "涨跌幅": 1.0, "换手率": 0.5}]
        out, meta = BI.normalize(raw, "akshare", min_rows_per_day=0)
        assert float(out.iloc[0]["volume"]) == 100_000.0, "手->股 的 ×100 没生效"
        assert float(out.iloc[0]["amount"]) == 5000.0, "成交额不应被换算(1:1)"
        assert float(out.iloc[0]["change_pct"]) == 1.0
        assert meta["source"] == "akshare"

    def test_empty_input_is_not_success(self):
        """空截面不得当成成功。"""
        with pytest.raises(ValueError, match="空表"):
            BI.normalize([], "stockdb_sdk", min_rows_per_day=0)


class TestGapAccounting:
    """`unfillable_gap`:**append 原理上写不进去的天必须显式报出**。"""

    def test_days_above_watermark_are_appended(self):
        g = BI.account_gap(["2026-09-23", "2026-09-24"], "2026-09-22")
        assert g["appended_days"] == ["2026-09-23", "2026-09-24"]
        assert g["unfillable_gap"] == []

    def test_days_at_or_below_watermark_are_unfillable(self):
        """**核心**: 水位之前的天不能用 append 回填, 必须显式分类。

        否则会出现「主源漏了一天 -> 备源取到 -> append 静默跳过 ->
        调用方以为补上了, 实际什么都没写」。
        """
        g = BI.account_gap(["2026-09-18", "2026-09-21", "2026-09-23"], "2026-09-22")
        assert g["appended_days"] == ["2026-09-23"]
        assert g["unfillable_gap"] == ["2026-09-18", "2026-09-21"]
        assert "运维口径" in g["action"]
        assert "不得" in g["action"]

    def test_watermark_equals_day_is_unfillable(self):
        """边界: `<= max` 被跳过(append 的判据是严格大于)。"""
        g = BI.account_gap(["2026-09-22"], "2026-09-22")
        assert g["unfillable_gap"] == ["2026-09-22"]

    def test_unknown_watermark_means_all_writable(self):
        g = BI.account_gap(["2026-09-22"], None)
        assert g["appended_days"] == ["2026-09-22"] and g["unfillable_gap"] == []


class TestGatesArePreserved:
    """两道闸门必须仍在写入路径上(本仓铁律)。"""

    def test_quality_gate_is_in_the_append_path(self):
        """结构哨兵在 `h5i_sync.append_daily_bars` 里 —— 必须仍在。

        `write_bars` **刻意不重复实现**它, 以免两处判据漂移。
        """
        import inspect
        import h5i_sync as H
        src = inspect.getsource(H.append_daily_bars)
        assert "data_quality_guard" in src and "gate_decision" in src, \
            "结构哨兵不见了 —— 脏行会直接入库"

    def test_monotonic_append_is_in_the_append_path(self):
        import inspect
        import h5i_sync as H
        src = inspect.getsource(H.append_daily_bars)
        assert 'work["_d"] <= m' in src and 'work["_d"] > m' in src, \
            "单调追加判据不见了 —— 会回改历史"

    def test_write_bars_does_not_reimplement_the_gate(self):
        """`write_bars` 不得自己再写一遍结构校验(避免两处判据漂移)。"""
        import inspect
        src = inspect.getsource(BI.write_bars)
        assert "validate_daily_bars" not in src, \
            "write_bars 重复实现了结构校验 —— 两处判据会漂移"


class TestWriteBarsContract:
    """`write_bars` 的返回值契约(路由层要依赖它)。"""

    def test_dry_run_reports_no_write(self):
        out = BI.write_bars(_SAMPLE, "stockdb_sdk", dry_run=True,
                            min_rows_per_day=0, h5i_max="2026-09-22")
        assert out["ok"] is True and out["dry_run"] is True
        assert out["appended"] == 0
        # `.venv314` 没有 h5i_db, 故 `h5i_max` 用注入值 —— 否则这条断言在测试环境
        # 只能靠跳过, 而"水位被正确读到"恰恰是缺口核算的前提。
        assert out["h5i_max_before"] == "2026-09-22"
        assert out["days"], "预演也要报出打算写哪些天"

    def test_gap_reported_from_injected_watermark(self):
        """水位之前的天必须进 `unfillable_gap`(而不是被静默跳过)。"""
        out = BI.write_bars(_SAMPLE, "stockdb_sdk", dry_run=True,
                            min_rows_per_day=0, h5i_max="2026-09-23")
        # 样例只有 2026-09-22 一天, 水位 09-23 => 不可回填
        assert out["unfillable_gap"] == ["2026-09-22"]
        assert out["appended_days"] == []
        assert "运维口径" in out.get("action", "")

    def test_append_is_injected_not_called_for_real(self):
        """用注入的 `_append` 验证写入分支, **不真碰 h5i**。

        这样"归一化后的 df 确实被交给 append"这件事在无 h5i 的环境里也能锁住。
        """
        seen = {}

        def fake_append(norm):
            seen["cols"] = list(norm.columns)
            seen["rows"] = len(norm)
            return {"ok": True, "appended": len(norm), "skipped_rows": 0}

        out = BI.write_bars(_SAMPLE, "stockdb_sdk", dry_run=False,
                            min_rows_per_day=0, h5i_max="2026-09-21",
                            _append=fake_append)
        assert out["ok"] is True and out["appended"] == seen["rows"] > 0
        assert seen["cols"] == _H5I_COLS, "交给 append 的列顺序不对"

    def test_unknown_source_fails_loudly_not_silently(self):
        out = BI.write_bars(_SAMPLE, "nope", dry_run=True)
        assert out["ok"] is False and out["error"]
        assert "未登记" in out["error"]

    def test_gap_is_computed_even_if_write_fails(self):
        """缺口信息**先于**写入算好 —— 写入失败时也不该丢。"""
        out = BI.write_bars(_SAMPLE, "stockdb_sdk", dry_run=True, min_rows_per_day=0)
        assert "unfillable_gap" in out and "appended_days" in out


class TestNormalizeIsIdempotent:
    """`normalize` 必须**既能吃原始记录, 也能吃已归一化的表**。

    ## 为什么这是必须的(实测踩到的真 bug)

    各源适配器(`engine_bars_sync.fetch_day`)**先把自己的原始记录归一化**再交出来,
    于是 `write_bars(fetch_day(...), "stockdb_sdk")` 拿到的是**已归一化**的
    `symbol/date/open/...` 表。而 `field_map` 期望源列叫 `code` —— 已归一化的表里
    **没有** `code`, 于是:

        code 列补 None -> `.astype(str)` 得到字面量 "None" -> 选出的 symbol 是 "None"
        -> drop_duplicates 只剩 1 行 -> 归一化出 **0 行**(date 不在映射里)
        -> 被残截面闸门拒为 "仅 0 行"

    实测: `write_bars(fetch_day('20260922'), 'stockdb_sdk')` 报
    **`归一化后仅 0 行 (< 阈值 1000)`**, 而 `fetch_day` 明明返回 **5481** 行。

    **只有把这条测出来, 才说明"统一写入路径"真的可用** —— 否则
    `write_bars` 对自家适配器的产物直接不工作, 而单测若只用原始记录就永远发现不了。
    """

    def test_normalized_frame_passes_through(self):
        raw = _SAMPLE
        once, _ = BI.normalize(raw, "stockdb_sdk", min_rows_per_day=0)
        twice, meta = BI.normalize(once, "stockdb_sdk", min_rows_per_day=0)
        assert len(twice) == len(once) > 0, \
            f"已归一化的表再归一化后变成 {len(twice)} 行 —— 幂等性缺失"
        assert meta.get("input") == "already_normalized"
        assert_frame_equal(twice, once, check_exact=True, check_dtype=True)

    def test_real_engine_df_is_accepted_by_write_bars(self):
        """端到端: 适配器产物 -> `write_bars` 必须真的可用。

        这条若失败, 说明"所有源经同一条写入路径"在**自家源上就已经不成立**。
        """
        try:
            df, _meta = E.fetch_day("20260922")
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"引擎不可用: {type(e).__name__}: {e}")
        out = BI.write_bars(df, "stockdb_sdk", dry_run=True)
        assert out["ok"] is True, f"适配器产物被 write_bars 拒了: {out.get('error')}"
        assert out["meta"]["rows"] == len(df) > 1000
        assert out["days"] == ["2026-09-22"], out["days"]

    def test_raw_records_still_work(self):
        """幂等入口不得破坏**原始记录**路径。"""
        out, meta = BI.normalize(_SAMPLE, "stockdb_sdk", min_rows_per_day=0)
        assert len(out) > 0
        assert meta.get("input") != "already_normalized", "原始记录被误判为已归一化"


class TestRoutingContract:
    """路由层契约: 调用方只该分支 `status`, 且缺口**不得**被当成功。"""

    @staticmethod
    def _fetch_ok(_date):
        return [{"code": "000001", "date": "20260922", "open": 10.0, "high": 11.0,
                 "low": 9.0, "close": 10.5, "volume": 100, "amount": 1000,
                 "pct_chg": 0.1, "turnover": 0.2}] * 3

    def test_normal_path_is_appended(self, monkeypatch):
        import bars_ingest as _bi
        monkeypatch.setattr(_bi, "write_bars",
                            lambda df, src, **kw: {"ok": True, "appended": 3,
                                                   "appended_days": ["2026-09-22"],
                                                   "unfillable_gap": []})
        r = BI.fetch_and_ingest("2026-09-22", "stockdb_sdk", self._fetch_ok)
        assert r["status"] == "appended" and r["appended"] == 3
        assert r["unfillable_gap"] == []

    def test_unfillable_gap_is_its_own_status(self, monkeypatch):
        """**核心**: 水位之前的缺口必须是 `unfillable_gap`, 而不是 `appended`。

        用户给的契约原文: 「水位之前的缺口, append 无法回填 ->
        return {status: 'unfillable_gap', action: '需走 h5i_ingest/h5i_rebuild 运维口径'}」。
        """
        import bars_ingest as _bi
        monkeypatch.setattr(_bi, "write_bars",
                            lambda df, src, **kw: {"ok": True, "appended": 0,
                                                   "appended_days": [],
                                                   "unfillable_gap": ["2026-09-22"]})
        r = BI.fetch_and_ingest("2026-09-22", "stockdb_sdk", self._fetch_ok)
        assert r["status"] == "unfillable_gap", r
        assert "运维口径" in r.get("action", "")
        assert r["unfillable_gap"] == ["2026-09-22"]

    def test_partial_write_still_flags_the_gap(self, monkeypatch):
        """部分可写时: 写进去一部分, **同时**报出剩余缺口 —— 两者都要说清。"""
        import bars_ingest as _bi
        monkeypatch.setattr(_bi, "write_bars",
                            lambda df, src, **kw: {"ok": True, "appended": 5,
                                                   "appended_days": ["2026-09-23"],
                                                   "unfillable_gap": ["2026-09-22"]})
        r = BI.fetch_and_ingest("2026-09-23", "stockdb_sdk", self._fetch_ok)
        assert r["status"] == "appended" and r["appended"] == 5
        assert r["unfillable_gap"] == ["2026-09-22"]
        assert "缺口" in r.get("action", "")

    def test_fetch_failure_is_failed_not_silent(self):
        def boom(_d):
            raise RuntimeError("源挂了")
        r = BI.fetch_and_ingest("2026-09-22", "stockdb_sdk", boom)
        assert r["status"] == "failed" and "源挂了" in r["error"]

    def test_write_failure_is_failed(self, monkeypatch):
        import bars_ingest as _bi
        monkeypatch.setattr(_bi, "write_bars",
                            lambda df, src, **kw: {"ok": False, "error": "闸门拒绝",
                                                   "appended": 0, "appended_days": [],
                                                   "unfillable_gap": []})
        r = BI.fetch_and_ingest("2026-09-22", "stockdb_sdk", self._fetch_ok)
        assert r["status"] == "failed" and "闸门拒绝" in r["error"]

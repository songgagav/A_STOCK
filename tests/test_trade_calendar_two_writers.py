# -*- coding: utf-8 -*-
"""交易日历"两个写入方"的回归测试 (2026-09-21 实事故).

事故经过
--------
`data/trade_calendar.json` 有**两个写入方、两套契约**, 而两者都曾**整体覆写**它:

  · `scripts/export_trade_calendar.py` 写 `days` = **数据同源**(只有"已经有数据的日子")
    并附 `trading_days`; `src/trading_calendar.py` 的 `refresh()` 写 `days` =
    **官方 AKShare 日历**(含全年未来日期)并附 `updated`。
  · 于是互相抹键。最致命的方向是:**导出脚本把 `days` 换成"只有有数据的日子"后,
    `is_trading_day(今天)` 恒为 False** —— 因为今天的日期必然晚于"最后有数据的日子",
    而该函数的实现就是 `return norm in cal`。
  · 后果极隐蔽: 守护把**交易日当节假日** —— 不跑盘前健康检查、08:30 不启动盘中引擎、
    收盘窗口只跑 `--maint`。表面上只是"今天没事做"。

本测试锁定四条契约, 全部使用**临时文件**, 绝不触碰生产日历:
  1. `trading_calendar._save_cache` 保留未知键(`trading_days` 等), 不抹掉另一套契约;
  2. 它把由 days 派生的 `n/first/last` **同步重算**, 不留下自相矛盾;
  3. `export_trade_calendar.main` 对 `days` **取并集、绝不缩小**, 并保留 `updated`;
  4. 端到端: 先有"含未来日期"的 days, 再跑一次导出脚本, **未来日期仍在** ——
     也就是 `is_trading_day(未来交易日)` 依然为 True。
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))
sys.path.insert(0, os.path.join(_REPO, "scripts"))

import trading_calendar as TC  # noqa: E402


@pytest.fixture
def cal(tmp_path, monkeypatch):
    """把 trading_calendar 的日历文件指向临时路径(绝不碰生产 data/)。"""
    fp = tmp_path / "trade_calendar.json"
    monkeypatch.setattr(TC, "CAL_FILE", str(fp), raising=True)
    monkeypatch.setattr(TC, "DATA_DIR", str(tmp_path), raising=True)
    return fp


def _write(fp, payload):
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 1) _save_cache 不抹掉另一套契约的键
# ---------------------------------------------------------------------------
class TestSaveCachePreservesForeignKeys:
    def test_keeps_trading_days_from_exporter(self, cal):
        """refresh() 写官方日历后, 导出脚本写的 `trading_days` 必须还在。

        否则外部 veighna_sim 的日历回退链会失效 —— 这是事故的**反方向**。
        """
        _write(cal, {"days": ["20260918"],
                     "trading_days": ["2026-09-17", "2026-09-18"],
                     "generated_at": "2026-09-21T00:39:14",
                     "n": 1, "first": "20260918", "last": "20260918"})
        TC._save_cache({"20260921", "20260922"})
        got = _read(cal)
        assert got["trading_days"] == ["2026-09-17", "2026-09-18"]
        assert got["generated_at"] == "2026-09-21T00:39:14"
        assert got["days"] == ["20260921", "20260922"]
        assert "updated" in got, "refresh 必须写 updated, 否则缓存年龄判据失效"

    def test_recomputes_derived_fields_instead_of_leaving_contradiction(self, cal):
        """`n/first/last` 由 days 派生; 保留它们时**必须同步重算**。

        留成"n=8707、last=2026-09-18 而 days 实际到 2026-12-31"比丢掉更难排查。
        """
        _write(cal, {"days": ["20260918"], "n": 8707,
                     "first": "19910114", "last": "20260918"})
        TC._save_cache({"20260105", "20261231"})
        got = _read(cal)
        assert got["n"] == 2 and got["first"] == "20260105" and got["last"] == "20261231"

    def test_works_when_file_absent(self, cal):
        assert not cal.exists()
        TC._save_cache({"20260921"})
        got = _read(cal)
        assert got["days"] == ["20260921"] and got["source"]


# ---------------------------------------------------------------------------
# 2) 导出脚本: days 取并集, 绝不缩小
# ---------------------------------------------------------------------------
class TestExporterNeverShrinks:
    def _run_exporter(self, monkeypatch, fp, h5i_days):
        import export_trade_calendar as EX
        importlib.reload(EX)
        # 契约提醒: 真实 `_trading_days()` 返回的是 **'YYYY-MM-DD'**(来自
        # `H5iBarStore().trading_days()`), 脚本再对 `days` 键去横线。
        # 初版测试替身喂了 'YYYYMMDD', 于是 `trading_days` 断言以一个看似
        # "实现有问题"的面目失败 —— 实为替身不符契约。故此处**显式断言**。
        assert all(len(d) == 10 and d[4] == "-" for d in h5i_days), \
            f"_trading_days 替身必须返回 'YYYY-MM-DD', 收到: {h5i_days}"
        monkeypatch.setattr(EX, "OUT_FP", str(fp), raising=True)
        monkeypatch.setattr(EX, "_trading_days",
                            lambda: (sorted(h5i_days), "test:h5i"), raising=True)
        return EX.main()

    def test_union_keeps_official_future_days(self, tmp_path, monkeypatch):
        """核心回归: 数据只到 09-18, 但 days 里已有官方未来日(09-21) —— 不能被砍掉。"""
        fp = tmp_path / "tc.json"
        _write(fp, {"days": ["20260918", "20260921", "20261231"],
                    "trading_days": ["2026-09-18"], "updated": "2026-09-21 00:52:44"})
        rc = self._run_exporter(monkeypatch, fp, ["2026-09-17", "2026-09-18"])
        assert rc == 0
        got = _read(fp)
        assert "20260921" in got["days"], "官方未来交易日被导出脚本砍掉了(事故重现)"
        assert got["days"][-1] == "20261231"
        assert got["trading_days"] == ["2026-09-17", "2026-09-18"], \
            "trading_days 必须保持数据同源语义"
        assert got["updated"] == "2026-09-21 00:52:44", "updated 必须保留"

    def test_days_is_superset_of_previous(self, tmp_path, monkeypatch):
        fp = tmp_path / "tc.json"
        old = ["20260101", "20260102"]
        _write(fp, {"days": old})
        self._run_exporter(monkeypatch, fp, ["2026-01-03"])
        assert set(old) <= set(_read(fp)["days"])

    def test_creates_file_when_absent(self, tmp_path, monkeypatch):
        fp = tmp_path / "new.json"
        assert self._run_exporter(monkeypatch, fp,
                                  ["2026-01-05", "2026-01-06"]) == 0
        got = _read(fp)
        assert got["days"] == ["20260105", "20260106"]
        assert got["trading_days"] == ["2026-01-05", "2026-01-06"]

    def test_refuses_empty_and_keeps_file(self, tmp_path, monkeypatch):
        fp = tmp_path / "tc.json"
        _write(fp, {"days": ["20260101"]})
        assert self._run_exporter(monkeypatch, fp, []) == 2
        assert _read(fp)["days"] == ["20260101"]


# ---------------------------------------------------------------------------
# 3) is_trading_day 的判据（事故的直接表现）
# ---------------------------------------------------------------------------
class TestIsTradingDay:
    def test_future_trading_day_is_true_when_in_official_calendar(self, cal):
        """**事故的直接断言**: 今天/未来交易日必须在集合里, 否则守护当成节假日。"""
        d = dt.date(2026, 9, 21)          # 周一
        assert d.weekday() == 0
        _write(cal, {"days": ["20260918", "20260921"]})
        assert TC.is_trading_day(d) is True

    def test_data_derived_calendar_makes_future_days_false(self, cal):
        """**记录病因**: days 若只到最后一个有数据的日子, 今天必然被判非交易日。

        这条用例把事故的机制固化下来 —— 它是"导出脚本必须取并集"的原因本身。
        """
        d = dt.date(2026, 9, 21)
        _write(cal, {"days": ["20260917", "20260918"]})   # 只有有数据的日子
        assert TC.is_trading_day(d) is False, \
            "若这条变成 True, 说明 is_trading_day 的语义变了, 请复核本文件的前提"

    def test_weekend_is_false_even_if_present_in_calendar(self, cal):
        """周末兜底: 即便集合里有该日期(实测 h5i 含 47 个 1991-93 年的周末), 也必须 False。"""
        d = dt.date(2026, 9, 19)          # 周六
        assert d.weekday() == 5
        _write(cal, {"days": ["20260919"]})
        assert TC.is_trading_day(d) is False

    def test_holiday_absent_from_official_set_is_false(self, cal):
        """节假日(如中秋 09-25)不在官方集合里 -> False, 这正是需要官方日历的原因。"""
        d = dt.date(2026, 9, 25)          # 周五, 但为中秋休市
        assert d.weekday() == 4
        _write(cal, {"days": ["20260924", "20260928"]})
        assert TC.is_trading_day(d) is False


# ---------------------------------------------------------------------------
# 4) 两个写入方交替运行（端到端）
# ---------------------------------------------------------------------------
class TestTwoWritersInterleave:
    def test_exporter_then_refresh_then_exporter_keeps_both_contracts(self, tmp_path, monkeypatch):
        fp = tmp_path / "tc.json"
        # 官方日历先落盘(含未来)
        _write(fp, {"days": ["20260918", "20260921"], "updated": "2026-09-21 00:52:44"})
        monkeypatch.setattr(TC, "CAL_FILE", str(fp), raising=True)
        # 导出脚本跑一次(数据只到 09-18)
        import export_trade_calendar as EX
        importlib.reload(EX)
        monkeypatch.setattr(EX, "OUT_FP", str(fp), raising=True)
        monkeypatch.setattr(EX, "_trading_days",
                            lambda: (["20260918"], "test:h5i"), raising=True)
        assert EX.main() == 0
        # 再用官方集合"刷新"(模拟 refresh 写入)
        TC._save_cache({"20260918", "20260921", "20260922"})
        got = _read(fp)
        assert "20260921" in got["days"] and "20260922" in got["days"]
        assert "trading_days" in got, "导出脚本的键必须活过 refresh"
        assert TC.is_trading_day(dt.date(2026, 9, 22)) is True

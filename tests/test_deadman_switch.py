# -*- coding: utf-8 -*-
"""deadman_switch (P0 清单第 6 项) 回归测试。

本模块要锁住的核心语义只有一条: **"没有 tick" 就是失败证据**, 且
**"账本为空" 不等于 "健康"** —— 后者是这类监控最容易被误读成 OK 的状态。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import deadman_switch as D  # noqa: E402

T0 = datetime(2026, 9, 22, 10, 0, 0)          # 周二盘中


def _write_ledger(tmp_path, rows):
    p = tmp_path / "ticks.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


def _reg(**over):
    r = {"alpha": {"period_s": 60.0, "desc": "test"}}
    r.update(over)
    return r


class TestTick:
    def test_tick_then_read_back(self, tmp_path):
        led = str(tmp_path / "t.jsonl")
        r = D.tick("alpha", note="n1", ledger=led, now=T0)
        assert r["ok"] is True
        recs = D.last_ticks(led)
        assert recs["alpha"]["note"] == "n1"
        assert recs["alpha"]["at"] == T0.strftime("%Y-%m-%d %H:%M:%S")

    def test_last_tick_wins(self, tmp_path):
        led = str(tmp_path / "t.jsonl")
        D.tick("alpha", note="old", ledger=led, now=T0)
        D.tick("alpha", note="new", ledger=led, now=T0 + timedelta(seconds=5))
        assert D.last_ticks(led)["alpha"]["note"] == "new"

    def test_multiple_components_independent(self, tmp_path):
        led = str(tmp_path / "t.jsonl")
        D.tick("alpha", ledger=led, now=T0)
        D.tick("beta", ledger=led, now=T0)
        assert set(D.last_ticks(led)) == {"alpha", "beta"}

    def test_tick_never_raises_on_bad_path(self, tmp_path):
        """留痕失败不得抛(本仓纪律: 留痕不得拖垮业务路径)。"""
        bad = str(tmp_path / "no" / "such" / "dir" / "t.jsonl")
        os.makedirs(os.path.dirname(os.path.dirname(bad)), exist_ok=True)
        r = D.tick("alpha", ledger="\0bad\0path")
        assert isinstance(r, dict) and "ok" in r

    def test_beat_returns_bool_and_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(D, "ledger_path", lambda p=None: str(tmp_path / "t.jsonl"))
        assert D.beat("alpha", "note") is True

    def test_beat_false_when_write_fails(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(D, "tick", boom)
        assert D.beat("alpha") is False


class TestLastTicks:
    def test_missing_file_is_empty(self, tmp_path):
        assert D.last_ticks(str(tmp_path / "nope.jsonl")) == {}

    def test_corrupt_lines_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"component":"alpha","at":"2026-09-22 10:00:00"}\n'
                     'not json at all\n'
                     '\n'
                     '{"no_component":1}\n', encoding="utf-8")
        recs = D.last_ticks(str(p))
        assert list(recs) == ["alpha"]

    def test_non_dict_json_skipped(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('[1,2,3]\n{"component":"beta"}\n', encoding="utf-8")
        assert list(D.last_ticks(str(p))) == ["beta"]


class TestVerdict:
    def test_empty_ledger_is_unknown_not_ok(self, tmp_path):
        """**核心断言**: 账本为空 => UNKNOWN。死手开关从未生效不是"健康",
        把它读成 OK 正是这类监控最危险的失效模式。"""
        v = D.verdict(T0, registry=_reg(), ledger=str(tmp_path / "none.jsonl"))
        assert v["level"] == D.UNKNOWN
        assert v["items"][0]["status"] == D.UNKNOWN
        assert any("从未生效" in r for r in v["reasons"])

    def test_empty_ledger_level_is_not_overdue_either(self, tmp_path):
        """空账本不能报 OVERDUE: 那会读成"有组件失联", 而真相是"还没有组件上线"。
        首次实现用 `overdue if led else unknown` 选桶, 而 `led` 是 dict, 只要账本
        **有任何**条目就为真 —— 于是 level 恒为 OVERDUE、UNKNOWN 永不出现。"""
        v = D.verdict(T0, registry=_reg(), ledger=str(tmp_path / "none.jsonl"))
        assert v["level"] != D.OVERDUE
        assert v["overdue"] == []

    def test_level_unknown_when_all_entries_unticked_and_ledger_has_others(self, tmp_path):
        """账本非空(别的组件在 tick) => 本组件从没 tick 是**真失联** => OVERDUE。"""
        led = _write_ledger(tmp_path, [{"component": "other", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0, registry=_reg(alpha={"period_s": 60.0}), ledger=led)
        assert v["level"] == D.OVERDUE
        assert v["unknown"] == []

    def test_fresh_tick_is_ok(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0 + timedelta(seconds=30), registry=_reg(), ledger=led)
        assert v["level"] == D.OK and v["items"][0]["status"] == D.OK

    def test_exactly_at_limit_is_ok(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0 + timedelta(seconds=180), registry=_reg(), ledger=led)
        assert v["items"][0]["status"] == D.OK

    def test_one_second_over_limit_is_overdue(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0 + timedelta(seconds=181), registry=_reg(), ledger=led)
        assert v["level"] == D.OVERDUE
        assert v["items"][0]["status"] == D.OVERDUE

    def test_timeout_mult_is_three_periods(self):
        assert D.TIMEOUT_MULT == 3.0

    def test_component_never_ticked_but_ledger_nonempty_is_overdue(self, tmp_path):
        """账本里有别的组件 => 死手开关**已生效**, 那么"本组件从没 tick 过"
        就是真失联, 不是"还没上线"。"""
        led = _write_ledger(tmp_path, [{"component": "other", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0, registry=_reg(alpha={"period_s": 60.0}), ledger=led)
        assert v["level"] == D.OVERDUE
        assert v["items"][0]["status"] == D.OVERDUE

    def test_iso_timestamp_parsed(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha",
                                        "ts": T0.isoformat(timespec="seconds")}])
        v = D.verdict(T0 + timedelta(seconds=10), registry=_reg(), ledger=led)
        assert v["items"][0]["status"] == D.OK

    def test_unparsable_timestamp_is_unknown(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": "not-a-time"}])
        v = D.verdict(T0, registry=_reg(), ledger=led)
        assert v["items"][0]["status"] == D.UNKNOWN

    def test_trading_hours_only_is_silent_after_close(self, tmp_path):
        """收盘后本就不该有 tick —— 不算失败, 但如实列出判定依据。"""
        led = _write_ledger(tmp_path, [{"component": "eng", "at": T0.strftime(D._TS_FMT)}])
        after = datetime(2026, 9, 22, 20, 0, 0)
        v = D.verdict(after, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["level"] == D.OK
        assert v["items"][0]["status"] == D.SILENT
        assert "非交易时段" in v["items"][0]["detail"]

    def test_trading_hours_only_is_enforced_during_hours(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "eng", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0 + timedelta(seconds=3600),
                      registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["level"] == D.OVERDUE

    def test_weekend_is_silent(self, tmp_path):
        sat = datetime(2026, 9, 26, 10, 0, 0)
        led = _write_ledger(tmp_path, [{"component": "eng", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(sat, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["items"][0]["status"] == D.SILENT

    def test_weekend_silent_even_with_empty_ledger(self, tmp_path):
        sat = datetime(2026, 9, 26, 10, 0, 0)
        v = D.verdict(sat, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=str(tmp_path / "none.jsonl"))
        assert v["items"][0]["status"] == D.SILENT

    def test_only_filter(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0, registry=_reg(beta={"period_s": 60.0}), ledger=led, only=["alpha"])
        assert [it["component"] for it in v["items"]] == ["alpha"]

    def test_limit_s_is_three_times_period(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0, registry={"alpha": {"period_s": 45.0}}, ledger=led)
        assert v["items"][0]["limit_s"] == 135.0

    def test_zero_or_missing_period_is_overdue_immediately(self, tmp_path):
        """周期缺省为 0 => 阈值 0 => 任何非零 age 都超时。
        这是**保守失败**(宁可报), 与该模块的立场一致。"""
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        v = D.verdict(T0 + timedelta(seconds=1), registry={"alpha": {}}, ledger=led)
        assert v["items"][0]["status"] == D.OVERDUE

    def test_result_is_json_serialisable(self, tmp_path):
        led = _write_ledger(tmp_path, [{"component": "alpha", "at": T0.strftime(D._TS_FMT)}])
        json.dumps(D.verdict(T0, registry=_reg(), ledger=led), ensure_ascii=False)

    def test_intraday_component_absent_today_is_overdue_not_silent(self, tmp_path):
        """**盘后回看时, "今天盘中一次都没出现" 不能被读成 "收盘后应静默"**。

        2026-09-22 实测: `realtime_engine` 实际 12:29:35 就停了(下午整段没跑),
        而盘后跑判定时它显示 SILENT_EXPECTED「非交易时段, 本项不该有 tick」——
        判据没错, 但它答的是"**现在**该不该有", 不是"**今天该跑的时候跑了吗**"。
        只看"现在是不是交易时段"的判定, 在盘后回看时永远看不出盘中停摆。

        此处: 今天是交易日, 盘后问, 最后一条 tick 是**昨天**的
        => 今天盘中从未出现 => OVERDUE。
        """
        led = _write_ledger(tmp_path, [
            {"component": "eng", "at": (T0 - timedelta(days=1)).strftime(D._TS_FMT)}])
        after = T0.replace(hour=20, minute=0)
        v = D.verdict(after, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["items"][0]["status"] == D.OVERDUE
        assert "从未出现" in v["items"][0]["detail"]

    def test_intraday_component_ticked_today_then_stopped_is_not_flagged(self, tmp_path):
        """**刻意不覆盖的情况**(留档, 免得后人以为它管这个)。

        引擎今天盘中出现过、但**早于收盘就停了**(今天正是这种: 最后 tick 11:31,
        实际 12:29 停)。这一档**不报 OVERDUE** —— 因为盘后无法区分"正常收盘静默"
        与"半路死掉", 想靠时间点比较糊过去, 结果会是**每天收盘都误报**。
        真正要覆盖它得靠"盘中周期心跳 + 收盘时的当日覆盖度断言", 属另一件事。
        """
        led = _write_ledger(tmp_path, [{"component": "eng", "at": T0.strftime(D._TS_FMT)}])
        after = T0.replace(hour=20, minute=0)
        v = D.verdict(after, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["items"][0]["status"] == D.SILENT

    def test_weekend_is_still_silent_after_the_fix(self, tmp_path):
        """**回归护栏**: 上面那个修复的首版把每个周末都报成了 OVERDUE。

        周末的最后一条 tick 天然早于"今天 09:30", 不加"今天本来就是交易日"这道
        判断就会满屏误报。此例与 `test_weekend_is_silent` 同义, 但把**为什么**
        写在这里 —— 它是被那次改动打破后补上的。
        """
        sat = datetime(2026, 9, 26, 10, 0, 0)
        led = _write_ledger(tmp_path, [
            {"component": "eng", "at": datetime(2026, 9, 25, 14, 0).strftime(D._TS_FMT)}])
        v = D.verdict(sat, registry={"eng": {"period_s": 60.0, "trading_hours_only": True}},
                      ledger=led)
        assert v["items"][0]["status"] == D.SILENT


class TestRegistry:
    def test_defaults_present(self):
        reg = D.load_registry()
        assert {"run_daily", "daemon", "realtime_engine", "obs_stack"} <= set(reg)

    def test_every_entry_has_period_and_desc(self):
        for name, spec in D.DEFAULT_REGISTRY.items():
            assert float(spec["period_s"]) > 0, name
            assert spec.get("desc"), name

    def test_file_overrides_defaults(self, tmp_path):
        p = tmp_path / "reg.json"
        p.write_text(json.dumps({"custom": {"period_s": 10.0}}), encoding="utf-8")
        assert list(D.load_registry(str(p))) == ["custom"]

    def test_corrupt_file_falls_back_to_defaults_not_empty(self, tmp_path):
        """**损坏时必须退回缺省而不是空表**: 空表会让 verdict 报"一切正常",
        把"注册表读坏了"伪装成"系统健康"。"""
        p = tmp_path / "reg.json"
        p.write_text("{not json", encoding="utf-8")
        assert "daemon" in D.load_registry(str(p))

    def test_empty_dict_falls_back(self, tmp_path):
        p = tmp_path / "reg.json"
        p.write_text("{}", encoding="utf-8")
        assert "daemon" in D.load_registry(str(p))

    def test_missing_file_falls_back(self, tmp_path):
        assert "daemon" in D.load_registry(str(tmp_path / "nope.json"))


class TestLedgerPath:
    def test_default_under_data(self):
        assert D.ledger_path().replace("\\", "/").endswith("data/deadman_ticks.jsonl")

    def test_override(self):
        assert D.ledger_path("x.jsonl") == "x.jsonl"


class TestCli:
    def test_status_exit_code_zero_when_ok(self, tmp_path, monkeypatch, capsys):
        led = str(tmp_path / "t.jsonl")
        D.tick("daemon", ledger=led, now=datetime.now())
        monkeypatch.setattr(D, "ledger_path", lambda p=None: led)
        monkeypatch.setattr(D, "load_registry", lambda p=None: {"daemon": {"period_s": 3600.0}})
        assert D._main(["--status"]) == 0

    def test_status_exit_code_one_when_overdue(self, tmp_path, monkeypatch):
        led = _write_ledger(tmp_path, [{"component": "daemon",
                                        "at": (datetime.now() - timedelta(days=5)).strftime(D._TS_FMT)}])
        monkeypatch.setattr(D, "ledger_path", lambda p=None: led)
        monkeypatch.setattr(D, "load_registry", lambda p=None: {"daemon": {"period_s": 60.0}})
        assert D._main(["--status"]) == 1

    def test_tick_via_cli(self, tmp_path, monkeypatch):
        led = str(tmp_path / "t.jsonl")
        monkeypatch.setattr(D, "ledger_path", lambda p=None: led)
        assert D._main(["--tick", "daemon", "--note", "hi"]) == 0
        assert D.last_ticks(led)["daemon"]["note"] == "hi"

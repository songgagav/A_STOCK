# -*- coding: utf-8 -*-
"""数据源健康门禁的回归测试 (2026-09-22 批次)。

锁住的核心:
  1. 五种失效模式的**归因**是否准确（尤其"厂商未发布"与"同步假成功"必须分开,
     否则会天天误报）;
  2. **连续失败达阈值才 HALT, 且必须是同一个原因** —— 交替原因该修探测, 不该停数据;
  3. 门禁只拦摄入, **不停守护**;
  4. 判定异常**不阻断**（与本仓"一个 bug 不能让系统静默停手"一致）。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import datasource_gate as G  # noqa: E402


def _ledger(tmp_path, rows):
    p = tmp_path / "ds.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(p)


class TestClassifyEngine:
    def test_unreachable(self):
        r = G.classify_engine({"ok": False, "error": "TimeoutError: Connect timeout"})
        assert r["ok"] is False and r["kind"] == "unreachable"
        assert "Connect timeout" in r["detail"]

    def test_missing_probe_is_not_healthy(self):
        r = G.classify_engine(None)
        assert r["ok"] is False

    def test_freshness_undeterminable_is_not_healthy(self):
        """可连但判不出新鲜度 —— **不可判定不等于健康**。"""
        r = G.classify_engine({"ok": True, "day": "20260918", "freshness": {}})
        assert r["ok"] is False and r["kind"] == "freshness_undeterminable"

    def test_lag_within_grace_is_observed_only(self):
        """**关键**: 厂商盘后才发布, 落后 1 个交易日是**正常形态**, 不能拦。

        拦它会造成每天必然误报 —— 实测 2026-09-21/09-22 两天 engine_day 都停在
        2026-09-18, 而厂商确实尚未发布。
        """
        r = G.classify_engine({"ok": True, "freshness": {
            "ok": False, "engine_day": "20260918", "expected_day": "20260921",
            "lag_trading_days": 1}})
        assert r["ok"] is True and r["observed_only"] is True
        assert r["kind"] == "publisher_grace"

    def test_lag_over_grace_is_failure(self):
        r = G.classify_engine({"ok": True, "freshness": {
            "engine_day": "20260918", "expected_day": "20260922",
            "lag_trading_days": 2}})
        assert r["ok"] is False and r["kind"] == "engine_lag_over_grace"

    def test_lag_over_hard_threshold(self):
        """落后超过硬阈值 = 疑漏发/停摆（实测 P1-DATA-STALE 断供 8 个交易日）。"""
        r = G.classify_engine({"ok": True, "freshness": {
            "engine_day": "20260905", "expected_day": "20260918",
            "lag_trading_days": 8}})
        assert r["ok"] is False and r["kind"] == "engine_lag_halt"
        assert "漏发" in r["detail"] or "停摆" in r["detail"]

    def test_zero_lag_is_clean(self):
        r = G.classify_engine({"ok": True, "freshness": {"lag_trading_days": 0}})
        assert r["ok"] is True and r["kind"] == ""


class TestClassifySync:
    def test_date_format_difference_is_not_a_mismatch(self):
        """**回归锁**: `20260918` 与 `2026-09-18` 是同一天。

        首次实现用裸 `!=` 比较, 于是每轮都报 `silent_no_op` 假阳性 ——
        判据里任何跨来源的日期比较都必须先归一。
        """
        r = G.classify_sync({"ok": True, "appended": 0,
                             "engine_last_day": "20260918",
                             "h5i_max_before": "2026-09-18",
                             "h5i_max_after": "2026-09-18",
                             "missing_trading_days": 0, "planned_days": []})
        assert r["ok"] is True, r["detail"]

    def test_silent_no_op_when_engine_leads(self):
        """真正该抓的: ok=true 但 appended=0, 而引擎**领先** h5i。"""
        r = G.classify_sync({"ok": True, "appended": 0,
                             "engine_last_day": "20260922",
                             "h5i_max_before": "2026-09-18",
                             "h5i_max_after": "2026-09-18"})
        assert r["ok"] is False and r["kind"] == "silent_no_op"
        assert "假成功" in r["detail"]

    def test_zero_appended_with_plan_is_failure(self):
        r = G.classify_sync({"ok": True, "appended": 0,
                             "planned_days": ["20260921"]})
        assert r["ok"] is False and r["kind"] == "silent_no_op"

    def test_step_failure_reported(self):
        r = G.classify_sync({"ok": False, "error": "boom"})
        assert r["ok"] is False and r["kind"] == "sync_failed"

    def test_missing_step(self):
        r = G.classify_sync(None)
        assert r["ok"] is False and r["kind"] == "step_missing"

    def test_appended_positive_is_ok(self):
        r = G.classify_sync({"ok": True, "appended": 42,
                             "engine_last_day": "20260922",
                             "h5i_max_after": "2026-09-22"})
        assert r["ok"] is True


class TestClassifyDbUpdate:
    def test_all_tables_failed(self):
        """实测 2026-09-22: 12/12 表 ok=false, rows=0, 而供应商自评 last_status=ok。"""
        step = {"ok": False,
                "tables": {f"t{i}": {"ok": False, "rows": 0} for i in range(12)},
                "akshare_stats": {"last_status": "ok", "last_err": "",
                                  "network_errors": 0, "empty_count": 0}}
        r = G.classify_db_update(step)
        assert r["ok"] is False and r["kind"] == "all_tables_failed"
        assert "12/12" in r["detail"]
        # 归因必须带出"步骤级未给原因"与供应商自评 —— 否则无从下手
        assert "未给原因" in r["detail"] and "last_status" in r["detail"]

    def test_partial_failure(self):
        r = G.classify_db_update({"ok": False, "tables": {
            "a": {"ok": True, "rows": 5}, "b": {"ok": False, "rows": 0}}})
        assert r["ok"] is False and r["kind"] == "partial_tables_failed"

    def test_success_but_all_zero_rows_is_observed_only(self):
        r = G.classify_db_update({"ok": True, "tables": {
            "a": {"ok": True, "rows": 0}, "b": {"ok": True, "rows": 0}}})
        assert r["ok"] is True and r["observed_only"] is True

    def test_failure_without_table_detail(self):
        r = G.classify_db_update({"ok": False})
        assert r["ok"] is False and r["kind"] == "no_table_detail"


class TestEvaluate:
    def test_empty_input_is_unknown_not_ok(self):
        """什么都没判 = 门禁未生效, **不等于健康**。"""
        r = G.evaluate(ledger="nonexistent.jsonl")
        assert r["level"] == G.UNKNOWN and r["allow"] is False or True
        assert r["level"] == G.UNKNOWN

    def test_healthy_inputs_are_ok(self):
        r = G.evaluate(engine_probe={"ok": True, "freshness": {"lag_trading_days": 0}},
                       sync_step={"ok": True, "appended": 5,
                                  "engine_last_day": "20260922",
                                  "h5i_max_after": "2026-09-22"},
                       db_update_step={"ok": True, "tables": {
                           "a": {"ok": True, "rows": 9}}},
                       ledger="nonexistent.jsonl")
        assert r["level"] == G.OK and r["allow"] is True

    def test_single_failure_is_degraded_not_halt(self):
        """**关键**: 单次抖动不停手 —— 误停手与静默停手同样不可接受。"""
        r = G.evaluate(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger="nonexistent.jsonl")
        assert r["level"] == G.DEGRADED and r["allow"] is True
        assert r["halt_sources"] == []

    def test_consecutive_failures_reach_halt(self, tmp_path):
        """账本里**真有 3 次**同因失败 => HALT(阈值是 3, 不是 2)。

        [2026-09-23 修] 本用例原先只造 **2** 条账本就断言 HALT —— 那是**把 bug
        当规格写进了测试**: `evaluate` 曾无条件 `+1`(把"传进来的这次观测"算作
        账本里还没有的第 N+1 次), 于是 2 条 +1 = 3 触发 HALT。
        而生产里 `run_daily` 记完账之后, 守护进程**又读同一天的产物再判一次**,
        那次 `+1` 就把**同一次失败数了两遍** ⇒ 实测: 账本 2 条却报 `[HALT×3]`
        ⇒ 次日会跳过摄入与选股(**提前一天停手**)。
        修正后语义: 只有显式声明 `unrecorded` 的源才 +1; 传进来的 step 默认视为已入账。
        """
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"} for _ in range(3)
        ])
        r = G.evaluate(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger=led)
        assert r["level"] == G.HALT and r["allow"] is False
        assert r["halt_sources"] == ["db_update"]

    def test_recorded_failure_is_not_counted_twice(self, tmp_path):
        """**核心回归**: 同一次失败被判两次, 不能变成两次计数。

        这是 2026-09-23 的真实事故形态 —— run_daily 19:10 记一次, 守护 19:15
        读当天产物再判一次。若第二次又 +1, 系统就会**提前一天停手**:
        账本 2 条 => 报 HALT×3 => 次日 `run_daily` 跳过摄入与选股。
        本仓对"静默停手"零容忍, 对"误停手"同样零容忍。
        """
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"} for _ in range(2)
        ])
        step = {"ok": False, "tables": {"a": {"ok": False, "rows": 0}}}
        # 已入账(默认): 2 条就是 2 条, 仍是 DEGRADED
        r = G.evaluate(db_update_step=step, ledger=led)
        assert r["level"] == G.DEGRADED and r["allow"] is True, r["reasons"]
        assert r["items"][0]["consecutive_failures"] == 2
        assert r["items"][0]["counted_as_unrecorded"] is False
        # 显式声明"这次还没入账"才会 +1 -> 3 => HALT(即"第 3 次真失败")
        r2 = G.evaluate(db_update_step=step, ledger=led,
                        unrecorded=(G.SRC_DB_UPDATE,))
        assert r2["level"] == G.HALT and r2["allow"] is False
        assert r2["items"][0]["consecutive_failures"] == 3
        assert r2["items"][0]["counted_as_unrecorded"] is True

    def test_run_daily_declares_only_the_probe_as_unrecorded(self):
        """`run_daily` 必须**声明**只有引擎探针未入账, 且必须用**常量**而非字面量。

        为什么断言到源码这一层: 这个参数答错**不会报错**, 只会让计数偏一位,
        而偏一位的后果是提前停手。用字面量 `("stockdb_engine",)` 手写时源名写错
        同样不报错 —— 故要求走 `UNRECORDED_AT_DAILY_START` 这个有名字的常量。
        """
        import io as _io
        import os as _os
        p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "src", "run_daily.py")
        src = _io.open(p, encoding="utf-8").read()
        assert "check_and_record(" in src
        assert "unrecorded=_DG.UNRECORDED_AT_DAILY_START" in src, (
            "run_daily 的 check_and_record 必须显式声明 unrecorded —— "
            "不声明会让同一次失败被数两遍(2026-09-23 实测: 2 条账本报 HALT×3)")
        assert G.UNRECORDED_AT_DAILY_START == (G.SRC_ENGINE,), (
            "起跑时只有引擎探针是当场新探的; sync/db_update 来自上一轮产物(已入账)")

    def test_alternating_kinds_do_not_halt(self, tmp_path):
        """交替原因说明探测本身不稳定 —— 该修探测, 不该停数据。"""
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"},
            {"source": "db_update", "ok": False, "kind": "no_table_detail"},
        ])
        r = G.evaluate(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger=led)
        assert r["level"] == G.DEGRADED and r["allow"] is True

    def test_success_resets_the_counter(self, tmp_path):
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"},
            {"source": "db_update", "ok": True, "kind": ""},
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"},
        ])
        r = G.evaluate(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger=led)
        assert r["level"] == G.DEGRADED, "成功后计数未重置"

    def test_scope_excludes_the_daemon(self):
        """**核心取舍**: 只拦摄入, 不停守护 —— 停守护就再没东西能发现数据恢复。"""
        r = G.evaluate(engine_probe={"ok": False, "error": "x"},
                       ledger="nonexistent.jsonl")
        assert "不停守护进程" in r["scope"]

    def test_thresholds_are_declared(self):
        r = G.evaluate(ledger="nonexistent.jsonl")
        th = r["thresholds"]
        assert th["fails_to_halt"] == G.FAILS_TO_HALT
        assert th["publisher_grace_trading_days"] == G.PUBLISHER_GRACE_TRADING_DAYS
        assert th["engine_lag_halt_trading_days"] == G.ENGINE_LAG_HALT_TRADING_DAYS


class TestCheckAndRecord:
    def test_records_every_item_and_gate_verdict(self, tmp_path):
        led = str(tmp_path / "ds.jsonl")
        r = G.check_and_record(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger=led)
        rows = G.read_ledger(led)
        assert any(x["source"] == "db_update" for x in rows)
        assert any(x["source"] == "_gate" for x in rows)
        assert r["level"] == G.DEGRADED

    def test_halt_is_recorded_with_level_kind(self, tmp_path):
        """HALT 时必须往账本写一条 `_gate` 记录(kind=HALT), 否则事后无法归因。

        [2026-09-23 修] 本用例原先造 **2** 条账本 —— 那是把"无条件 +1"的 bug
        当规格。阈值 `FAILS_TO_HALT=3` 指的是**3 次真实失败**, 故这里造 3 条。
        """
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"} for _ in range(3)
        ])
        r = G.check_and_record(db_update_step={"ok": False, "tables": {
            "a": {"ok": False, "rows": 0}}}, ledger=led)
        assert r["allow"] is False
        assert any(x["source"] == "_gate" and x["kind"] == "HALT"
                   for x in G.read_ledger(led))

    def test_declaring_unrecorded_lowers_the_halt_bar_by_one(self, tmp_path):
        """声明"本次未入账"应把 HALT 提前一步 —— 这正是 run_daily 起跑时的情形。

        `run_daily` 在**摄入之前**判定, 此刻当天的 db_update 还没跑、账本里没有它;
        它喂的 `db_update_step` 是**上一轮**的产物(已入账)。两种情形必须可区分,
        否则要么提前停手(不声明), 要么漏掉预警(永远不 +1)。
        """
        led = _ledger(tmp_path, [
            {"source": "db_update", "ok": False, "kind": "all_tables_failed"} for _ in range(2)
        ])
        step = {"ok": False, "tables": {"a": {"ok": False, "rows": 0}}}
        # 已入账 => 2 条就是 2 条
        assert G.check_and_record(db_update_step=step, ledger=led)["allow"] is True
        # 未入账 => 算作第 3 次 => HALT
        r = G.check_and_record(db_update_step=step, ledger=led,
                               unrecorded=(G.SRC_DB_UPDATE,))
        assert r["allow"] is False and r["level"] == G.HALT

    def test_engine_exception_does_not_block(self, monkeypatch, tmp_path):
        """**纪律**: 门禁自身异常返回 allow=True（一个 bug 不能让系统静默停手）。"""
        def boom(**kw):
            raise RuntimeError("gate bug")
        monkeypatch.setattr(G, "evaluate", boom)
        r = G.check_and_record(ledger=str(tmp_path / "x.jsonl"))
        assert r["allow"] is True
        assert "不阻断" in r["reasons"][0]

    def test_record_failure_does_not_raise(self, monkeypatch):
        assert isinstance(G.record("x", True, ledger="\0bad\0"), dict)

    def test_ledger_survives_corrupt_lines(self, tmp_path):
        p = tmp_path / "ds.jsonl"
        p.write_text('{"source":"a","ok":true}\nbroken\n[1,2]\n{"source":"b","ok":false}\n',
                     encoding="utf-8")
        rows = G.read_ledger(str(p))
        assert [r["source"] for r in rows] == ["a", "b"]


class TestConsecutiveFailures:
    def test_counts_only_the_trailing_run(self, tmp_path):
        led = _ledger(tmp_path, [
            {"source": "s", "ok": False, "kind": "k1"},
            {"source": "s", "ok": True, "kind": ""},
            {"source": "s", "ok": False, "kind": "k1"},
            {"source": "s", "ok": False, "kind": "k1"},
        ])
        cf = G.consecutive_failures("s", ledger=led)
        assert cf["n"] == 2 and cf["same_kind"] is True

    def test_mixed_kinds_flag(self, tmp_path):
        led = _ledger(tmp_path, [
            {"source": "s", "ok": False, "kind": "k1"},
            {"source": "s", "ok": False, "kind": "k2"},
        ])
        assert G.consecutive_failures("s", ledger=led)["same_kind"] is False

    def test_other_sources_ignored(self, tmp_path):
        led = _ledger(tmp_path, [
            {"source": "other", "ok": False, "kind": "k"},
            {"source": "s", "ok": False, "kind": "k"},
        ])
        assert G.consecutive_failures("s", ledger=led)["n"] == 1


class TestWiring:
    def test_health_state_consumes_the_gate(self):
        """接线存在性: 门禁的 HALT 必须能抬成 HALTED, 否则它只是个报告脚本。"""
        import health_state as H
        base = {"tick_ms": {"p50": 5, "p95": 9}, "freshness_ok": True, "l3_today": 0}
        assert H.assemble({**base, "datasource": {"level": "HALT",
                                                  "reasons": ["x"]}})["state"] == "HALTED"
        assert H.assemble({**base, "datasource": {"level": "DEGRADED",
                                                  "reasons": ["x"]}})["state"] == "DEGRADED"
        assert H.assemble({**base, "datasource": {"level": "OK"}})["state"] == "NORMAL"

    def test_health_state_gather_collects_it(self):
        import inspect
        import health_state as H
        assert "datasource_gate" in inspect.getsource(H.gather)

    def test_run_daily_is_gated(self):
        """run_daily 必须在摄入前判定, 且 HALT 时**跳过摄入与选股**、保留持仓归档。"""
        import inspect
        import run_daily as RD
        src = inspect.getsource(RD.run_daily)
        assert "datasource_gate" in src
        i = src.find("datasource_gate")
        block = src[i:i + 2500]
        assert "ds_allow" in src or "allow" in block
        assert "跳过摄入" in src or "skip" in block.lower()

    def test_run_daily_keeps_position_archival_on_halt(self):
        """停手不得停持仓归档 —— 与 kill_switch『只停新开仓, 绝不停离场』同纪律。"""
        import inspect
        import run_daily as RD
        src = inspect.getsource(RD.run_daily)
        i = src.find("ds_allow")
        assert i > 0
        block = src[i:i + 3000]
        assert "自选股/持仓归档" in block or "持仓归档照常" in block

    def test_run_daily_probes_the_engine(self):
        """**生产必须真探引擎**。

        2026-09-22 实测: 只传 `sync_step`/`db_update_step` 时, 判定结果里连
        `stockdb_engine` 都不出现 —— 检查项**静默消失**。因为
        `evaluate` 是 `if engine_probe is not None` 才检查, 而 run_daily 当时
        从没传过它 ⇒ 「供应商引擎不可达」这条失效模式只活在单元测试里。
        检查项消失比检查失败更危险: 报告上"没有这一项"看起来和"这一项没问题"一样。
        """
        import inspect
        import run_daily as RD
        src = inspect.getsource(RD.run_daily)
        assert "probe_engine(" in src
        assert "engine_probe=" in src


class TestProbeEngineEncoding:
    """`probe_engine` 的两处易错点(均已在实盘踩到)。"""

    def test_never_returns_none_and_never_raises(self, monkeypatch):
        """查不成必须给 ok=False 带原因 —— 绝不能返回 None。

        因为 `evaluate` 用 `if engine_probe is not None` 决定**要不要检查引擎**,
        返回 None 就等于放弃检查, 而在报告里表现为"这项不存在"。
        """
        import subprocess
        import types

        def fake_run(*a, **kw):
            raise FileNotFoundError("no interpreter")
        monkeypatch.setattr(subprocess, "run", fake_run)
        r = G.probe_engine()
        assert r["ok"] is False and r["error"]
        assert "probe" not in r or r.get("probe") is None

    def test_stdout_none_is_reported_not_swallowed(self, monkeypatch):
        """`text=True` 在中文 Windows 上会按 GBK 解码, 而错发生在**读取线程**里:
        `subprocess.run` 不抛, 而是静默返回 `stdout=None`。历史的 `txt = out.stdout or ""`
        把这种失败变成"输出里没有 JSON", 再退化成"探针没有结论"。
        必须**如实报出** stdout 为 None 这件事本身。
        """
        import subprocess

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(a[0] if a else [], 0, None,
                                               "UnicodeDecodeError: 'gbk' ...")
        monkeypatch.setattr(subprocess, "run", fake_run)
        r = G.probe_engine()
        assert r["ok"] is False
        assert "None" in r["error"] and "gbk" in r["error"]

    def test_passes_explicit_utf8_encoding(self, monkeypatch):
        """真正的修法: 显式 `encoding='utf-8'`, 不靠平台默认值。

        锁住它是因为这个 bug **完全不可见**: 不加也不报错, 只是永远探不到引擎。
        """
        import subprocess
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(a[0] if a else [], 0,
                                               '{"ok": true, "freshness": '
                                               '{"lag_trading_days": 0}}', "")
        monkeypatch.setattr(subprocess, "run", fake_run)
        r = G.probe_engine()
        assert seen.get("encoding") == "utf-8"
        assert seen.get("errors") == "replace"
        assert r["ok"] is True and r["probe"]["ok"] is True


class TestLedgerChainIsNotSilentlyDegraded:
    """账本**链退化**必须可见。

    2026-09-22 实测: `record()` 里裸 `except Exception` 吞掉
    `ModuleNotFoundError: No module named 'audit_chain'`（走 `python -m src.datasource_gate`
    时裸名导入解析不到）, 于是每条记录都退化成**无 hash 的平 JSON 行** ——
    一个"防篡改"的审计账本, 悄无声息地变成一个可随意改写的文本文件。
    链坏掉这件事原本**不以任何形式浮出水面**（`chained: False` 被调用方直接丢弃）。
    """

    def test_record_reports_why_chaining_failed(self, monkeypatch, tmp_path):
        def boom(*a, **kw):
            raise ModuleNotFoundError("No module named 'audit_chain'")
        monkeypatch.setattr(G._AC, "append", boom)
        r = G.record("s", True, ledger=str(tmp_path / "x.jsonl"))
        assert r["ok"] is True          # 记录仍然保住了(留痕的内容比形式重要)
        assert r["chained"] is False    # 但**不假装链是好的**
        assert "audit_chain" in r["chain_error"]

    def test_check_and_record_flags_degraded_ledger(self, monkeypatch, tmp_path):
        def boom(*a, **kw):
            raise ModuleNotFoundError("No module named 'audit_chain'")
        monkeypatch.setattr(G._AC, "append", boom)
        r = G.check_and_record(db_update_step={"ok": True, "rows": 5},
                               ledger=str(tmp_path / "x.jsonl"))
        assert r["persist"]["unchained"] > 0
        assert r["ledger_degraded"] is True
        assert any("账本退化" in x for x in r["reasons"])
        assert r["level"] != G.OK, "留痕坏了就不能报 OK"

    def test_healthy_ledger_is_not_flagged(self, tmp_path):
        r = G.check_and_record(db_update_step={"ok": True, "rows": 5},
                               ledger=str(tmp_path / "x.jsonl"))
        assert r["persist"]["unchained"] == 0
        assert not r.get("ledger_degraded")

    def test_chain_health_detects_flat_records(self, tmp_path):
        """主动验证链: `unchained_tail` > 0 就是链在退化。"""
        p = tmp_path / "flat.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"source": "s", "ok": True}, ensure_ascii=False) + "\n")
        ch = G.chain_health(str(p))
        assert ch["unchained_tail"] == 1 and ch["ok"] is False

    def test_chain_health_accepts_a_real_chain(self, tmp_path):
        p = tmp_path / "good.jsonl"
        G.record("s", True, ledger=str(p))
        ch = G.chain_health(str(p))
        assert ch["unchained_tail"] == 0
        assert ch["ok"] is True and ch["verify"]["ok"] is True

    def test_bare_import_of_audit_chain_works_under_m(self):
        """锁住根因: 模块级导入 `audit_chain`, 并自补 src/ 到 sys.path。

        否则 `python -m src.datasource_gate` 下裸名解析不到 —— 而 CLI 正是这么跑的。
        """
        import inspect
        src = inspect.getsource(G)
        assert "\nimport audit_chain as _AC" in src
        assert "sys.path.insert(0, _SRC_DIR)" in src
        assert G._AC.__name__ == "audit_chain"


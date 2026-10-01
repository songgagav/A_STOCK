# -*- coding: utf-8 -*-
"""门禁结论的**读取侧**判据: 快照读已落盘的权威结论, 不重算.

## 这次修的是什么(实测定性, 2026-09-28)

`health_state.gather()` 原先**自己重算**门禁:
`evaluate(sync_step=..., db_update_step=...)` —— 只喂两个**辅助源**, 而唯一的
关键源 `stockdb_engine` 需要 `engine_probe`, 快照路径从不传它。后果:

  · 快照档位在结构上只能是 `OK/DEGRADED/UNKNOWN`, **HALT 不可达**
    (辅助源按 `datasource_gate` 的 L538-546 只让 DEGRADED, 永不计入 halt_sources);
  · 输入取自"今天"的 `daily_summary.json`(19:10 才写) ⇒ 交易时段读的是上一轮 ——
    实测 09-24 的 `00:03..19:08` 共 177 条快照**全是 UNKNOWN**;
  · **最要紧**: 09-27 落盘结论是 `HALT/allow=False`(权威), 而快照重算得
    `DEGRADED/allow=True` —— **方向相反**, 因为真正致停的关键源恰好缺席。

## 修后的设计(用户确认)

```text
字段: 「最近一轮门禁结论」
  ├─ 值        : 最后一次 run_daily 落盘的结论(steps.datasource_gate)
  ├─ 时间戳    : 该结论的 checked_at
  └─ 新鲜度标记: is_today / age_hours / stale
消费者判断:
  ├─ 时间戳是今天 19:10 后 -> 用当日结论
  ├─ 是上一轮            -> 标注「截至 <day> 那一轮」
  └─ 超过阈值未更新       -> 告警(不得沉默)
```

**为什么语义定为「最近一轮」而不是「当日」**: 19:10 之前当日结论尚不存在,
此时如实给出"截至哪一轮"比伪装成当日更有用。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import health_state as H  # noqa: E402

_TODAY = datetime(2026, 9, 28, 2, 0, 0)


def _verdict(level="HALT", allow=False, checked_at="2026-09-27 15:05:23",
             halt_sources=("stockdb_engine",),
             reasons=("stockdb_engine: [HALT×3] 引擎落后 2 个交易日 > 发布宽限 1 —— 疑厂商漏发",)):
    """构造一份结论。

    `reasons` 默认值**照抄真实落盘文本**(含源名前缀) —— 因为 `assemble` 是把它
    原样转达的, 用"简化版"理由会掩盖"归因里有没有那个源"这件事。
    """
    return {"allow": allow, "level": level, "items": [], "checked_at": checked_at,
            "halt_sources": list(halt_sources), "reasons": list(reasons),
            "degraded_sources": [], "thresholds": {}}


def _write_day(root: Path, day8: str, verdict=None, extra_steps=None) -> None:
    d = root / day8
    d.mkdir(parents=True, exist_ok=True)
    steps = dict(extra_steps or {})
    if verdict is not None:
        steps["datasource_gate"] = verdict
    (d / "daily_summary.json").write_text(
        json.dumps({"day": day8, "steps": steps}, ensure_ascii=False),
        encoding="utf-8")


def _write_snapshot(root: Path, verdict: dict, day8: str) -> None:
    H.publish(str(root / "health" / "state.json"), payload={
        "ts": "2026-09-28 02:00:00",
        "state": "HALTED" if verdict["level"] == "HALT" else "NORMAL",
        "reasons": [],
        "observed": {
            "gate_verdict": verdict,
            "datasource": verdict,
            "gate_verdict_at": verdict["checked_at"],
            "gate_verdict_day": day8,
        },
    })


def _snap(**kw):
    s = {"tick_ms": None, "freshness_ok": None, "live_source": None, "l3_today": 0}
    s.update(kw)
    return s


class TestReadGateVerdict:
    """`read_gate_verdict`: 纯文件读, 取**最近一轮**结论。"""

    def test_reads_recorded_verdict_untouched(self, tmp_path):
        _write_day(tmp_path, "20260927", _verdict())
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["day"] == "20260927"
        assert r["value"]["level"] == "HALT"
        assert r["value"]["allow"] is False
        assert r["at"] == "2026-09-27 15:05:23"
        assert r["label"] == "截至昨日"
        assert r["error"] is None

    def test_does_not_recompute(self, tmp_path):
        """**核心不变量**: 报告的值必须与落盘**逐字节一致**, 不得被改写。

        这是"不重算"的可测形式: 故意落一个"反常识"的结论(level=OK 却 allow=False),
        读取侧必须**原样**报告它, 而不是"贴心地"修正成自洽值 ——
        修正会掩盖写入侧的 bug(那正是冲突检测要抓的东西)。
        """
        odd = _verdict(level="OK", allow=False)
        _write_day(tmp_path, "20260927", odd)
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["value"]["level"] == "OK"
        assert r["value"]["allow"] is False

    def test_is_today_flag(self, tmp_path):
        _write_day(tmp_path, "20260928", _verdict(checked_at="2026-09-28 19:12:00"))
        r = H.read_gate_verdict(str(tmp_path), today=datetime(2026, 9, 28, 20, 0))
        assert r["is_today"] is True
        assert r["label"] == "当日"
        assert r["day"] == "20260928"

    def test_today_does_not_require_a_fixed_1910_cutoff(self, tmp_path):
        _write_day(tmp_path, "20260928", _verdict(checked_at="2026-09-28 15:05:36"))
        r = H.read_gate_verdict(str(tmp_path), today=datetime(2026, 9, 28, 16, 0))
        assert r["is_today"] is True

    def test_future_timestamp_warns_instead_of_claiming_today(self, tmp_path):
        _write_day(tmp_path, "20260928", _verdict(checked_at="2026-09-28 19:12:00"))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert not r["is_today"]
        assert "晚于当前时间" in r["error"]

    def test_picks_the_most_recent_day_that_has_a_verdict(self, tmp_path):
        """从最近往回找**第一个含结论**的目录 —— 19:10 之前今天还没结论。"""
        _write_day(tmp_path, "20260926", _verdict(checked_at="2026-09-26 15:00:00"))
        _write_day(tmp_path, "20260927", _verdict(checked_at="2026-09-27 15:05:23"))
        # 今天(09-28)目录存在但还没跑到门禁那一步
        _write_day(tmp_path, "20260928", None,
                   extra_steps={"db_update": {"ok": True}})
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["day"] == "20260927", "应跳过尚无结论的今天, 取上一轮"

    def test_skips_days_whose_verdict_lacks_level(self, tmp_path):
        """`level` 缺失的条目不算结论(写入侧异常/半截产物)。"""
        _write_day(tmp_path, "20260927", {"allow": True})            # 无 level
        _write_day(tmp_path, "20260926", _verdict(checked_at="2026-09-26 15:00:00"))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["day"] == "20260926"

    def test_no_daily_dir_is_an_error_not_health(self, tmp_path):
        r = H.read_gate_verdict(str(tmp_path / "nope"), today=_TODAY)
        assert r["value"] is None
        assert r["error"], "取不到结论必须给出原因, 不得沉默"

    def test_empty_daily_dir_is_an_error(self, tmp_path):
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["value"] is None
        assert "没有任何日目录" in r["error"]

    def test_no_verdict_anywhere_is_an_error(self, tmp_path):
        _write_day(tmp_path, "20260927", None, extra_steps={"db_update": {"ok": True}})
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["value"] is None
        assert "没有任何一轮落盘过门禁结论" in r["error"]

    def test_malformed_checked_at_is_reported_not_treated_as_fresh(self, tmp_path):
        """时刻不可解析 ⇒ **如实标记**, 不能当成"新鲜"。"""
        _write_day(tmp_path, "20260927", _verdict(checked_at="not-a-time"))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["value"]["level"] == "HALT"       # 结论本身照报
        assert r["age_hours"] is None
        assert r["stale"] is False
        assert "checked_at" in (r["error"] or "")

    def test_tolerates_iso_and_compact_formats(self, tmp_path):
        for ts in ("2026-09-27T15:05:23", "2026-09-27 15:05"):
            _write_day(tmp_path, "20260927", _verdict(checked_at=ts))
            r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
            assert r["age_hours"] is not None, f"{ts} 应可解析"


class TestStaleness:
    """新鲜度: 结论陈旧 = 这段时间**没有在判定**, 属监测盲区。"""

    def test_fresh_verdict_is_not_stale(self, tmp_path):
        _write_day(tmp_path, "20260928",
                   _verdict(checked_at="2026-09-28 01:00:00"))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["age_hours"] == pytest.approx(1.0, abs=0.01)
        assert r["stale"] is False

    def test_old_verdict_is_stale(self, tmp_path):
        old = (_TODAY - timedelta(hours=H.GATE_VERDICT_STALE_HOURS + 2)
               ).strftime("%Y-%m-%d %H:%M:%S")
        _write_day(tmp_path, "20260927", _verdict(checked_at=old))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["stale"] is True
        assert r["age_hours"] > H.GATE_VERDICT_STALE_HOURS

    def test_threshold_boundary(self, tmp_path):
        """恰好等于阈值**不算**陈旧(严格大于才报)。"""
        edge = (_TODAY - timedelta(hours=H.GATE_VERDICT_STALE_HOURS)
                ).strftime("%Y-%m-%d %H:%M:%S")
        _write_day(tmp_path, "20260927", _verdict(checked_at=edge))
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["stale"] is False

    def test_env_override(self, tmp_path, monkeypatch):
        _write_day(tmp_path, "20260927",
                   _verdict(checked_at="2026-09-27 15:05:23"))   # 约 11h 前
        monkeypatch.setenv(H.ENV_GATE_STALE_HOURS, "1")
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["stale"] is True, "阈值改小后同一结论应变陈旧"

    def test_invalid_env_falls_back_instead_of_raising(self, monkeypatch):
        for bad in ("abc", "0", "-3"):
            monkeypatch.setenv(H.ENV_GATE_STALE_HOURS, bad)
            assert H._gate_stale_hours() == H.GATE_VERDICT_STALE_HOURS


class TestAssembleConsumesRecordedVerdict:
    """`assemble` 必须按落盘结论装配, 并把"截至哪一轮"讲清楚。"""

    def _snap_with(self, **rec):
        base = {"gate_verdict": None, "gate_verdict_at": None,
                "gate_verdict_day": None, "gate_verdict_is_today": False,
                "gate_verdict_age_hours": None, "gate_verdict_stale": False,
                "gate_verdict_error": None, "datasource": {}}
        base.update(rec)
        return _snap(**base)

    def test_halt_verdict_escalates_to_halted(self):
        """**09-27 场景**: 权威结论是 HALT ⇒ 状态必须 HALTED(修前是 DEGRADED)。"""
        r = H.assemble(self._snap_with(
            gate_verdict=_verdict(), datasource=_verdict(),
            gate_verdict_at="2026-09-27 15:05:23", gate_verdict_day="20260927",
            gate_verdict_is_today=False, gate_verdict_age_hours=11.0))
        assert r["state"] == "HALTED"
        joined = " ".join(r["reasons"])
        assert "HALT" in joined
        # 归因必须带上**真正致停的关键源**, 而不是只说"某辅助源失败"
        assert "stockdb_engine" in joined, "必须给出致停的关键源"

    def test_previous_round_is_labelled(self):
        """非当日结论必须标注「截至 <day> 那一轮」—— 否则读者会当成当日。"""
        r = H.assemble(self._snap_with(
            gate_verdict=_verdict(), datasource=_verdict(),
            gate_verdict_at="2026-09-27 15:05:23", gate_verdict_day="20260927",
            gate_verdict_is_today=False))
        assert any("截至 20260927" in x for x in r["reasons"])

    def test_today_verdict_is_not_labelled_as_previous(self):
        r = H.assemble(self._snap_with(
            gate_verdict=_verdict(), datasource=_verdict(),
            gate_verdict_at="2026-09-28 19:12:00", gate_verdict_day="20260928",
            gate_verdict_is_today=True))
        assert not any("截至" in x for x in r["reasons"])

    def test_stale_verdict_is_reported(self):
        r = H.assemble(self._snap_with(
            gate_verdict=_verdict(level="OK", allow=True), datasource=_verdict(level="OK", allow=True),
            gate_verdict_at="2026-09-26 15:00:00", gate_verdict_day="20260926",
            gate_verdict_is_today=False, gate_verdict_age_hours=40.0,
            gate_verdict_stale=True))
        assert r["state"] == "DEGRADED"
        assert any("未更新" in x for x in r["reasons"])

    def test_missing_verdict_is_degraded_not_normal(self):
        """**取不到结论 ≠ 健康**(新路径)。这是 DISC-2 同族纪律。"""
        r = H.assemble(self._snap_with(
            gate_verdict=None,
            gate_verdict_error="data/daily 下没有任何日目录"))
        assert r["state"] == "DEGRADED"
        assert any("取不到" in x for x in r["reasons"])

    def test_gate_said_unknown_is_degraded(self):
        v = _verdict(level="UNKNOWN", allow=True, halt_sources=[], reasons=[])
        r = H.assemble(self._snap_with(gate_verdict=v, datasource=v,
                                       gate_verdict_day="20260927"))
        assert r["state"] == "DEGRADED"
        assert any("UNKNOWN" in x for x in r["reasons"])

    def test_ok_verdict_alone_is_normal(self):
        v = _verdict(level="OK", allow=True, halt_sources=[], reasons=[])
        r = H.assemble(self._snap_with(gate_verdict=v, datasource=v,
                                       gate_verdict_day="20260928",
                                       gate_verdict_is_today=True))
        assert r["state"] == "NORMAL", r["reasons"]

    def test_canonical_field_wins_if_legacy_alias_diverges(self):
        halt = _verdict()
        ok = _verdict(level="OK", allow=True, halt_sources=[], reasons=[])
        r = H.assemble(self._snap_with(gate_verdict=halt, datasource=ok))
        assert r["state"] == "HALTED"

    def test_legacy_snap_without_new_keys_still_works(self):
        """向后兼容: 老快照(只有 `datasource`)仍按原语义装配。"""
        r = H.assemble(_snap(datasource={"level": "HALT", "allow": False,
                                         "reasons": ["旧格式"]}))
        assert r["state"] == "HALTED"
        assert any("旧格式" in x for x in r["reasons"])


class TestSnapshotMatchesDisk:
    """**清单第 4 项**: 快照读到的值 == 落盘值(永不冲突)。

    按设计二者同源, 故"冲突"只可能来自"有人又加了重算"。
    """

    def test_snapshot_value_is_identical_to_recorded(self, tmp_path):
        v = _verdict()
        _write_day(tmp_path, "20260927", v)
        r = H.read_gate_verdict(str(tmp_path), today=_TODAY)
        assert r["value"] == v, "读到的值必须与落盘逐字段相同"

    def test_conflict_detector_clean_on_consistent_data(self, tmp_path):
        daily = tmp_path / "daily"
        _write_day(daily, "20260927", _verdict())
        _write_day(daily, "20260926",
                   _verdict(level="DEGRADED", allow=True, halt_sources=[],
                            checked_at="2026-09-26 15:00:00"))
        _write_snapshot(tmp_path, _verdict(), "20260927")
        c = H.detect_gate_verdict_conflict(str(daily), today=_TODAY)
        assert c["ok"] is True
        assert c["conflict"] is False, c["details"]
        assert c["snapshot_compared"] is True
        assert c["checked"] == 2

    def test_conflict_detector_catches_published_value_mismatch(self, tmp_path):
        daily = tmp_path / "daily"
        _write_day(daily, "20260927", _verdict())
        other = _verdict(level="DEGRADED", allow=True, halt_sources=[])
        _write_snapshot(tmp_path, other, "20260927")
        c = H.detect_gate_verdict_conflict(str(daily), today=_TODAY)
        assert c["snapshot_compared"] is True
        assert c["conflict"] is True
        assert any("快照门禁值" in x for x in c["details"])

    def test_conflict_detector_catches_timestamp_mismatch(self, tmp_path):
        daily = tmp_path / "daily"
        v = _verdict()
        _write_day(daily, "20260927", v)
        _write_snapshot(tmp_path, v, "20260927")
        fp = tmp_path / "health" / "state.json"
        payload = json.loads(fp.read_text(encoding="utf-8"))
        payload["observed"]["gate_verdict_at"] = "2026-09-26 15:05:23"
        H.publish(str(fp), payload=payload)
        c = H.detect_gate_verdict_conflict(str(daily), today=_TODAY)
        assert c["conflict"] is True
        assert any("gate_verdict_at" in x for x in c["details"])


class TestPremarketCheckStatusMapping:
    """`premarket_healthcheck.check_gate_verdict_consistency` 的状态映射。

    把它放盘前体检(每天一次)而不是快照(每 5 分钟)的理由见该函数 docstring:
    交叉验证要读文件, 而"重算权威结论"需要起引擎探针子进程 —— 不能进热路径。
    """

    @staticmethod
    def _mod(tmp_path, monkeypatch, now=None):
        """把体检模块的 `DATA_DIR` 指向临时根, 并**冻结 `datetime.now`**。

        为什么必须冻结: `check_gate_verdict_consistency` 内部走
        `read_gate_verdict(...)` 的默认 `today=datetime.now()`。若不冻结,
        用固定时刻造的数据相对**真实当前时间**算年龄, 断言就随运行日漂移
        (本组第一版就是这么飘的)。
        """
        import datetime as _dt

        import premarket_healthcheck as P
        monkeypatch.setattr(P, "DATA_DIR", str(tmp_path), raising=False)
        fixed = now or _TODAY

        class _FrozenDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(H, "datetime", _FrozenDT)
        return P

    def _write(self, tmp_path, day8, verdict):
        """写到 `DATA_DIR/daily/<day8>/` —— 与体检模块的读取路径一致。"""
        _write_day(tmp_path / "daily", day8, verdict)

    def test_ok_when_consistent_and_fresh(self, tmp_path, monkeypatch):
        self._write(tmp_path, "20260928", _verdict(
            level="DEGRADED", allow=True, halt_sources=[],
            checked_at="2026-09-28 01:30:00"))
        _write_snapshot(tmp_path, _verdict(
            level="DEGRADED", allow=True, halt_sources=[],
            checked_at="2026-09-28 01:30:00"), "20260928")
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()
        assert r["status"] == "OK", r.get("detail")
        assert r["detail"]["conflict"] is False
        assert r["detail"]["checked_rounds"] == 1

    def test_fail_on_conflict(self, tmp_path, monkeypatch):
        """level↔allow 不自洽 => FAIL(不变量被破坏)。"""
        self._write(tmp_path, "20260927", _verdict(level="HALT", allow=True))
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()
        assert r["status"] == "FAIL"
        assert r["detail"]["conflict"] is True

    def test_warn_when_no_verdict_ever_recorded(self, tmp_path, monkeypatch):
        """从没落盘过结论 => WARN(门禁等于没在判定), **不是 OK**。"""
        _write_day(tmp_path / "daily", "20260927", None,
                   extra_steps={"db_update": {"ok": True}})
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()
        assert r["status"] == "WARN", r.get("detail")

    def test_warn_when_stale(self, tmp_path, monkeypatch):
        old = (_TODAY - timedelta(hours=H.GATE_VERDICT_STALE_HOURS + 5)
               ).strftime("%Y-%m-%d %H:%M:%S")
        self._write(tmp_path, "20260927", _verdict(
            level="DEGRADED", allow=True, halt_sources=[], checked_at=old))
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()
        assert r["status"] == "WARN"
        assert r["detail"]["stale"] is True

    def test_warn_when_snapshot_missing(self, tmp_path, monkeypatch):
        self._write(tmp_path, "20260928", _verdict(
            level="OK", allow=True, halt_sources=[], reasons=[],
            checked_at="2026-09-28 01:30:00"))
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()
        assert r["status"] == "WARN"
        assert r["detail"]["snapshot_error"]

    def test_warn_when_detector_cannot_run(self, tmp_path, monkeypatch):
        """检测器自己跑不成 => WARN(不可判定 ≠ 没问题)。"""
        P = self._mod(tmp_path, monkeypatch)
        r = P.check_gate_verdict_consistency()      # tmp_path 下没有 data/daily
        assert r["status"] == "WARN", r.get("detail")
        assert r["detail"]["read_error"]


class TestConflictDetectorCatchesInvariantBreaks:
    """冲突检测要能抓住**不变量的破坏**, 而不是只能说"一致"。"""

    def test_level_allow_inconsistency(self, tmp_path):
        _write_day(tmp_path, "20260927", _verdict(level="HALT", allow=True))
        c = H.detect_gate_verdict_conflict(str(tmp_path), today=_TODAY)
        assert c["conflict"] is True
        assert any("allow" in x for x in c["details"])

    def test_halt_without_halt_sources(self, tmp_path):
        _write_day(tmp_path, "20260927", _verdict(halt_sources=[]))
        c = H.detect_gate_verdict_conflict(str(tmp_path), today=_TODAY)
        assert c["conflict"] is True
        assert any("halt_sources" in x for x in c["details"])

    def test_unparseable_checked_at_flagged(self, tmp_path):
        _write_day(tmp_path, "20260927", _verdict(checked_at=""))
        c = H.detect_gate_verdict_conflict(str(tmp_path), today=_TODAY)
        assert c["conflict"] is True

    def test_detector_is_read_only_and_survives_missing_dir(self, tmp_path):
        c = H.detect_gate_verdict_conflict(str(tmp_path / "nope"), today=_TODAY)
        assert c["ok"] is False and c["conflict"] is False
        assert c["details"], "取不到也要说明原因"

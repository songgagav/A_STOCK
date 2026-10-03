# -*- coding: utf-8 -*-
"""09:25 冻结在引擎侧的 fail-closed 契约。"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import config  # noqa: E402
import realtime_engine as RE  # noqa: E402
from paper_book import PaperBook  # noqa: E402
from signal_snapshot import build_snapshot, read_snapshot, write_snapshot  # noqa: E402


_SH = ZoneInfo("Asia/Shanghai")
_AT_0924 = datetime(2026, 10, 3, 9, 24, 59, tzinfo=_SH)
_AT_0925 = datetime(2026, 10, 3, 9, 25, 0, tzinfo=_SH)
_AFTER_0925 = datetime(2026, 10, 3, 9, 26, 0, tzinfo=_SH)
_TARGETS = [{"canon": "600000.SH", "target_weight": 1.0}]


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """绕过网络、数据库和 PaperBook 初始化的最小引擎实例。"""
    instance = object.__new__(RE.RealtimeEngine)
    instance.pb = SimpleNamespace(
        trade_date="2026-10-03",
        positions={},
        d_price={},
    )
    instance.cur_day = "2026-10-03"
    instance.targets = [dict(item) for item in _TARGETS]
    instance.sel = {"top_n": instance.targets}
    instance.sel_day = "20261002"
    instance._freeze_attempted = False
    instance.snapshot_status = "pending"
    instance.snapshot_ref = None
    instance.snapshot_hash = None
    instance.tick = 0
    instance.push_only_in_session = True
    instance.midday_done = True
    instance._ca_applied_day = instance.pb.trade_date
    instance.feed = SimpleNamespace(
        last_error=None,
        quotes={},
        set_positions=Mock(),
        get_latest=Mock(return_value={"600000.SH": 10.0}),
    )
    instance._disk_ref_prices = Mock(return_value={})
    instance._apply_corporate_actions = Mock()
    instance._write_state = Mock()
    instance._rebalance = Mock()
    instance.in_session = Mock(return_value=True)
    monkeypatch.setattr(RE, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        RE,
        "load_targets",
        lambda _day: ([dict(item) for item in _TARGETS], {"top_n": _TARGETS}, "20261002"),
    )
    return instance


def test_missing_or_invalid_mode_control_defaults_to_shadow(tmp_path):
    """无配置或坏配置不能意外把真实消费路径切成 enforce。"""
    assert RE.read_mode_control(str(tmp_path)) == {"mode": "shadow"}
    control = tmp_path / "config" / "signal_freeze_mode.json"
    control.parent.mkdir()
    control.write_text("{bad-json", encoding="utf-8")
    assert RE.read_mode_control(str(tmp_path)) == {"mode": "shadow"}

    control.write_text('{"mode": "enforce", "promoted_by": "tester"}', encoding="utf-8")
    assert RE.read_mode_control(str(tmp_path)) == {"mode": "shadow"}


def test_paper_book_rejects_nonpaper_broker_at_order_entry(monkeypatch):
    """防止未来配置被改成券商通道后仍从纸面账本悄然产生订单。"""
    monkeypatch.setattr(config, "TRADE_BROKER", "easytrader")
    book = PaperBook(init_capital=10_000)

    with pytest.raises(RuntimeError, match="TRADE_BROKER"):
        book.buy("600000.SH", 100, 10.0)


def test_at_0925_engine_freezes_weighted_live_targets(engine, tmp_path):
    """09:25 仅此一次重新解析、加权、原子写入并复读校验。"""
    targets, _sel, _source_day, meta = engine._freeze_or_load_targets(_AT_0925)

    assert targets == _TARGETS
    assert meta["snapshot_status"] == "ready"
    persisted = read_snapshot(str(tmp_path), "20261003")
    assert persisted["status"] == "ready"
    assert persisted["snapshot"]["weights"] == {"600000.SH": 1.0}


def test_before_cutoff_keeps_live_resolution_for_preparation(engine):
    """09:25 前只允许准备候选，不得写出权威冻结结论。"""
    targets, _sel, _source_day, meta = engine._freeze_or_load_targets(_AT_0924)

    assert targets == _TARGETS
    assert meta["snapshot_status"] == "pending"
    assert engine._freeze_attempted is False


def test_after_cutoff_reads_verified_snapshot_without_live_resolve(engine, monkeypatch, tmp_path):
    """冻结窗口结束后唯一权威来源是已落盘快照，而非实时五级解析器。"""
    snapshot = build_snapshot(
        "20261003", _TARGETS, "selection_same_day", "20261002", [], _AT_0925, _REPO,
    )
    write_snapshot(str(tmp_path), snapshot)
    engine._freeze_attempted = True
    monkeypatch.setattr(RE, "load_targets", Mock(side_effect=AssertionError("不得实时重算")))

    targets, _sel, source_day, meta = engine._freeze_or_load_targets(_AFTER_0925)

    assert targets == _TARGETS
    assert source_day == "20261002"
    assert meta["snapshot_status"] == "ready"


def test_nonready_snapshot_is_latched_for_the_rest_of_the_day(engine, tmp_path):
    """L1 后即使有人补写文件，当日自动调仓也不得恢复。"""
    engine._freeze_attempted = True
    _targets, _sel, _source_day, first = engine._freeze_or_load_targets(_AFTER_0925)
    assert first["snapshot_status"] == "missing"

    snapshot = build_snapshot(
        "20261003", _TARGETS, "selection_same_day", "20261002", [], _AT_0925, _REPO,
    )
    write_snapshot(str(tmp_path), snapshot)
    _targets, _sel, _source_day, second = engine._freeze_or_load_targets(_AFTER_0925)

    assert second["snapshot_status"] == "missing"
    assert second["snapshot_reason"] == "snapshot_not_found"


@pytest.mark.parametrize("status", ["missing", "invalid", "tampered"])
def test_enforce_nonready_snapshot_only_values_and_never_rebalances(engine, monkeypatch, status):
    """L1/L2/L3 均 fail-closed：仍估值，但自动调仓必须为零。"""
    monkeypatch.setattr(RE, "read_mode_control", lambda _root: {"mode": "enforce"})
    monkeypatch.setattr(
        engine,
        "_freeze_or_load_targets",
        lambda _now: (engine.targets, engine.sel, engine.sel_day, {
            "snapshot_status": status,
            "snapshot_ref": None,
            "snapshot_hash": None,
        }),
    )

    engine.run_tick(now=_AFTER_0925)

    assert engine.snapshot_status == status
    engine._rebalance.assert_not_called()
    engine._write_state.assert_called_once()

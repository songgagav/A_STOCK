# -*- coding: utf-8 -*-
"""V3 backend contract for the dashboard Hero daily series."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import dashboard  # noqa: E402


def _write_summary(root: Path, day: str, equity: float) -> None:
    folder = root / day
    folder.mkdir(parents=True)
    (folder / "daily_summary.json").write_text(
        json.dumps({"day": day, "equity": equity}),
        encoding="utf-8",
    )


def test_read_daily_series_is_empty_without_valid_daily_summaries(tmp_path, monkeypatch):
    """没有可用历史时返回空数组，不抛异常、不补造数据。"""
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    monkeypatch.setattr(dashboard, "DAILY_DIR", str(daily_root))

    assert dashboard.read_daily_series() == []


def test_read_daily_series_returns_recent_twenty_in_ascending_order(tmp_path, monkeypatch):
    """序列最多 20 条，取最近数据并按日期升序返回。"""
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    for index in range(1, 23):
        _write_summary(daily_root, f"202609{index:02d}", 100000 + index * 100)
    monkeypatch.setattr(dashboard, "DAILY_DIR", str(daily_root))

    rows = dashboard.read_daily_series()

    assert len(rows) == 20
    assert [row["day"] for row in rows] == sorted(row["day"] for row in rows)
    assert rows[0]["day"] == "2026-09-03"
    assert rows[-1]["day"] == "2026-09-22"
    assert set(rows[0]) == {"day", "equity", "nav", "dd", "daily_return"}


def test_read_daily_series_uses_decimal_metrics_and_rounding(tmp_path, monkeypatch):
    """净值、回撤和日收益均使用约定的小数单位。"""
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    _write_summary(daily_root, "20260901", 100000.00)
    _write_summary(daily_root, "20260902", 101000.00)
    _write_summary(daily_root, "20260903", 99000.00)
    monkeypatch.setattr(dashboard, "DAILY_DIR", str(daily_root))

    rows = dashboard.read_daily_series()

    assert rows[0] == {
        "day": "2026-09-01",
        "equity": 100000.0,
        "nav": 1.0,
        "dd": 0.0,
        "daily_return": 0.0,
    }
    assert rows[1]["daily_return"] == pytest.approx(0.01)
    assert rows[2]["daily_return"] == pytest.approx(-0.019802)
    assert rows[2]["dd"] == pytest.approx(-0.0198)


def test_read_live_payload_always_exposes_daily_series(tmp_path, monkeypatch):
    """/api/live 的兼容包装始终提供 daily_series 字段。"""
    live_state = tmp_path / "live_state.json"
    live_state.write_text(json.dumps({"day": "20261003", "capital": {}}), encoding="utf-8")
    monkeypatch.setattr(dashboard, "LIVE_STATE", str(live_state))
    daily_root = tmp_path / "daily"
    daily_root.mkdir()
    monkeypatch.setattr(dashboard, "DAILY_DIR", str(daily_root))

    payload = dashboard.read_live_payload()

    assert payload["day"] == "20261003"
    assert payload["daily_series"] == []

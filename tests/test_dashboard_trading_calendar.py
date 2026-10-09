# -*- coding: utf-8 -*-
"""看板交易日日历的只读契约。"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import dashboard  # noqa: E402


def _official_days(day: date) -> bool:
    return day.isoformat() in {
        "2026-10-08",
        "2026-10-09",
        "2026-10-12",
        "2026-10-13",
    }


def test_read_trading_calendar_exposes_explicit_session_and_neighbors(monkeypatch):
    monkeypatch.setattr(dashboard, "is_trading_day", _official_days)
    monkeypatch.setattr(
        dashboard,
        "calendar_provenance",
        lambda: {
            "source": "akshare_tool_trade_date_hist_sina",
            "strength": "official",
            "updated": "2026-10-09 00:00:00",
            "n": 4,
            "future_days": 2,
            "fallback_ok": True,
        },
    )

    result = dashboard.read_trading_calendar(date(2026, 10, 9), window=2)

    assert result["ok"] is True
    assert result["status"] == "authoritative"
    assert result["today"] == {
        "date": "2026-10-09",
        "is_trading_day": True,
        "session": "trading_day",
    }
    assert result["previous_trading_day"] == "2026-10-08"
    assert result["next_trading_day"] == "2026-10-12"
    assert [row["date"] for row in result["days"]] == [
        "2026-10-07",
        "2026-10-08",
        "2026-10-09",
        "2026-10-10",
        "2026-10-11",
    ]
    assert result["provenance"]["strength"] == "official"


def test_read_trading_calendar_does_not_guess_when_calendar_is_unavailable(monkeypatch):
    monkeypatch.setattr(dashboard, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(
        dashboard,
        "calendar_provenance",
        lambda: {
            "source": None,
            "strength": "weekday_only",
            "updated": None,
            "n": 0,
            "future_days": 0,
            "fallback_ok": False,
        },
    )

    result = dashboard.read_trading_calendar(date(2026, 10, 9), window=1)

    assert result["ok"] is True
    assert result["status"] == "unknown"
    assert result["today"]["session"] == "unknown"
    assert result["today"]["is_trading_day"] is None
    assert result["previous_trading_day"] is None
    assert result["next_trading_day"] is None
    assert all(row["session"] == "unknown" for row in result["days"])


def test_dashboard_page_contains_trading_calendar_contract():
    source = dashboard.PAGE
    script = (Path(dashboard.STATIC_ROOT) / "dashboard.js").read_text(encoding="utf-8")
    assert "calendarRail" in source
    assert "calendarStatus" in source
    assert "/api/trading-calendar" in script

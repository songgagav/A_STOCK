"""Deterministic calendar input for tests that exercise date semantics.

The production calendar is deliberately runtime data under ``data/`` and is
ignored by Git.  Tests that assert specific 2026 dates therefore need a small
in-process calendar instead of depending on a developer's local cache.
"""

from __future__ import annotations

import datetime as _dt


def deterministic_calendar_days() -> set[str]:
    """Return weekdays for the tested range, with the known 2026-09-25 break."""
    start = _dt.date(2020, 1, 1)
    end = _dt.date(2026, 12, 31)
    holiday = "20260925"
    days: set[str] = set()
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur.strftime("%Y%m%d") != holiday:
            days.add(cur.strftime("%Y%m%d"))
        cur += _dt.timedelta(days=1)
    return days


def install_if_missing(monkeypatch) -> None:
    """Provide the deterministic calendar only when no runtime cache exists."""
    import trading_calendar as tc

    if tc._calendar_days() is None:
        days = deterministic_calendar_days()
        monkeypatch.setattr(tc, "_calendar_days", lambda: set(days))

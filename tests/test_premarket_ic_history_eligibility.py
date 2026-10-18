# -*- coding: utf-8 -*-
"""An empty selection cannot produce IC, but must remain visible in health."""

import json
import os
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import ic_track  # noqa: E402
import premarket_healthcheck as health  # noqa: E402


_VALID_POOL = [
    {"canon": "000001.SZ", "signal": 0.1},
    {"canon": "000002.SZ", "signal": 0.2},
    {"canon": "000003.SZ", "signal": 0.3},
    {"canon": "000004.SZ", "signal": 0.4},
    {"canon": "000005.SZ", "signal": 0.5},
]


def _case(tmp_path, monkeypatch, *, include_latest_ic):
    daily = tmp_path / "daily"
    for day, pool in (
        ("20260828", _VALID_POOL),
        ("20260831", []),
        ("20260901", _VALID_POOL),
    ):
        day_dir = daily / day
        day_dir.mkdir(parents=True)
        (day_dir / "selection.json").write_text(
            json.dumps({"date": day, "top_n": pool}), encoding="utf-8"
        )

    history = daily / "ic_history.csv"
    rows = ["day,ic_h1,ic_h3,ic_h5,ic_h10", "20260828,0.1,0.2,0.3,0.4"]
    if include_latest_ic:
        rows.append("20260901,0.1,0.2,0.3,0.4")
    history.write_text("\n".join(rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(ic_track, "DAILY_DIR", str(daily))
    monkeypatch.setattr(ic_track, "IC_HISTORY", str(history))
    monkeypatch.setattr(health, "_latest_bar_day", lambda: ("20260902", "h5i"))
    monkeypatch.setattr(
        health, "_distinct_trade_days",
        lambda n: ["20260828", "20260831", "20260901", "20260902"],
    )


def test_empty_selection_is_warn_not_missing_ic(tmp_path, monkeypatch):
    _case(tmp_path, monkeypatch, include_latest_ic=True)

    result = health.check_ic_history()

    assert result["status"] == "WARN"
    assert result["detail"]["missing"] == []
    assert result["detail"]["empty_selection_days"] == ["20260831"]
    assert result["detail"]["eligible_due_days"] == 2
    assert any("20260831" in problem for problem in result["detail"]["problems"])


def test_missing_nonempty_selection_still_fails(tmp_path, monkeypatch):
    _case(tmp_path, monkeypatch, include_latest_ic=False)

    result = health.check_ic_history()

    assert result["status"] == "FAIL"
    assert result["detail"]["missing"] == ["20260901"]
    assert result["detail"]["empty_selection_days"] == ["20260831"]


def test_only_empty_selection_requires_no_ic_history_row(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    day_dir = daily / "20260831"
    day_dir.mkdir(parents=True)
    (day_dir / "selection.json").write_text(
        json.dumps({"date": "20260831", "top_n": []}), encoding="utf-8"
    )
    history = daily / "ic_history.csv"
    history.write_text("day,ic_h1,ic_h3,ic_h5,ic_h10\n", encoding="utf-8")
    monkeypatch.setattr(ic_track, "DAILY_DIR", str(daily))
    monkeypatch.setattr(ic_track, "IC_HISTORY", str(history))
    monkeypatch.setattr(health, "_latest_bar_day", lambda: ("20260902", "h5i"))
    monkeypatch.setattr(
        health, "_distinct_trade_days",
        lambda n: ["20260828", "20260831", "20260901", "20260902"],
    )

    result = health.check_ic_history()

    assert result["status"] == "WARN"
    assert result["detail"]["missing"] == []
    assert result["detail"]["eligible_due_days"] == 0
    assert result["detail"]["empty_selection_days"] == ["20260831"]


@pytest.mark.parametrize("bad_entry", [None, {"canon": "000001.SZ", "signal": "bad"}])
def test_malformed_nonempty_selection_fails_even_with_ic_row(
    tmp_path, monkeypatch, bad_entry
):
    _case(tmp_path, monkeypatch, include_latest_ic=True)
    (tmp_path / "daily" / "20260831" / "selection.json").write_text(
        json.dumps({"date": "20260831", "top_n": [bad_entry]}), encoding="utf-8"
    )
    with (tmp_path / "daily" / "ic_history.csv").open("a", encoding="utf-8") as f:
        f.write("20260831,0.1,0.2,0.3,0.4\n")

    result = health.check_ic_history()

    assert result["status"] == "FAIL"
    assert "20260831" in str(result["detail"])

# -*- coding: utf-8 -*-
"""冻结后 PaperBook 行情 watchlist 的来源隔离。"""
from __future__ import annotations

import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import paper_book as PB  # noqa: E402


def test_watchlist_is_verified_snapshot_targets_union_current_positions():
    feed = PB.PriceFeed()
    feed.set_snapshot_targets([{"canon": "600000.SH"}])
    feed.set_positions({"000001.SZ": {"qty": 100}})

    assert feed.watchlist_codes() == {"600000.SH", "000001.SZ"}


def test_positions_are_copied_when_injected():
    feed = PB.PriceFeed()
    positions = {"000001.SZ": {"qty": 100}}
    feed.set_snapshot_targets([{"canon": "600000.SH"}])
    feed.set_positions(positions)
    positions["300001.SZ"] = {"qty": 100}

    assert feed.watchlist_codes() == {"600000.SH", "000001.SZ"}


def test_enforced_watchlist_does_not_scan_selection_or_legacy_file(monkeypatch, tmp_path):
    daily = tmp_path / "daily" / "20261003"
    daily.mkdir(parents=True)
    (daily / "selection.json").write_text(
        json.dumps({"top_n": [{"canon": "300001.SZ"}]}), encoding="utf-8",
    )
    (tmp_path / "watchlist.json").write_text(json.dumps(["688001.SH"]), encoding="utf-8")
    monkeypatch.setattr(PB, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(PB, "DAILY_DIR", str(tmp_path / "daily"))
    feed = PB.PriceFeed()
    feed.set_snapshot_targets([{"canon": "600000.SH"}])
    feed.set_positions({"000001.SZ": {"qty": 100}})

    assert feed.watchlist_codes() == {"600000.SH", "000001.SZ"}

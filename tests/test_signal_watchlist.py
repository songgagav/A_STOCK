# -*- coding: utf-8 -*-
"""冻结后 PaperBook 行情 watchlist 的来源隔离。"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

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


def test_shadow_watchlist_skips_non_date_entries_and_empty_current_day(monkeypatch, tmp_path):
    """盘中当天可能还没有 selection.json，且 daily 下允许存在统计文件。"""
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "ic_history.csv").write_text("day,ic\n20261008,0.1\n", encoding="utf-8")
    (daily / "20261009").mkdir()
    prior = daily / "20261008"
    prior.mkdir()
    (prior / "selection.json").write_text(
        json.dumps({"top_n": [{"canon": "600000.SH"}, {"canon": "000001.SZ"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(PB, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(PB, "DAILY_DIR", str(daily))

    feed = PB.PriceFeed()

    assert feed.watchlist_codes() == {"600000.SH", "000001.SZ"}


def test_reference_fallback_uses_h5i_backed_store_and_marks_provenance(
    monkeypatch,
):
    class FakeDB:
        def get_bars(self, canon, n):
            return pd.DataFrame([{"close": 12.34}])

        def close(self):
            pass

    monkeypatch.setattr(PB, "_open_reference_db", lambda: FakeDB(), raising=False)
    feed = PB.PriceFeed()

    quotes = feed._fetch_from_duckdb(["600000.SH"])

    assert quotes["600000.SH"]["price"] == 12.34
    assert quotes["600000.SH"]["fallback_source"] == "h5i_reference"


def test_h5i_reference_snapshot_is_exposed_to_runtime_provenance():
    feed = PB.PriceFeed()
    feed._fetch_spot_with_timeout = lambda fetch_all=False: {
        "600000.SH": {
            "price": 12.34,
            "last_close": 12.34,
            "limit_up": 13.57,
            "limit_down": 11.11,
            "volume": 0,
            "suspended": None,
            "fallback": True,
            "fallback_source": "h5i_reference",
        }
    }

    assert feed.get_latest(["600000.SH"]) == {"600000.SH": 12.34}
    assert feed.last_source == "h5i_reference"
    assert feed.fallback_codes == {"600000.SH"}

"""Actual bar-result backend provenance, including H5i -> DuckDB fallback."""
from __future__ import annotations

import json
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

import db
import paper_book as PB
import realtime_engine as RE
from health_state import assemble
from flow_watchdog import evaluate
from test_runtime_provenance_receipt import HELD, POOL, NOW, quote, runtime


@pytest.fixture(autouse=True)
def isolated_bar_routing(monkeypatch):
    monkeypatch.setattr(db, "BAR_STORE", "h5i")
    monkeypatch.setattr(db, "_H5I_BULK_SLOT", {"frame": None, "key": None, "groups": None})
    monkeypatch.setattr("access_probe.record", lambda *_args, **_kwargs: None)


@pytest.fixture
def duck_fallback(monkeypatch):
    """Real StockDB SQL on an in-memory legacy store, after actual H5i failure."""
    def failed_h5i():
        raise OSError("H5i unavailable")

    monkeypatch.setattr(db, "_h5i_store", failed_h5i)
    monkeypatch.setattr(db, "duck_available", lambda: True)
    connection = duckdb.connect(":memory:")
    connection.execute("""
        CREATE TABLE daily_bars (
            symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
            close DOUBLE, volume DOUBLE, amount DOUBLE, change_pct DOUBLE
        )
    """)
    connection.execute("""
        INSERT INTO daily_bars VALUES
        ('600000', '2026-10-08', 9, 11, 8, 10, 100, 1000, 1)
    """)
    bars_db = db.StockDB()
    bars_db._con = connection
    yield bars_db
    bars_db.close()


@pytest.mark.parametrize("route", ["bulk-index", "bulk-filter", "direct"])
def test_h5i_return_sites_tag_backend_without_changing_values(monkeypatch, route):
    bars = pd.DataFrame({"symbol": ["600000"], "d": ["2026-10-08"], "close": [10.0]})
    bars.attrs["backend_source"] = "duckdb"  # stale upstream metadata must be overwritten
    monkeypatch.setattr(db, "_h5i_store", lambda: SimpleNamespace(bars=lambda *_a, **_kw: bars))
    if route != "direct":
        monkeypatch.setattr(db, "_H5I_BULK_SLOT", {
            "frame": bars, "key": "latest",
            "groups": {"600000": bars} if route == "bulk-index" else None,
        })
    result = db.StockDB().get_bars(HELD, 1)
    assert result.to_dict("list") == {"date": ["2026-10-08"], "close": [10.0]}
    assert result.attrs.get("backend_source") == "h5i"
    assert bars.attrs["backend_source"] == "duckdb", "Do not mutate the shared upstream frame"


def test_h5i_failure_tags_real_duckdb_result(duck_fallback):
    result = duck_fallback.get_bars(HELD, 1)
    assert result["close"].tolist() == [10.0]
    assert result.attrs.get("backend_source") == "duckdb"


def test_feed_uses_actual_duckdb_tag_under_h5i_config(duck_fallback, monkeypatch):
    monkeypatch.setattr(db, "StockDB", lambda: duck_fallback)
    result = PB.PriceFeed()._fetch_from_duckdb(symbols=[HELD])
    assert result[HELD]["price"] == 10.0
    assert result[HELD]["fallback_source"] == "duckdb_reference"


def test_engine_uses_actual_duckdb_tag_under_h5i_config(runtime, duck_fallback,
                                                       monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.feed, "_fetch_spot_with_timeout", lambda: {POOL: quote(20)})
    monkeypatch.setattr(runtime, "_ref_db", lambda: duck_fallback)
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == "duckdb_reference_held"
    assert state["price_sources"] == {HELD: "duckdb_reference", POOL: "akshare_spot"}
    assert state["data_ts"] is None
    assert runtime.pb.d_price == {HELD: 10.0, POOL: 20}


@pytest.mark.parametrize("backend,expected", [(None, "unknown_reference"),
                                             ("unexpected", "unknown_reference"),
                                             ("h5i", "h5i_reference"),
                                             ("duckdb", "duckdb_reference")])
def test_feed_reference_labels_result_attrs_not_config(monkeypatch, backend, expected):
    class BarReader:
        def get_bars(self, canon, n):
            frame = pd.DataFrame({"close": [10.0]})
            if backend is not None:
                frame.attrs["backend_source"] = backend
            return frame

        def close(self):
            pass

    monkeypatch.setattr(db, "StockDB", BarReader)
    result = PB.PriceFeed()._fetch_from_duckdb(symbols=[HELD])
    assert result[HELD]["fallback_source"] == expected
    assert result[HELD]["price_source"] == expected


@pytest.mark.parametrize("backend,expected", [(None, "unknown_reference_held"),
                                             ("h5i", "h5i_reference_held"),
                                             ("duckdb", "duckdb_reference_held")])
def test_engine_reference_labels_result_attrs_not_config(runtime, monkeypatch, tmp_path,
                                                         backend, expected):
    class BarReader:
        def get_bars(self, canon, n):
            frame = pd.DataFrame({"close": [10.0]})
            if backend is not None:
                frame.attrs["backend_source"] = backend
            return frame

    monkeypatch.setattr(runtime.feed, "_fetch_spot_with_timeout", lambda: {POOL: quote(20)})
    monkeypatch.setattr(runtime, "_ref_db", BarReader)
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == expected
    assert state["price_sources"][HELD] == expected.removesuffix("_held")
    assert state["data_ts"] is None
    assert assemble({"live_source": state["live_source"]})["state"] == "DEGRADED"


def test_untagged_feed_fallback_does_not_claim_a_backend(runtime, monkeypatch, tmp_path):
    untagged = quote(10)
    untagged.update(fallback=True)
    untagged.pop("price_source")
    monkeypatch.setattr(runtime.feed, "_fetch_spot_with_timeout",
                        lambda: {HELD: untagged, POOL: quote(20)})
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == "unknown_reference_held"
    assert state["price_sources"][HELD] == "unknown_reference"
    assert state["data_ts"] is None


def test_unknown_held_reference_is_degraded():
    assert assemble({"live_source": "unknown_reference_held"})["state"] == "DEGRADED"


def test_unknown_held_reference_is_feed_stale():
    state = {"in_session": True, "pid_alive": True, "tick": 10, "prev_tick": 9,
             "updated": "2026-10-09 10:00:00", "feed_error": "",
             "live_source": "unknown_reference_held"}
    result = evaluate(state, now=NOW.replace(tzinfo=None))
    assert (result["level"], result["cause"]) == ("CRITICAL", "feed_stale")

"""Task 2: real runtime price provenance and validated snapshot receipts."""
from __future__ import annotations

import json
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

import db
import paper_book as PB
import realtime_engine as RE
import run_daily as RD
from flow_watchdog import evaluate
from health_state import assemble
from signal_snapshot import SHANGHAI, build_snapshot, read_snapshot, write_snapshot

HELD = "600000.SH"
POOL = "000001.SZ"
EXTRA = "600519.SH"
DAY = "20261009"
NOW = datetime(2026, 10, 9, 10, 30, tzinfo=SHANGHAI)
TARGETS = [{"canon": HELD, "target_weight": 0.5},
           {"canon": POOL, "target_weight": 0.5}]


def quote(price, source="akshare_spot"):
    result = {"price": price, "last_close": price, "limit_up": price * 1.1,
              "limit_down": price * 0.9, "volume": 100, "suspended": False,
              "price_source": source}
    if source.endswith("reference"):
        result.update(fallback=True, fallback_source=source, volume=0)
    return result


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Real feed cache, PaperBook, run_tick and persisted state; no external IO."""
    engine = object.__new__(RE.RealtimeEngine)
    engine.pb = PB.PaperBook()
    engine.pb.trade_date = "2026-10-09"
    engine.pb.positions = {HELD: {"qty": 100, "avg_cost": 9.0,
                                  "buy_date": "2026-10-08"}}
    engine.targets = [dict(item) for item in TARGETS]
    engine.sel = {"top_n": engine.targets}
    engine.sel_day = DAY
    engine.tick = 0
    engine.push_only_in_session = True
    engine.midday_done = True
    engine.midday_targets = engine.midday_result = None
    engine._tick_ms = deque()
    engine._ca_applied_day = engine.pb.trade_date
    engine.feed = PB.PriceFeed(cache_ttl=0)
    engine._rebalance = lambda _prices: None
    engine._freeze_or_load_targets = lambda _now: (
        engine.targets, engine.sel, DAY,
        {"snapshot_status": "ready", "snapshot_ref": None, "snapshot_hash": None},
    )
    monkeypatch.setattr(RE, "read_mode_control", lambda _root: {"mode": "shadow"})
    monkeypatch.setattr("trading_calendar.is_trading_day", lambda _now: True)
    monkeypatch.setattr("deadman_switch.beat", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(RE, "LIVE_STATE", str(tmp_path / "live_state.json"))
    monkeypatch.setattr(RE, "STATE_FILE", str(tmp_path / "state.json"))
    return engine


@pytest.mark.parametrize(
    "live,reference,expected,sources,health",
    [
        ({HELD: quote(10), POOL: quote(20)}, {}, "akshare_spot",
         {HELD: "akshare_spot", POOL: "akshare_spot"}, "NORMAL"),
        ({HELD: quote(10)}, {POOL: 20}, "h5i_reference_pool",
         {HELD: "akshare_spot", POOL: "h5i_reference"}, "NORMAL"),
        ({POOL: quote(20)}, {HELD: 10}, "h5i_reference_held",
         {HELD: "h5i_reference", POOL: "akshare_spot"}, "DEGRADED"),
        ({}, {HELD: 10, POOL: 20}, "h5i_reference_held",
         {HELD: "h5i_reference", POOL: "h5i_reference"}, "DEGRADED"),
        ({HELD: quote(10, "h5i_reference"), POOL: quote(20)}, {},
         "h5i_reference_held", {HELD: "h5i_reference", POOL: "akshare_spot"},
         "DEGRADED"),
        ({HELD: quote(10), POOL: quote(20, "h5i_reference")}, {},
         "h5i_reference_pool", {HELD: "akshare_spot", POOL: "h5i_reference"},
         "NORMAL"),
        ({POOL: quote(20, "h5i_reference")}, {}, "price_missing_held",
         {HELD: "price_missing", POOL: "h5i_reference"}, "DEGRADED"),
        ({HELD: quote(10, "duckdb_reference"), POOL: quote(20, "h5i_reference")},
         {}, "duckdb_reference_held",
         {HELD: "duckdb_reference", POOL: "h5i_reference"}, "DEGRADED"),
        ({HELD: quote(10, "sina_spot"), POOL: quote(20, "sina_spot")}, {},
         "sina_spot", {HELD: "sina_spot", POOL: "sina_spot"}, "NORMAL"),
        ({HELD: quote(10) | {"fallback_source": "h5i_reference"}, POOL: quote(20)}, {},
         "h5i_reference_held", {HELD: "h5i_reference", POOL: "akshare_spot"},
         "DEGRADED"),
    ],
    ids=["all-live", "candidate-reference", "held-reference", "no-spot",
         "feed-already-fallback-held", "feed-already-fallback-pool",
         "missing-held-with-h5i-candidate", "mixed-reference-backends", "sina-live",
         "fallback-source-without-flag"],
)
def test_tick_persists_per_code_sources(runtime, monkeypatch, tmp_path,
                                        live, reference, expected, sources, health):
    monkeypatch.setattr(runtime.feed, "_fetch_spot_with_timeout", lambda: live)
    class ReferenceDB:
        def get_bars(self, canon, n):
            frame = pd.DataFrame({"close": [reference[canon]]}) if canon in reference else pd.DataFrame()
            frame.attrs["backend_source"] = "h5i"
            return frame

    monkeypatch.setattr(runtime, "_ref_db", ReferenceDB)
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == expected
    assert state.get("price_sources") == sources
    if any(source.endswith("reference") or source == "price_missing" for source in sources.values()):
        assert state["data_ts"] is None, "Reference prices must not advertise a realtime timestamp"
    else:
        assert state["data_ts"]
    assert assemble({"live_source": state["live_source"]})["state"] == health
    assert runtime.pb.d_price == {code: q["price"] for code, q in live.items()} | reference
    if sources[HELD] == "h5i_reference":
        assert runtime.feed.quotes[HELD]["fallback_source"] == "h5i_reference"


def test_reference_only_without_holdings_is_not_called_spot(runtime, monkeypatch, tmp_path):
    runtime.pb.positions = {}
    runtime.targets = [{"canon": POOL, "target_weight": 1.0}]
    monkeypatch.setattr(runtime.feed, "_fetch_spot_with_timeout",
                        lambda: {POOL: quote(20, "h5i_reference")})
    monkeypatch.setattr(runtime, "_disk_ref_prices", lambda _codes: {})
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == "h5i_reference_pool"
    assert state.get("price_sources") == {POOL: "h5i_reference"}


def test_cached_spot_keeps_fetch_time_and_cached_reference_keeps_source(runtime, monkeypatch, tmp_path):
    cached_at = datetime(2026, 10, 9, 10, 20)
    class FeedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 9, 10, 30)

    monkeypatch.setattr(PB, "datetime", FeedClock)
    runtime.feed.ttl = 3600
    runtime.feed._ts = cached_at.timestamp()
    runtime.feed.quotes = {HELD: quote(10), POOL: quote(20)}
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["data_ts"] == "2026-10-09 10:20:00"

    runtime.feed.quotes[HELD] = quote(10, "h5i_reference")
    runtime.run_tick(NOW)
    state = json.loads((tmp_path / "live_state.json").read_text(encoding="utf-8"))
    assert state["live_source"] == "h5i_reference_held"
    assert state["price_sources"][HELD] == "h5i_reference"
    assert state["data_ts"] is None


def test_unbounded_reference_still_uses_legacy_duckdb(monkeypatch):
    class LegacyDB:
        def execute(self, sql, params=None):
            self.sql = sql
            return self

        def fetchone(self):
            return ("2026-10-09",)

        def fetchdf(self):
            return pd.DataFrame({"symbol": ["600000"], "close": [10.0]})

        def close(self):
            pass

    def forbidden_h5i():
        raise AssertionError("All-market H5i fallback is excluded by the audit")

    monkeypatch.setattr(db, "StockDB", forbidden_h5i)
    monkeypatch.setattr("duckdb.connect", lambda *_args, **_kwargs: LegacyDB())
    result = PB.PriceFeed()._fetch_from_duckdb(symbols=None)
    assert set(result) == {HELD}
    assert result[HELD]["price"] == 10.0
    assert result[HELD]["price_source"] == "duckdb_reference"


def test_reference_only_source_label():
    assert RE._live_src_label([], [], reference_source="h5i_reference") == "h5i_reference"


def test_bounded_reference_uses_stockdb_without_changing_quote_metadata(monkeypatch):
    calls = []
    class ReferenceDB:
        def get_bars(self, canon, n):
            calls.append((canon, n))
            frame = pd.DataFrame({"close": [10.0]})
            frame.attrs["backend_source"] = "h5i"
            return frame

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(db, "StockDB", ReferenceDB)
    feed = PB.PriceFeed()
    result = feed._fetch_from_duckdb(symbols=[HELD])
    assert result == {HELD: {"price": 10.0, "last_close": 10.0,
                            "limit_up": 11.0, "limit_down": 9.0, "volume": 0,
                            "suspended": False, "fallback": True,
                            "fallback_source": "h5i_reference",
                            "price_source": "h5i_reference"}}
    assert calls == [(HELD, 1), "closed"]
    calls.clear()
    assert feed._fetch_from_duckdb(symbols=[]) == {}
    assert calls == []


def test_normal_spot_and_timeout_reference_stay_bounded(monkeypatch):
    akshare = SimpleNamespace(stock_zh_a_spot_em=lambda: pd.DataFrame({
        "代码": ["600000"], "最新价": [10], "昨收": [9], "成交量": [100],
    }))
    monkeypatch.setitem(sys.modules, "akshare", akshare)
    feed = PB.PriceFeed()
    feed.set_snapshot_targets([{"canon": POOL}])
    feed.set_positions({HELD: {}})
    monkeypatch.setattr(feed, "_fetch_sina_spot", lambda _codes: {})
    requests = []
    def references(symbols=None):
        requests.append(symbols)
        assert symbols is not None, "Tick fallback must not enumerate the market"
        return {c: quote(20, "h5i_reference") for c in symbols}
    monkeypatch.setattr(feed, "_fetch_from_duckdb", references)
    snap = feed._fetch_spot()
    assert set(snap) == {HELD, POOL}
    assert snap[HELD].get("price_source") == "akshare_spot"
    assert snap[POOL]["fallback_source"] == "h5i_reference"
    assert requests == [[POOL]]

    requests.clear()
    def unavailable(_fetch_all):
        raise OSError("spot unavailable")
    monkeypatch.setattr(feed, "_fetch_spot", unavailable)
    fallback = feed._fetch_spot_with_timeout()
    assert set(fallback) == {HELD, POOL}
    assert requests == [sorted([HELD, POOL])]


def test_h5i_held_reference_health():
    assert assemble({"live_source": "h5i_reference_held"})["state"] == "DEGRADED"


def test_h5i_held_reference_watchdog_attribution():
    sample = {"in_session": True, "pid_alive": True, "tick": 11, "prev_tick": 10,
              "updated": (NOW - timedelta(seconds=600)).strftime("%Y-%m-%d %H:%M:%S"),
              "feed_error": "", "live_source": "h5i_reference_held"}
    result = evaluate(sample, now=NOW.replace(tzinfo=None))
    assert (result["level"], result["cause"]) == ("CRITICAL", "feed_stale")


@pytest.fixture
def receipt(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    data = root / "data"
    data.mkdir(parents=True)
    monkeypatch.setattr(RD, "__file__", str(root / "src" / "run_daily.py"))
    monkeypatch.setattr(RD, "DATA_DIR", str(data))
    snapshot = build_snapshot(DAY, TARGETS, "selection_same_day", DAY, [], NOW, str(root))
    path = write_snapshot(str(data), snapshot)
    live = {"day": "2026-10-09", "snapshot_status": "ready", "snapshot_ref": path,
            "snapshot_hash": snapshot["snapshot_hash"],
            "positions": [{"canon": HELD}, {"canon": EXTRA}],
            "capital": {"equity": 10000, "market_value": 4000,
                        "cash": 6000, "cash_ratio": 0.6}}
    # A valid-looking shadow DRL plan must never rescue a failed receipt.
    plan = data / "drl" / DAY / "target_plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"top_n": [{"canon": EXTRA}]}), encoding="utf-8")
    return data, live, snapshot


def report_receipt(data, live):
    (data / "live_state.json").write_text(json.dumps(live), encoding="utf-8")
    return RD.portfolio_construction()


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-ref", "relative-ref"])
def test_matching_authoritative_snapshot_reports_construction(receipt, relative):
    data, live, _snapshot = receipt
    if relative:
        live["snapshot_ref"] = f"data/daily/{DAY}/signal_snapshot_{DAY}.json"
    result = report_receipt(data, live)
    assert result.get("ok") is True, result
    assert result["source"] == "signal_snapshot"
    assert (result["target_count"], result["current_count"], result["pending_buys"]) == (2, 1, 1)
    assert result["pending_list"] == [POOL]
    assert result["extra_holdings"] == [EXTRA]
    assert result["deployment_ratio"] == 0.4


@pytest.mark.parametrize("failure", ["advertised-mismatch", "missing-hash", "stale-embedded-hash",
    "wrong-snapshot-day", "wrong-live-day", "pending-status", "missing-status",
    "missing-ref", "missing-file", "non-authoritative-ref", "invalid-schema"])
def test_invalid_snapshot_blocks_even_with_drl_plan(receipt, failure):
    data, live, snapshot = receipt
    path = live["snapshot_ref"]
    if failure == "advertised-mismatch":
        live["snapshot_hash"] = "f" * 64
    elif failure == "missing-hash":
        live.pop("snapshot_hash")
    elif failure == "stale-embedded-hash":
        snapshot["targets"][0]["canon"] = EXTRA
    elif failure == "wrong-snapshot-day":
        snapshot = build_snapshot("20261008", TARGETS, "selection_same_day", DAY,
                                  [], NOW, str(data.parent))
        live["snapshot_hash"] = snapshot["snapshot_hash"]
    elif failure == "wrong-live-day":
        live["day"] = "2026-10-08"
    elif failure == "pending-status":
        live["snapshot_status"] = "pending"
    elif failure == "missing-status":
        live.pop("snapshot_status")
    elif failure == "missing-ref":
        live.pop("snapshot_ref")
    elif failure == "missing-file":
        os.remove(path)
    elif failure == "non-authoritative-ref":
        live["snapshot_ref"] = str(data / "copy.json")
        (data / "copy.json").write_text(json.dumps(snapshot), encoding="utf-8")
    elif failure == "invalid-schema":
        snapshot["schema_version"] = 999
    if failure not in {"missing-file"}:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream)
    if failure == "stale-embedded-hash":
        assert read_snapshot(str(data), DAY)["status"] == "tampered"
    result = report_receipt(data, live)
    assert result.get("ok") is False, result
    assert result.get("error")
    assert "source" not in result
    assert "target_count" not in result


def test_embedded_live_targets_remain_the_reported_targets(receipt):
    data, live, _snapshot = receipt
    live["targets"] = [{"canon": EXTRA}]
    live["snapshot_status"] = "invalid"
    result = report_receipt(data, live)
    assert result["source"] == "live_state.targets"
    assert (result["target_count"], result["current_count"], result["pending_buys"]) == (1, 1, 0)

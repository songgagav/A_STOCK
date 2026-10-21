from __future__ import annotations

import builtins
from datetime import date, timedelta
import sys
from types import SimpleNamespace

import pandas as pd
import pytest


def _api():
    import importlib.util

    spec = importlib.util.find_spec("h5i_selector_ic")
    assert spec is not None, "read-only H5i selector IC adapter is not implemented"
    import h5i_selector_ic

    return h5i_selector_ic


def _require(name):
    api = _api()
    assert hasattr(api, name), f"H5i selector IC adapter is missing {name}"
    return getattr(api, name)


class _Query:
    def __init__(self, frame):
        self.frame = frame

    def to_pandas(self):
        return self.frame.copy()


class _FakeDatabase:
    def __init__(self, frames=None):
        self.frames = frames or {}
        self.queries = []
        self.sql_options = []
        self.closed = False

    def sql(self, query, **options):
        self.queries.append(query)
        self.sql_options.append(options)
        if query == "SHOW TABLES":
            return _Query(pd.DataFrame({"name": ["daily_bars", "valuation", "financials"]}))
        if query.startswith("DESCRIBE "):
            table = query.split()[-1]
            columns = {
                "daily_bars": ["ts", "symbol", "open", "high", "low", "close",
                               "volume", "amount", "change_pct", "turnover"],
                "valuation": ["ts", "symbol", "pb"],
                "financials": ["ts", "symbol", "roe"],
            }.get(table, [])
            return _Query(pd.DataFrame({"column_name": columns}))
        for prefix, frame in self.frames.items():
            if query.startswith(prefix):
                return _Query(frame)
        raise AssertionError(f"unexpected SQL: {query}")

    def close(self):
        self.closed = True


def test_h5i_source_opens_explicit_database_read_only_and_closes_it():
    source_type = _require("H5iReadOnlySource")
    db = _FakeDatabase()
    calls = []

    def factory(path, read_only=False):
        calls.append((path, read_only))
        return db

    with source_type("C:/private/market.db", database_factory=factory) as source:
        schema = source.schema()
        assert "ts" in schema["valuation"]

    assert calls == [("C:/private/market.db", True)]
    assert db.closed is True


def test_schema_uses_h5i_native_read_only_schema_api_when_available():
    source_type = _require("H5iReadOnlySource")

    class NativeSchema:
        def __init__(self, names):
            self.names = names

    class NativeMetadataDatabase(_FakeDatabase):
        def __init__(self):
            super().__init__()
            self.native_schema_calls = []

        def tables(self):
            return ["daily_bars", "valuation", "financials"]

        def schema(self, table):
            self.native_schema_calls.append(table)
            columns = {
                "daily_bars": ["ts", "symbol", "open", "close", "change_pct"],
                "valuation": ["ts", "symbol", "pb"],
                "financials": ["ts", "symbol", "roe"],
            }
            return NativeSchema(columns[table])

    db = NativeMetadataDatabase()
    with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
        schema = source.schema()

    assert schema["daily_bars"] == ["ts", "symbol", "open", "close", "change_pct"]
    assert set(schema) == {"daily_bars", "valuation", "financials"}
    assert db.native_schema_calls == ["daily_bars", "financials", "valuation"]
    assert db.queries == []
    assert db.closed is True


def test_h5i_daily_bar_query_is_bounded_and_preserves_calendar_session_date():
    source_type = _require("H5iReadOnlySource")
    frame = pd.DataFrame({
        "session_date": [date(2026, 10, 20)],
        "symbol": ["600000"],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100.0], "amount": [1000.0], "change_pct": [5.0],
        "turnover": [1.0],
    })
    db = _FakeDatabase({"SELECT CAST(ts AS DATE) AS session_date": frame})
    with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
        result = source.bars_for_sessions("2026-10-19", "2026-10-21")

    query = next(q for q in db.queries if q.startswith("SELECT CAST(ts AS DATE) AS session_date"))
    assert "ts >= TIMESTAMP '2026-10-19 00:00:00'" in query
    assert "ts < TIMESTAMP '2026-10-22 00:00:00'" in query
    assert "CAST(ts AS DATE) >= " not in query
    options = db.sql_options[db.queries.index(query)]
    assert options == {"timeout": 60.0, "max_rows": 1_000_000}
    options = db.sql_options[db.queries.index(query)]
    assert options["timeout"] == 60.0
    assert options["max_rows"] == 1_000_000
    assert result.iloc[0]["session_date"] == date(2026, 10, 20)
    assert result.iloc[0]["symbol"] == "600000"


def test_h5i_observed_sessions_are_bounded_and_sorted():
    source_type = _require("H5iReadOnlySource")
    assert hasattr(source_type, "trading_sessions")
    trading_sessions = source_type.trading_sessions
    frame = pd.DataFrame({
        "session_date": [date(2026, 10, 21), date(2026, 10, 20)],
    })
    db = _FakeDatabase({
        "SELECT DISTINCT CAST(ts AS DATE) AS session_date FROM daily_bars": frame,
    })
    with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
        result = trading_sessions(source, "2026-10-20", "2026-10-21")

    assert result == ["2026-10-20", "2026-10-21"]
    query = next(q for q in db.queries if q.startswith("SELECT DISTINCT"))
    assert "ts >= TIMESTAMP '2026-10-20 00:00:00'" in query
    assert "ts < TIMESTAMP '2026-10-22 00:00:00'" in query


def test_valuation_asof_keeps_source_ts_and_excludes_late_or_date_only_signal_day_rows():
    source_type = _require("H5iReadOnlySource")
    frame = pd.DataFrame({
        "ts": ["2026-10-21 09:25:00", "2026-10-21 09:25:01", "2026-10-21",
               "2026-10-20"],
        "symbol": ["600000", "000001", "000002", "000003"],
        "pb": [1.1, 1.2, 1.3, 1.4],
    })
    db = _FakeDatabase({"SELECT ts, symbol, pb": frame})
    with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
        rows = source.valuation_asof(_api().signal_cutoff("2026-10-21"), ["600000", "000003"])

    assert set(rows["symbol"]) == {"600000", "000003"}
    assert "ts" in rows.columns
    assert rows.set_index("symbol").loc["600000", "pb"] == 1.1
    assert any("TIMESTAMP '2026-10-21 09:25:00'" in q for q in db.queries)
    query = next(q for q in db.queries if q.startswith("SELECT ts, symbol, pb"))
    assert "symbol IN ('000003','600000')" in query
    assert "CAST(ts AS DATE) >= " not in query


def test_valuation_symbol_filter_rejects_sql_fragments_before_query():
    source_type = _require("H5iReadOnlySource")
    db = _FakeDatabase({"SELECT ts, symbol, pb": pd.DataFrame()})
    with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
        with pytest.raises(_require("H5iInputError"), match="symbol"):
            source.valuation_asof(_api().signal_cutoff("2026-10-21"), ["600000' OR 1=1--"])
    assert not any(q.startswith("SELECT ts, symbol, pb") for q in db.queries)


def test_h5i_source_closes_connection_when_query_raises():
    source_type = _require("H5iReadOnlySource")

    class BrokenDatabase(_FakeDatabase):
        def sql(self, query):
            raise RuntimeError("read failed")

    db = BrokenDatabase()
    with pytest.raises(RuntimeError, match="read failed"):
        with source_type("fake.db", database_factory=lambda *_a, **_k: db) as source:
            source.schema()
    assert db.closed is True


def test_h5i_schema_fails_closed_when_required_valuation_timestamp_is_missing():
    source_type = _require("H5iReadOnlySource")

    class MissingTimestampDatabase(_FakeDatabase):
        def sql(self, query):
            if query == "SHOW TABLES":
                return _Query(pd.DataFrame({"name": ["daily_bars", "valuation"]}))
            if query == "DESCRIBE valuation":
                return _Query(pd.DataFrame({"column_name": ["symbol", "pb"]}))
            return super().sql(query)

    with source_type("fake.db", database_factory=lambda *_a, **_k: MissingTimestampDatabase()) as source:
        with pytest.raises(_require("H5iSchemaError")):
            source.schema()


def test_missing_h5i_dependency_surfaces_without_legacy_duckdb_fallback(monkeypatch, tmp_path):
    source_type = _require("H5iReadOnlySource")
    original_import = builtins.__import__
    seen = []

    def controlled_import(name, *args, **kwargs):
        if name == "h5i_db":
            raise ModuleNotFoundError("h5i_db intentionally unavailable")
        if name == "duckdb":
            seen.append(name)
            raise AssertionError("legacy DuckDB fallback must not be attempted")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", controlled_import)
    db_path = tmp_path / "market.db"
    db_path.write_bytes(b"test-only empty placeholder")
    with pytest.raises(ModuleNotFoundError, match="h5i_db intentionally unavailable"):
        with source_type(str(db_path)):
            pass
    assert seen == []


def test_default_factory_accepts_directory_backed_h5i_and_still_opens_read_only(monkeypatch, tmp_path):
    source_type = _require("H5iReadOnlySource")
    db_path = tmp_path / "market.db"
    db_path.mkdir()
    (db_path / "catalog").mkdir()
    db = _FakeDatabase()
    calls = []

    def database(path, *, read_only):
        calls.append((path, read_only))
        return db

    monkeypatch.setitem(sys.modules, "h5i_db", SimpleNamespace(Database=database))
    with source_type(str(db_path)) as source:
        assert source.schema()["daily_bars"]

    assert calls == [(str(db_path), True)]
    assert db.closed is True


def test_diagnostic_panels_use_prior_session_universe_and_keep_roe_mf_unsupported():
    builder = _require("build_diagnostic_panels")
    sessions = []
    cursor = date(2026, 9, 1)
    while len(sessions) < 42:
        if cursor.weekday() < 5:
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    signal_index = 21
    signal_day = sessions[signal_index]
    prior_day = sessions[signal_index - 1]
    symbols = [f"60000{i}" for i in range(5)]
    rows = []
    for i, symbol in enumerate(symbols):
        for idx, session in enumerate(sessions):
            close = float(100 + i) if idx < signal_index else float(100 + i)
            if idx == signal_index - 1:
                close = float(100 + i) * (1.0 + i / 100.0)
            rows.append({
                "session_date": date.fromisoformat(session), "symbol": symbol,
                "open": close, "high": close, "low": close, "close": close,
                "volume": 1000.0, "amount": 10000.0,
                "change_pct": float(i), "turnover": 1.0,
            })
    # This name exists on the signal date but was not in the prior-session universe.
    rows.append({
        "session_date": date.fromisoformat(signal_day), "symbol": "999999",
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 10.0, "amount": 100.0, "change_pct": 0.0, "turnover": 1.0,
    })
    bars = pd.DataFrame(rows)

    class FakePanelSource:
        def __init__(self):
            self.valuation_cutoffs = []

        def schema(self):
            return {
                "daily_bars": ["ts", "symbol", "open", "high", "low", "close",
                               "volume", "amount", "change_pct", "turnover"],
                "valuation": ["ts", "symbol", "pb"],
                "financials": ["ts", "symbol", "roe"],
            }

        def bars_for_sessions(self, start, end):
            mask = bars["session_date"].astype(str).between(start, end)
            return bars.loc[mask].copy()

        def valuation_asof(self, cutoff, requested_symbols=None):
            self.valuation_cutoffs.append(cutoff)
            return pd.DataFrame({
                "ts": [f"{signal_day} 09:25:01"] +
                      [f"{signal_day} 09:25:00"] * (len(symbols) - 1),
                "symbol": symbols,
                "pb": [0.8 + i * 0.2 for i in range(len(symbols))],
            })

    source = FakePanelSource()
    panels = builder(source, sessions, signal_day, signal_day)

    mom = panels["factor_values"].query("signal_date == @signal_day and factor == 'mom_rev'")
    assert set(mom["symbol"]) == set(symbols)
    assert set(mom["last_closed_bar_date"]) == {prior_day}
    assert "999999" not in set(panels["factor_values"]["symbol"])
    pb_rows = panels["factor_values"].query("signal_date == @signal_day and factor == 'pb_rev'")
    assert "source_age_days" in pb_rows.columns
    assert set(pb_rows.query("status == 'ok'")["source_age_days"]) == {0.0}
    late_pb = pb_rows.query("symbol == @symbols[0]").iloc[0]
    assert late_pb["status"] == "missing"
    assert pd.isna(late_pb["value"])
    unsupported = panels["ic"].query("factor in ['roe', 'mf_net']")
    assert not unsupported.empty
    assert set(unsupported["status"]) == {"unsupported"}
    assert unsupported["ic"].isna().all()
    unsupported_h20 = unsupported.query("horizon == 20")
    assert set(unsupported_h20["label_valid_count"]) == {5}
    mom_ic = panels["ic"].query("signal_date == @signal_day and factor == 'mom_rev'")
    assert set(mom_ic["expected_direction"]) == {-1}
    assert set(mom_ic["orientation"]) == {"raw_mom_20"}
    assert set(panels["labels"].query("horizon == 20")["settlement_date"]) == {
        sessions[signal_index + 19]
    }
    assert source.valuation_cutoffs[0].strftime("%H:%M:%S") == "09:25:00"


def test_diagnostic_panel_rejects_duplicate_calendar_sessions():
    builder = _require("build_diagnostic_panels")
    error_type = _require("H5iInputError")

    with pytest.raises(error_type, match="unique"):
        builder(object(), ["2026-10-20", "2026-10-20"], "2026-10-20", "2026-10-20")


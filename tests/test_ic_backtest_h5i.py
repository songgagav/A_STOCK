from datetime import date, datetime, timedelta

import pytest

import ic_backtest
import ic_curve_refresh


def _write_h5i_daily_bars(path):
    h5i_db = pytest.importorskip("h5i_db")
    import pyarrow as pa

    symbols = [f"S{i}" for i in range(6)]
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(12)]
    rows = [
        (datetime.combine(day, datetime.min.time()), symbol, float((i + 1) * (j + 1)))
        for i, day in enumerate(days)
        for j, symbol in enumerate(symbols)
    ]
    schema = pa.schema([
        pa.field("ts", pa.timestamp("us")),
        pa.field("symbol", pa.string()),
        pa.field("change_pct", pa.float64()),
    ])
    db = h5i_db.Database(str(path), create=True)
    try:
        db.create_table("daily_bars", schema, time_column="ts")
        db.append(
            "daily_bars",
            pa.Table.from_pydict(
                {
                    "ts": [row[0] for row in rows],
                    "symbol": [row[1] for row in rows],
                    "change_pct": [row[2] for row in rows],
                },
                schema=schema,
            ),
        )
    finally:
        db.close()


def test_run_uses_h5i_when_legacy_duckdb_is_absent(tmp_path, monkeypatch):
    h5i_path = tmp_path / "market.db"
    _write_h5i_daily_bars(h5i_path)
    monkeypatch.setattr(ic_backtest, "DUCKDB_PATH", str(tmp_path / "missing.duckdb"))
    monkeypatch.setattr(ic_backtest, "H5I_PATH", str(h5i_path), raising=False)

    series, curve_file = ic_backtest.run(
        "2026-06-01", "2026-06-12", 3, [1], "mom_3", use_adj=False,
        write_curve=False,
    )

    assert curve_file.endswith("ic_curve_mom_3_k3.csv")
    assert not series[1].dropna().empty


def test_ic_refresh_bounds_are_read_from_h5i(tmp_path, monkeypatch):
    h5i_path = tmp_path / "market.db"
    _write_h5i_daily_bars(h5i_path)
    monkeypatch.setattr(ic_backtest, "H5I_PATH", str(h5i_path))
    monkeypatch.setattr(ic_backtest, "DUCKDB_PATH", str(tmp_path / "missing.duckdb"))
    monkeypatch.delenv("BAR_STORE", raising=False)

    assert str(ic_curve_refresh._latest_settled_day())[:10] == "2026-06-12"
    assert str(ic_curve_refresh._window_start("2026-06-12", days=5))[:10] == "2026-06-08"

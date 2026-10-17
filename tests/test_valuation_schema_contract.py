# -*- coding: utf-8 -*-
"""Valuation snapshot merges must preserve the live h5i provenance schema."""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import valuation_backfill as VB  # noqa: E402


@pytest.mark.parametrize("include_original_source", [True, False])
def test_snapshot_merge_accepts_live_valuation_schema(
        tmp_path, monkeypatch, include_original_source):
    h5i_db = pytest.importorskip("h5i_db")
    pa = pytest.importorskip("pyarrow")

    db = h5i_db.Database(str(tmp_path / "market.db"), create=True)
    snapshot_schema = pa.schema([
        ("ts", pa.timestamp("us")), ("symbol", pa.string()),
        ("name", pa.string()), ("price", pa.float64()),
        ("pe_ttm", pa.float64()), ("pb", pa.float64()),
        ("total_mv", pa.float64()), ("float_mv", pa.float64()),
        ("amount", pa.float64()), ("float_shares", pa.float64()),
        ("total_shares", pa.float64()), ("is_st", pa.string()),
        ("turnover", pa.float64()),
    ])
    valuation_fields = [
        ("ts", pa.timestamp("us")), ("symbol", pa.string()),
        ("pe_ttm", pa.float64()), ("pb", pa.float64()),
        ("ps_ttm", pa.float64()), ("pcf_ncf_ttm", pa.float64()),
        ("is_st", pa.bool_()), ("original_source", pa.string()),
        ("market_cap", pa.float64()), ("free_cap", pa.float64()),
        ("float_shares", pa.float64()), ("source", pa.string()),
    ]
    if not include_original_source:
        valuation_fields = [
            field for field in valuation_fields if field[0] != "original_source"]
    valuation_schema = pa.schema(valuation_fields)
    db.create_table("valuation_snapshot", snapshot_schema, time_column="ts")
    db.create_table("valuation", valuation_schema, time_column="ts")
    try:
        snapshot = pa.Table.from_pylist([{
            "ts": dt.datetime(2026, 10, 16, 15),
            "symbol": "600519", "name": "sample", "price": 1329.0,
            "pe_ttm": 20.18, "pb": 6.61, "total_mv": 16614.0,
            "float_mv": 16500.0, "amount": 1000000.0,
            "float_shares": None, "total_shares": None,
            "is_st": "", "turnover": 0.31,
        }], schema=snapshot_schema)
        db.append("valuation_snapshot", snapshot)

        class _DatabaseProxy:
            def sql(self, query):
                return db.sql(query)

            def schema(self, name):
                return db.schema(name)

            def append(self, name, table):
                return db.append(name, table)

            def close(self):
                # The test fixture owns and closes db.
                pass

        monkeypatch.setattr(VB, "_open_db", lambda: _DatabaseProxy())
        result = VB.merge_snapshot_to_valuation(day="2026-10-16")

        assert result["ok"] is True, result
        assert result["merged"][0]["appended"] == 1, result
        select = "source, original_source" if include_original_source else "source"
        row = db.sql(
            f"SELECT {select} FROM valuation "
            "WHERE CAST(ts AS DATE)=DATE '2026-10-16' AND symbol='600519'"
        ).to_pandas().iloc[0]
        assert row["source"] == "snapshot"
        if include_original_source:
            assert row["original_source"] == "snapshot"
    finally:
        db.close()


def test_rebuild_preserves_existing_original_source_column(tmp_path, monkeypatch):
    h5i_db = pytest.importorskip("h5i_db")
    pa = pytest.importorskip("pyarrow")

    db = h5i_db.Database(str(tmp_path / "rebuild.db"), create=True)
    schema = pa.schema([
        ("ts", pa.timestamp("us")), ("symbol", pa.string()),
        ("pe_ttm", pa.float64()), ("pb", pa.float64()),
        ("ps_ttm", pa.float64()), ("pcf_ncf_ttm", pa.float64()),
        ("is_st", pa.bool_()), ("original_source", pa.string()),
        ("market_cap", pa.float64()), ("free_cap", pa.float64()),
        ("float_shares", pa.float64()), ("source", pa.string()),
    ])
    db.create_table("valuation", schema, time_column="ts")
    try:
        db.append("valuation", pa.Table.from_pylist([{
            "ts": dt.datetime(2026, 9, 8), "symbol": "600519",
            "pe_ttm": 20.18, "pb": 6.61, "ps_ttm": None,
            "pcf_ncf_ttm": None, "is_st": False,
            "original_source": "snapshot", "market_cap": 1.0,
            "free_cap": 1.0, "float_shares": None, "source": "snapshot",
        }], schema=schema))

        class _DatabaseProxy:
            def __getattr__(self, name):
                return getattr(db, name)

            def close(self):
                # The test fixture owns and closes db.
                pass

        monkeypatch.setattr(VB, "_open_db", lambda: _DatabaseProxy())
        result = VB.rebuild_valuation()

        assert result["ok"] is True, result
        assert db.schema("valuation").names == schema.names
        row = db.sql(
            "SELECT source, original_source FROM valuation WHERE symbol='600519'"
        ).to_pandas().iloc[0]
        assert row["source"] == "snapshot"
        assert row["original_source"] == "snapshot"
    finally:
        db.close()


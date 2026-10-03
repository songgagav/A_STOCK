# -*- coding: utf-8 -*-
"""仪表台第一批契约：冻结状态可见、表名边界明确。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import dashboard  # noqa: E402
from signal_snapshot import build_snapshot, write_snapshot  # noqa: E402


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _snapshot() -> dict:
    return build_snapshot(
        "20261003",
        [{"canon": "600000.SH", "target_weight": 1.0}],
        "drl_same_day",
        "20261002",
        [],
        datetime(2026, 10, 3, 9, 25, tzinfo=_SHANGHAI),
        _REPO,
    )


def test_signal_freeze_status_reports_missing_snapshot_and_shadow_mode(tmp_path, monkeypatch):
    """L1 缺失必须在看板上成为显式状态，不能伪装成正常。"""
    monkeypatch.setattr(dashboard, "DATA_DIR", str(tmp_path))

    result = dashboard.read_signal_freeze_status("20261003")

    assert result["ok"] is True
    assert result["status"] == "missing"
    assert result["mode"] == "shadow"
    assert result["late_count"] == 0
    assert result["snapshot_hash"] is None


def test_signal_freeze_status_reads_verified_snapshot_and_late_count(tmp_path, monkeypatch):
    """看板只展示已验证快照，并把迟到归档数量暴露给操作员。"""
    monkeypatch.setattr(dashboard, "DATA_DIR", str(tmp_path))
    write_snapshot(str(tmp_path), _snapshot())
    late_path = Path(tmp_path) / "daily" / "20261003" / "late_signals_20261003.json"
    late_path.write_text(
        json.dumps({"schema_version": 1, "date": "20261003", "items": [{"candidate_id": "c1"}, {"candidate_id": "c2"}]}),
        encoding="utf-8",
    )

    result = dashboard.read_signal_freeze_status("20261003")

    assert result["status"] == "ready"
    assert result["is_today"] is True
    assert result["generated_at"] == "2026-10-03T09:25:00+08:00"
    assert result["snapshot_hash"] == _snapshot()["snapshot_hash"]
    assert result["late_count"] == 2
    assert result["unexplained_count"] == 0


@pytest.mark.parametrize(
    "name",
    [
        "DAILY_BARS",
        "daily_bars/../valuation",
        "../config",
        "daily_bars%2F..%2Fvaluation",
        "daily_bars; DROP TABLE daily_bars;",
        "daily_bars' OR 1=1 --",
        "",
        "x" * 10000,
    ],
)
def test_dashboard_table_allowlist_rejects_ambiguous_or_injected_names(name):
    """db_table 只接受精确白名单，大小写、路径和 SQL 片段均不得绕过。"""
    assert dashboard.validate_dashboard_table_name(name) is None


def test_dashboard_table_allowlist_accepts_canonical_name():
    assert dashboard.validate_dashboard_table_name("daily_bars") == "daily_bars"


def test_dashboard_page_contains_freeze_rail_contract():
    """视觉层必须保留冻结状态的可见锚点，避免后端已接入但页面无入口。"""
    for marker in ("freezeRail", "freezeStatus", "freezeLate", "freezeUnexplained", "/api/signal-freeze"):
        assert marker in dashboard.PAGE


def test_fallback_state_uses_source_timestamp_not_now(tmp_path, monkeypatch):
    """离线回退必须暴露原始状态时间，不能把旧数据伪装成刚更新。"""
    monkeypatch.setattr(dashboard, "DATA_DIR", str(tmp_path))
    state_path = Path(tmp_path) / "state.json"
    state_path.write_text(
        json.dumps({"day": "20261002", "equity": 100000, "cash": 100000, "positions": {}}),
        encoding="utf-8",
    )
    source_epoch = datetime(2026, 10, 2, 15, 0, tzinfo=_SHANGHAI).timestamp()
    os.utime(state_path, (source_epoch, source_epoch))

    result = dashboard.fallback_state()

    assert result["updated"] == "2026-10-02 15:00:00"
    assert result["stale"] is True


def test_missing_h5i_dependency_degrades_without_raising(tmp_path, monkeypatch):
    """缺 h5i_db 时接口应返回可渲染的空源状态，不能炸掉 HTTP 工作线程。"""
    monkeypatch.setattr(dashboard, "_H5I_PATH", str(tmp_path))
    monkeypatch.setitem(sys.modules, "h5i_db", None)

    assert dashboard._h5i_store() is None


def test_source_etag_is_stable_and_changes_with_source_file(tmp_path):
    """实时源未变化时可复用 304，源文件变化后必须生成新标识。"""
    source = Path(tmp_path) / "live_state.json"
    source.write_text("one", encoding="utf-8")
    first = dashboard.source_etag(str(source))
    assert first
    assert dashboard.source_etag(str(source)) == first

    source.write_text("two", encoding="utf-8")
    os.utime(source, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_001))
    assert dashboard.source_etag(str(source)) != first
    assert dashboard.source_etag(str(tmp_path / "missing.json")) is None


def test_named_parquet_rows_follow_requested_columns_not_file_order(tmp_path, monkeypatch):
    """物化视图列顺序变化时，读取结果仍按字段名对齐。"""
    import duckdb

    parquet_dir = Path(tmp_path) / "views" / "parquet"
    parquet_dir.mkdir(parents=True)
    parquet_path = parquet_dir / "v_market_breadth.parquet"
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "COPY (SELECT 2 AS n_down, '2026-10-03' AS date, 3 AS n_up) "
            "TO ? (FORMAT PARQUET)",
            [str(parquet_path)],
        )
    finally:
        con.close()

    monkeypatch.setattr(dashboard, "VIEWS_PARQUET", str(parquet_dir))

    rows = dashboard._read_view_parquet_named(
        "v_market_breadth.parquet",
        cols=["date", "n_up", "n_down"],
    )

    assert rows == [{"date": "2026-10-03", "n_up": 3, "n_down": 2}]


def test_named_parquet_rows_return_none_when_required_column_is_missing(tmp_path, monkeypatch):
    """物化快照缺列时必须触发上游 fallback，不能返回错位数据。"""
    import duckdb

    parquet_dir = Path(tmp_path) / "views" / "parquet"
    parquet_dir.mkdir(parents=True)
    parquet_path = parquet_dir / "v_factor_ic_latest.parquet"
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            "COPY (SELECT 'roe' AS factor, 0.1 AS ic_value) TO ? (FORMAT PARQUET)",
            [str(parquet_path)],
        )
    finally:
        con.close()

    monkeypatch.setattr(dashboard, "VIEWS_PARQUET", str(parquet_dir))

    rows = dashboard._read_view_parquet_named(
        "v_factor_ic_latest.parquet",
        cols=["factor", "ic_value", "mean_20"],
    )

    assert rows is None


def test_named_duckdb_rows_use_the_same_field_contract(monkeypatch):
    """Parquet 与 DuckDB fallback 必须向 read_regime 提供同一套字段名。"""
    monkeypatch.setattr(
        dashboard,
        "_duckdb_query",
        lambda sql, params=None: [("2026-10-03", 3, 2)],
    )

    rows = dashboard._duckdb_query_named(
        "SELECT date, n_up, n_down FROM v_market_breadth",
        cols=["date", "n_up", "n_down"],
    )

    assert rows == [{"date": "2026-10-03", "n_up": 3, "n_down": 2}]


def test_named_rows_reject_width_mismatch_instead_of_silently_truncating():
    """字段数不一致时必须触发 fallback，不能静默丢列。"""
    assert dashboard._rows_to_named([("2026-10-03", 3)], ["date", "n_up", "n_down"]) is None


def test_read_regime_consumes_named_rows_from_both_materialized_views(monkeypatch):
    """read_regime 不应再通过位置索引读取宽度和因子 IC。"""
    def named_reader(name, order_by="", cols=None):
        if name == "v_market_breadth.parquet":
            return [{
                "date": "2026-10-03", "total": 2, "n_up": 1, "n_down": 1,
                "n_limit_up": 0, "n_limit_dn": 0, "total_amount": 10.0,
                "avg_change_pct": 0.0, "breadth_ratio": 0.0,
            }]
        return [{
            "factor": "roe", "ic_value": 0.1, "mean_20": 0.1,
            "std_20": 0.2, "icir_20": 0.5, "win_rate_20": 0.6,
            "n_days": 20,
        }]

    monkeypatch.setattr(dashboard, "_read_view_parquet_named", named_reader)

    result = dashboard.read_regime()

    assert result["components"]["breadth_source"] == "parquet"
    assert result["components"]["breadth"][0]["n_up"] == 1
    assert result["components"]["factor_ic"][0]["factor"] == "roe"

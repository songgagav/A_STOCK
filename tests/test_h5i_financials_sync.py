from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _api():
    script = SCRIPTS / "h5i_financials_sync.py"
    assert script.is_file(), "tested H5i financial sync is not implemented"
    import h5i_financials_sync
    return h5i_financials_sync


def _provider_row(period="2026-09-30"):
    return pd.DataFrame([{
        "报告期": period,
        "营业总收入": "123.45亿",
        "营业总收入同比增长率": "10.00%",
        "净利润": "12.5亿",
        "净利润同比增长率": "-2.50%",
        "扣非净利润": "10.5亿",
        "扣非净利润同比增长率": "1.25%",
        "基本每股收益": "0.52",
        "每股净资产": "4.2",
        "每股资本公积金": "0.5",
        "每股未分配利润": "2.4",
        "每股经营现金流": "0.21",
        "销售净利率": "12.00%",
        "净资产收益率": "8.00%",
        "净资产收益率-摊薄": "7.50%",
        "资产负债率": "45.00%",
        "流动比率": "1.8",
        "速动比率": "1.2",
        "保守速动比率": "0.9",
        "产权比率": "0.8",
        "营业周期": "65.2",
        "应收账款周转天数": "30.1",
        "销售毛利率": "33.30%",
        "存货周转率": "4.8",
        "存货周转天数": "72.0",
    }])


def test_normalize_ths_rows_to_h5i_schema_and_units():
    api = _api()
    row = api.normalize_provider_frame("000001", _provider_row()).iloc[0]

    assert str(row["ts"])[:10] == "2026-09-30"
    assert row["symbol"] == "000001"
    assert row["revenue"] == 12_345_000_000
    assert row["net_profit"] == 1_250_000_000
    assert row["rev_yoy"] == 0.1
    assert row["roe"] == 0.08
    assert row["debt_ratio"] == 0.45
    assert row["source"] == "ak_stock_financial_abstract_ths"


def test_batch_only_appends_complete_expected_period_after_h5i_watermark():
    api = _api()
    fetched = {
        "000001": _provider_row(),
        "600519": _provider_row(),
    }

    result = api.prepare_append_batch(
        fetched,
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        minimum_coverage=1.0,
    )

    assert result["status"] == "ready"
    assert result["coverage"] == 1.0
    assert result["rows"].shape[0] == 2
    assert set(result["rows"]["symbol"]) == {"000001", "600519"}
    assert set(result["rows"]["ts"].dt.strftime("%Y-%m-%d")) == {"2026-09-30"}


def test_incomplete_provider_period_is_pending_and_writes_no_partial_rows():
    api = _api()
    fetched = {
        "000001": _provider_row(),
        "600519": _provider_row("2026-06-30"),
        "300750": None,
    }

    result = api.prepare_append_batch(
        fetched,
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        minimum_coverage=1.0,
    )

    assert result["status"] == "pending"
    assert result["rows"].empty
    assert result["missing_period_symbols"] == ["600519"]
    assert result["failed_symbols"] == ["300750"]


def test_normalizer_drops_invalid_report_dates_and_preserves_nulls():
    api = _api()
    raw = pd.concat([
        _provider_row("not-a-date"),
        _provider_row("2026-09-30"),
    ], ignore_index=True)
    raw.loc[1, "资产负债率"] = "False"

    rows = api.normalize_provider_frame("000001", raw)

    assert len(rows) == 1
    assert pd.isna(rows.iloc[0]["debt_ratio"])


def test_expected_period_obeys_existing_pit_release_dates():
    api = _api()

    assert api.expected_report_period("2026-10-30") == "2026-06-30"
    assert api.expected_report_period("2026-10-31") == "2026-09-30"
    assert api.expected_report_period("2027-04-30") == "2027-03-31"


def test_normalize_em_bulk_report_converts_percent_units_and_keeps_unknowns_null():
    api = _api()
    frame = pd.DataFrame([{
        "股票代码": "600519",
        "营业总收入-营业总收入": 1000.0,
        "营业总收入-同比增长": 0.28535,
        "净利润-净利润": 200.0,
        "净利润-同比增长": -23.69,
        "每股收益": 2.5,
        "每股净资产": 20.0,
        "每股经营现金流量": 4.0,
        "净资产收益率": 24.47,
        "销售毛利率": 89.581,
        "最新公告日期": "2026-10-28",
    }])

    rows = api.normalize_em_bulk_frame(
        frame, report_period="2026-09-30", as_of="2026-11-10"
    )

    row = rows.iloc[0]
    assert row["symbol"] == "600519"
    assert str(row["ts"])[:10] == "2026-09-30"
    assert row["rev_yoy"] == 0.0028535
    assert row["np_yoy"] == -0.2369
    assert row["roe"] == 0.2447
    assert row["gross_margin"] == 0.89581
    assert pd.isna(row["debt_ratio"])
    assert row["source"] == "ak_stock_yjbb_em"


def test_bulk_batch_appends_high_coverage_rows_but_marks_missing_symbols_pending():
    api = _api()
    frame = pd.DataFrame([
        {
            "股票代码": symbol,
            "营业总收入-营业总收入": 100.0,
            "营业总收入-同比增长": 10.0,
            "净利润-净利润": 5.0,
            "净利润-同比增长": 2.0,
            "每股收益": None,
            "每股净资产": 4.0,
            "每股经营现金流量": None,
            "净资产收益率": 8.0,
            "销售毛利率": None,
            "最新公告日期": "2026-10-30",
        }
        for symbol in ("000001", "600519")
    ])

    result = api.prepare_bulk_append_batch(
        frame,
        universe_symbols=["000001", "600519", "300750"],
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        as_of="2026-11-10",
        minimum_coverage=0.6,
    )

    assert result["status"] == "partial_pending"
    assert result["coverage"] == 2 / 3
    assert result["missing_period_symbols"] == ["300750"]
    assert set(result["rows"]["symbol"]) == {"000001", "600519"}
    assert pd.isna(result["rows"].loc[result["rows"]["symbol"] == "000001", "eps"].iloc[0])
    assert (result["rows"]["ts"] > pd.Timestamp("2026-06-30")).all()


def test_bulk_batch_rejects_rows_announced_after_explicit_as_of():
    api = _api()
    frame = pd.DataFrame([{
        "股票代码": "000001",
        "营业总收入-营业总收入": 100.0,
        "最新公告日期": "2026-11-11",
    }])

    result = api.prepare_bulk_append_batch(
        frame,
        universe_symbols=["000001"],
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        as_of="2026-11-10",
        minimum_coverage=0.99,
    )

    assert result["status"] == "pending"
    assert result["rows"].empty
    assert result["missing_period_symbols"] == ["000001"]


def test_bulk_normalizer_restores_leading_zeroes_lost_by_numeric_provider_codes():
    api = _api()
    frame = pd.DataFrame([{
        "股票代码": 2783,
        "营业总收入-营业总收入": 100.0,
        "最新公告日期": "2026-10-30",
    }])

    rows = api.normalize_em_bulk_frame(
        frame, report_period="2026-09-30", as_of="2026-11-10"
    )

    assert rows["symbol"].tolist() == ["002783"]


def test_bulk_retry_checks_active_universe_and_appends_only_still_missing_symbols():
    api = _api()
    frame = pd.DataFrame([{
        "股票代码": "300750",
        "营业总收入-营业总收入": 100.0,
        "最新公告日期": "2026-10-30",
    }])

    result = api.prepare_bulk_append_batch(
        frame,
        universe_symbols=["000001", "600519", "300750"],
        existing_symbols=["000001", "600519"],
        max_timestamp="2026-09-30",
        expected_period="2026-09-30",
        as_of="2026-11-10",
        minimum_coverage=0.999,
    )

    assert result["status"] == "ready"
    assert result["coverage"] == 1.0
    assert result["rows"]["symbol"].tolist() == ["300750"]
    assert result["missing_period_symbols"] == []


def test_bulk_batch_rejects_duplicate_provider_rows_for_one_symbol():
    api = _api()
    row = {
        "股票代码": "600519",
        "营业总收入-营业总收入": 100.0,
        "净利润-净利润": 5.0,
        "最新公告日期": "2026-10-30",
    }
    frame = pd.DataFrame([row, {**row, "净利润-净利润": 6.0}])

    result = api.prepare_bulk_append_batch(
        frame,
        universe_symbols=["600519"],
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        as_of="2026-11-10",
    )

    assert result["status"] == "invalid"
    assert result["rows"].empty
    assert result["failed_symbols"] == ["600519"]


def test_bulk_sync_default_requires_full_universe_coverage():
    api = _api()
    frame = pd.DataFrame([{
        "股票代码": f"{i:06d}",
        "营业总收入-营业总收入": 100.0,
        "最新公告日期": "2026-10-30",
    } for i in range(1, 1000)])

    result = api.prepare_bulk_append_batch(
        frame,
        universe_symbols=[f"{i:06d}" for i in range(1, 1001)],
        max_timestamp="2026-06-30",
        expected_period="2026-09-30",
        as_of="2026-11-10",
    )

    assert result["minimum_coverage"] == 1.0
    assert result["status"] == "pending"
    assert result["rows"].empty
    assert result["missing_period_symbols"] == ["001000"]


def test_retry_receipt_fingerprints_large_raw_response_without_duplicating_it(tmp_path):
    import json

    api = _api()
    result = api._persist_run(
        output_root=tmp_path,
        expected_period="2026-09-30",
        as_of="2026-11-10",
        status="pending",
        details={},
        raw_rows=_provider_row(),
        store_raw_rows=False,
    )

    bundle = Path(result["bundle_path"])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["raw_capture_status"] == "fingerprint_only_no_new_append"
    assert manifest["raw_input_row_count"] == 1
    assert len(manifest["raw_input_sha256"]) == 64
    assert (bundle / "raw_provider_rows.jsonl").stat().st_size == 0


def test_raw_response_deduplication_requires_an_exact_content_hash_match(tmp_path):
    api = _api()
    first = _provider_row()
    captured = api._persist_run(
        output_root=tmp_path,
        expected_period="2026-09-30",
        as_of="2026-11-10",
        status="available",
        details={},
        raw_rows=first,
    )

    changed = first.copy()
    changed.loc[0, "净利润"] = "12.6亿"

    assert api._raw_evidence_exists(tmp_path, "2026-09-30", first) is True
    assert api._raw_evidence_exists(tmp_path, "2026-09-30", changed) is False


def test_raw_response_deduplication_recognizes_legacy_windows_newline_artifacts(tmp_path):
    import hashlib
    import json

    api = _api()
    rows = _provider_row()
    captured = api._persist_run(
        output_root=tmp_path,
        expected_period="2026-09-30",
        as_of="2026-11-10",
        status="available",
        details={},
        raw_rows=rows,
    )
    bundle = Path(captured["bundle_path"])
    raw_path = bundle / "raw_provider_rows.jsonl"
    legacy_bytes = raw_path.read_bytes().replace(b"\n", b"\r\n")
    raw_path.write_bytes(legacy_bytes)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][raw_path.name] = {
        "sha256": hashlib.sha256(legacy_bytes).hexdigest(),
        "size_bytes": len(legacy_bytes),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert api._raw_evidence_exists(tmp_path, "2026-09-30", rows) is True


def test_raw_response_deduplication_does_not_trust_a_stale_manifest_hash(tmp_path):
    api = _api()
    rows = _provider_row()
    captured = api._persist_run(
        output_root=tmp_path,
        expected_period="2026-09-30",
        as_of="2026-11-10",
        status="available",
        details={},
        raw_rows=rows,
    )
    raw_path = Path(captured["bundle_path"]) / "raw_provider_rows.jsonl"
    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")

    assert api._raw_evidence_exists(tmp_path, "2026-09-30", rows) is False


def test_daily_retry_tasks_use_local_service_with_limited_privileges():
    installer = (ROOT / "ops" / "install_daily_data_retries.ps1").read_text(encoding="utf-8")

    assert "-UserId 'NT AUTHORITY\\LOCAL SERVICE'" in installer
    assert "-RunLevel Limited" in installer
    assert "-UserId 'SYSTEM'" not in installer

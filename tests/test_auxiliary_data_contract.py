from __future__ import annotations

import importlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _module():
    try:
        return importlib.import_module("auxiliary_data")
    except ModuleNotFoundError:
        pytest.fail("auxiliary_data module is required")


def _request(tmp_path: Path, **overrides):
    A = _module()
    values = {
        "output_root": tmp_path / "auxiliary",
        "source": "lhb",
        "trade_day": "2026-11-05",
        "observed_at": "2026-11-05T18:30:00+08:00",
        "provider": "akshare:stock_lhb_detail_em",
        "status": "available",
        "reasons": [],
        "raw_records": [{"代码": "600000", "龙虎榜净买额": 12.5}],
        "normalized_records": [
            {"symbol": "600000", "trade_day": "2026-11-05", "net_amount": 12.5}
        ],
        "coverage": {"requested": "market-day", "received": 1, "failed": 0},
    }
    values.update(overrides)
    return A.AuxiliaryBuildRequest(**values)


def test_research_sources_are_explicitly_non_authoritative():
    specs = _module().source_specs()

    for name in ("stock_news", "orderbook_snapshot", "lhb", "block_trade"):
        spec = specs[name]
        assert spec.tier == "research_only"
        assert spec.authoritative is False
        assert spec.affects_strategy is False
        assert spec.observation_impact == "non_critical_degradation"


def test_same_explicit_input_builds_the_same_bundle(tmp_path):
    A = _module()
    request = _request(tmp_path)

    first = A.build_auxiliary_bundle(request)
    second = A.build_auxiliary_bundle(request)

    assert first.bundle_id == second.bundle_id
    assert first.path == second.path
    assert first.manifest == second.manifest
    assert A.verify_auxiliary_bundle(first.path)["bundle_id"] == first.bundle_id


def test_finalized_bundle_is_not_overwritten_after_tampering(tmp_path):
    A = _module()
    result = A.build_auxiliary_bundle(_request(tmp_path))
    raw_path = result.path / "raw.json"
    raw_path.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(A.AuxiliaryBundleError, match="tampered"):
        A.build_auxiliary_bundle(_request(tmp_path))

    assert json.loads(raw_path.read_text(encoding="utf-8"))["tampered"] is True


def test_concurrent_identical_finalization_converges_on_windows(tmp_path):
    A = _module()
    workers = 16
    barrier = threading.Barrier(workers)

    def build_once(_index):
        barrier.wait(timeout=5)
        return A.build_auxiliary_bundle(_request(tmp_path))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(build_once, range(workers)))

    assert len({item.bundle_id for item in results}) == 1
    assert len({item.path for item in results}) == 1
    assert A.verify_auxiliary_bundle(results[0].path)["bundle_id"] == results[0].bundle_id


def test_pending_publication_is_not_relabelled_failed(tmp_path):
    A = _module()
    result = A.build_auxiliary_bundle(
        _request(
            tmp_path,
            source="block_trade",
            provider="akshare:stock_dzjy_mrmx",
            status="pending_publication",
            reasons=["provider_not_yet_published"],
            raw_records=[],
            normalized_records=[],
            coverage={"requested": "market-day", "received": None, "failed": None},
        )
    )

    assert result.manifest["evidence_status"] == "pending_publication"
    assert result.manifest["failed"] is False
    assert result.manifest["record_counts"] == {"raw": 0, "normalized": 0}


def test_raw_and_normalized_evidence_are_both_committed(tmp_path):
    A = _module()
    result = A.build_auxiliary_bundle(_request(tmp_path))

    assert (result.path / "raw.json").is_file()
    assert (result.path / "records.jsonl").is_file()
    assert set(result.manifest["constituent_artifact_hashes"]) == {
        "raw.json",
        "records.jsonl",
    }
    assert A.verify_auxiliary_bundle(result.path)["evidence_status"] == "available"


def test_builder_does_not_touch_production_artifacts(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    sentinels = {
        name: production / name
        for name in ("selection.json", "target_plan.json", "paper_book.json", "state.json")
    }
    for path in sentinels.values():
        path.write_text('{"unchanged":true}\n', encoding="utf-8")
    before = {name: path.read_bytes() for name, path in sentinels.items()}

    _module().build_auxiliary_bundle(_request(tmp_path))

    assert {name: path.read_bytes() for name, path in sentinels.items()} == before


@pytest.mark.parametrize(
    ("source", "raw", "expected"),
    [
        (
            "lhb",
            {"代码": "600000", "名称": "浦发银行", "龙虎榜买入额": 3, "龙虎榜卖出额": 1, "龙虎榜净买额": 2, "上榜原因": "测试"},
            {"symbol": "600000", "buy_amount": 3.0, "sell_amount": 1.0, "net_amount": 2.0},
        ),
        (
            "block_trade",
            {"证券代码": "600000", "证券简称": "浦发银行", "成交价": 10, "成交量": 20, "成交额": 200},
            {"symbol": "600000", "deal_price": 10.0, "deal_qty": 20.0, "deal_amount": 200.0},
        ),
        (
            "stock_news",
            {"关键词": "浦发银行", "新闻标题": "标题", "新闻内容": "内容", "发布时间": "2026-11-05 18:00:00", "文章来源": "来源", "新闻链接": "https://example.invalid/n"},
            {"title": "标题", "content": "内容", "published_at": "2026-11-05 18:00:00", "source_name": "来源"},
        ),
    ],
)
def test_known_vendor_shapes_are_normalized(source, raw, expected):
    row = _module().normalize_records(source, [raw], trade_day="2026-11-05")[0]

    for key, value in expected.items():
        assert row[key] == value
    assert row["trade_day"] == "2026-11-05"


def test_status_vocabulary_rejects_null_or_invented_success(tmp_path):
    A = _module()

    with pytest.raises(ValueError, match="evidence status"):
        A.build_auxiliary_bundle(_request(tmp_path, status="ok-ish"))


def test_collection_writes_traceable_latest_receipt(tmp_path):
    A = _module()

    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="lhb",
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:lhb",
            publication_state="published",
            records=[{"代码": "600000", "龙虎榜净买额": 2}],
            coverage={"requested": "market-day", "received": 1, "failed": 0},
            reasons=[],
        ),
    )

    receipt_path = tmp_path / "auxiliary" / "receipts" / "lhb.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert result.manifest["evidence_status"] == "available"
    assert receipt["bundle_id"] == result.bundle_id
    assert receipt["manifest_sha256"] == A.file_sha256(result.path / "manifest.json")
    assert receipt["authoritative"] is False
    assert receipt["affects_strategy"] is False
    assert receipt["coverage"] == {"requested": "market-day", "received": 1, "failed": 0}


def test_latest_receipt_never_moves_backwards(tmp_path):
    A = _module()

    def fetcher(**_):
        return A.AuxiliaryFetchResult(
            provider="fixture:lhb",
            publication_state="published",
            records=[{"代码": "600000", "龙虎榜净买额": 2}],
            coverage={"requested": "market-day", "received": 1, "failed": 0},
            reasons=[],
        )

    root = tmp_path / "auxiliary"
    newer = A.run_auxiliary_collection(
        output_root=root,
        source="lhb",
        trade_day="2026-11-05",
        observed_at="2026-11-05T19:00:00+08:00",
        fetcher=fetcher,
    )
    A.run_auxiliary_collection(
        output_root=root,
        source="lhb",
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:00:00+08:00",
        fetcher=fetcher,
    )

    receipt = json.loads((root / "receipts" / "lhb.json").read_text(encoding="utf-8"))
    assert receipt["observed_at"] == "2026-11-05T19:00:00+08:00"
    assert receipt["bundle_id"] == newer.bundle_id


def test_empty_before_publication_is_pending_and_retryable(tmp_path):
    A = _module()
    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="block_trade",
        trade_day="2026-11-05",
        observed_at="2026-11-05T17:00:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:block",
            publication_state="pending",
            records=[],
            coverage={"requested": "market-day", "received": None, "failed": None},
            reasons=["provider_not_yet_published"],
        ),
    )

    assert result.manifest["evidence_status"] == "pending_publication"
    assert result.manifest["failed"] is False
    receipt = json.loads((tmp_path / "auxiliary" / "receipts" / "block_trade.json").read_text(encoding="utf-8"))
    assert receipt["retryable"] is True


def test_empty_published_response_before_release_is_pending(tmp_path):
    A = _module()
    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="lhb",
        trade_day="2026-11-05",
        observed_at="2026-11-05T17:00:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:lhb",
            publication_state="published",
            records=[],
            coverage={"requested": "market-day", "received": 0, "failed": 0},
            reasons=[],
        ),
    )

    assert result.manifest["evidence_status"] == "pending_publication"
    assert "before_auxiliary_release_window" in result.manifest["reasons"]


def test_fetch_error_is_blocked_without_fabricated_zero(tmp_path):
    A = _module()

    def broken(**_):
        raise TimeoutError("vendor timeout")

    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="stock_news",
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        fetcher=broken,
    )

    assert result.manifest["evidence_status"] == "blocked"
    assert result.manifest["coverage"]["received"] is None
    assert "TimeoutError" in result.manifest["reasons"][0]


def test_normalization_failure_preserves_raw_vendor_evidence(tmp_path, monkeypatch):
    A = _module()
    monkeypatch.setattr(
        A,
        "normalize_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad vendor shape")),
    )

    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="lhb",
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:lhb",
            publication_state="published",
            records=[{"unexpected": "raw-value"}],
            coverage={"requested": "market-day", "received": 1, "failed": 0},
            reasons=[],
        ),
    )

    assert result.manifest["evidence_status"] == "invalid"
    assert result.manifest["record_counts"] == {"raw": 1, "normalized": 0}
    raw = json.loads((result.path / "raw.json").read_text(encoding="utf-8"))
    assert raw == [{"unexpected": "raw-value"}]


def test_akshare_adapters_receive_explicit_trade_day():
    A = _module()
    calls = []

    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            assert orient == "records"
            return [{"代码": "600000"}]

    class FakeAk:
        def stock_lhb_detail_em(self, **kwargs):
            calls.append(("lhb", kwargs))
            return FakeFrame()

        def stock_dzjy_mrmx(self, **kwargs):
            calls.append(("block", kwargs))
            return FakeFrame()

        def stock_news_em(self, **kwargs):
            calls.append(("news", kwargs))
            return FakeFrame()

    fake = FakeAk()
    A.fetch_source_records("lhb", "2026-11-05", ak_module=fake)
    A.fetch_source_records("block_trade", "2026-11-05", ak_module=fake)
    A.fetch_source_records("stock_news", "2026-11-05", ak_module=fake)

    assert calls == [
        ("lhb", {"start_date": "20261105", "end_date": "20261105"}),
        ("block", {"start_date": "20261105", "end_date": "20261105"}),
        ("news", {"symbol": "全部A股"}),
    ]


def test_news_adapter_reports_keyword_scope_and_publication_freshness():
    A = _module()

    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"新闻标题": "same day", "发布时间": "2026-11-05 09:00:00"},
                {"新闻标题": "older", "发布时间": "2026-11-03 18:00:00"},
            ]

    class FakeAk:
        def stock_news_em(self, **kwargs):
            assert kwargs == {"symbol": "全部A股"}
            return FakeFrame()

    result = A.fetch_source_records("stock_news", "2026-11-05", ak_module=FakeAk())

    assert result.coverage == {
        "query": "全部A股",
        "scope": "provider_keyword_results",
        "received": 2,
        "same_day_received": 1,
        "latest_published_at": "2026-11-05 09:00:00",
        "failed": 0,
    }


def test_news_normalization_separates_collection_day_from_publication_day():
    row = _module().normalize_records(
        "stock_news",
        [{"新闻标题": "older", "发布时间": "2026-11-03 18:00:00"}],
        trade_day="2026-11-05",
    )[0]

    assert row["collection_trade_day"] == "2026-11-05"
    assert row["published_date"] == "2026-11-03"


def test_news_without_same_day_keyword_result_is_missing_not_available(tmp_path):
    A = _module()
    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="stock_news",
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:news-keyword",
            publication_state="published",
            records=[{"新闻标题": "older", "发布时间": "2026-11-03 18:00:00"}],
            coverage={
                "query": "全部A股",
                "scope": "provider_keyword_results",
                "received": 1,
                "same_day_received": 0,
                "latest_published_at": "2026-11-03 18:00:00",
                "failed": 0,
            },
            reasons=[],
        ),
    )

    assert result.manifest["evidence_status"] == "missing"
    assert "no_same_day_keyword_result" in result.manifest["reasons"]


def test_orderbook_requires_explicit_symbols_and_closes_client():
    A = _module()
    observed = {"closed": False, "symbols": None}

    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            assert orient == "records"
            return [{"code": "600000", "bid1": 9.9, "ask1": 10.1}]

    class FakeQuotes:
        def quotes(self, symbols):
            observed["symbols"] = symbols
            return FakeFrame()

        def close(self):
            observed["closed"] = True

    result = A.fetch_source_records(
        "orderbook_snapshot",
        "2026-11-05",
        symbols=["600000.SH"],
        quote_client_factory=lambda: FakeQuotes(),
    )

    assert observed == {"closed": True, "symbols": ["600000"]}
    assert result.coverage == {"requested": 1, "received": 1, "failed": 0}
    with pytest.raises(ValueError, match="explicit symbols"):
        A.fetch_source_records("orderbook_snapshot", "2026-11-05", symbols=[])


def test_collection_uses_default_fetcher_with_explicit_symbols(tmp_path, monkeypatch):
    A = _module()
    calls = []

    def fake_fetch(source, trade_day, *, symbols=None, **_kwargs):
        calls.append((source, trade_day, symbols))
        return A.AuxiliaryFetchResult(
            provider="fixture:orderbook",
            publication_state="published",
            records=[{"code": "600000", "bid1": 9.9, "ask1": 10.1}],
            coverage={"requested": 1, "received": 1, "failed": 0},
            reasons=[],
        )

    monkeypatch.setattr(A, "fetch_source_records", fake_fetch)
    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="orderbook_snapshot",
        trade_day="2026-11-05",
        observed_at="2026-11-05T14:50:00+08:00",
        symbols=["600000.SH"],
    )

    assert calls == [("orderbook_snapshot", "2026-11-05", ["600000.SH"])]
    assert result.manifest["evidence_status"] == "available"


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"code": "600000", "price": 10.0}],
        [{"code": "600000", "bid1": 9.9, "ask1": None}],
    ],
)
def test_orderbook_requires_real_bid_and_ask_depth(records):
    A = _module()

    class FakeFrame:
        empty = not records

        def to_dict(self, orient):
            assert orient == "records"
            return records

    class FakeQuotes:
        def quotes(self, _symbols):
            return FakeFrame()

        def close(self):
            pass

    result = A.fetch_source_records(
        "orderbook_snapshot",
        "2026-11-05",
        symbols=["600000.SH"],
        quote_client_factory=lambda: FakeQuotes(),
    )

    assert result.publication_state == "missing"
    assert result.coverage == {"requested": 1, "received": 0, "failed": 1}
    assert "no_valid_bid_ask_depth" in result.reasons


def test_partial_orderbook_is_retryable_missing_evidence(tmp_path):
    A = _module()
    result = A.run_auxiliary_collection(
        output_root=tmp_path / "auxiliary",
        source="orderbook_snapshot",
        trade_day="2026-11-05",
        observed_at="2026-11-05T14:50:00+08:00",
        fetcher=lambda **_: A.AuxiliaryFetchResult(
            provider="fixture:orderbook",
            publication_state="partial",
            records=[{"code": "600000", "bid1": 9.9, "ask1": 10.1}],
            coverage={"requested": 2, "received": 1, "failed": 1},
            reasons=["partial_orderbook_coverage"],
        ),
    )

    assert result.manifest["evidence_status"] == "missing"
    receipt = json.loads((tmp_path / "auxiliary" / "receipts" / "orderbook_snapshot.json").read_text(encoding="utf-8"))
    assert receipt["retryable"] is True


def test_orderbook_rejects_valid_depth_for_unrequested_symbol():
    A = _module()

    class FakeFrame:
        empty = False

        def to_dict(self, orient):
            assert orient == "records"
            return [{"code": "000001", "bid1": 9.9, "ask1": 10.1}]

    class FakeQuotes:
        def quotes(self, _symbols):
            return FakeFrame()

        def close(self):
            pass

    result = A.fetch_source_records(
        "orderbook_snapshot",
        "2026-11-05",
        symbols=["600000.SH"],
        quote_client_factory=lambda: FakeQuotes(),
    )

    assert result.publication_state == "missing"
    assert result.coverage == {
        "requested": 1,
        "received": 0,
        "failed": 1,
        "unexpected": 1,
    }
    assert "unexpected_orderbook_symbols" in result.reasons


def test_vendor_datetime_and_nonfinite_values_are_json_safe(tmp_path):
    A = _module()
    result = A.build_auxiliary_bundle(_request(
        tmp_path,
        raw_records=[{
            "代码": "600000",
            "time": datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc),
            "missing": float("nan"),
        }],
        normalized_records=[{
            "symbol": "600000",
            "trade_day": "2026-11-05",
            "metric": float("inf"),
        }],
    ))

    raw = json.loads((result.path / "raw.json").read_text(encoding="utf-8"))
    normalized = json.loads((result.path / "records.jsonl").read_text(encoding="utf-8"))
    assert raw[0]["time"] == "2026-11-05T18:00:00+00:00"
    assert raw[0]["missing"] is None
    assert normalized["metric"] is None



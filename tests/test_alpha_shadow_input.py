from __future__ import annotations

import pytest

from alpha_shadow_input import (
    aggregate_shadow_results,
    benchmark_from_forward_returns,
    build_normalized_payload,
    forward_returns_from_bars,
)


def _xsec_rows():
    return [
        {
            "canon": "600001.SH",
            "signal": 0.20,
            "trend": 1.0,
            "govern": 0.8,
            "liquidity": 0.6,
            "vol": 0.4,
            "mom_rev": 0.3,
        },
        {
            "canon": "000002.SZ",
            "signal": 0.10,
            "trend": 0.0,
            "govern": 0.7,
            "liquidity": 0.5,
            "vol": 0.5,
            "mom_rev": 0.2,
        },
    ]


def _fusion_rows():
    return [
        {"symbol": "600001", "pb_inv": 2.0, "ep": 0.5, "ocf_ps": 1.0, "roe_yy_chg": 0.2},
        {"symbol": "000002", "pb_inv": 1.0, "ep": 0.2, "ocf_ps": 0.5, "roe_yy_chg": 0.1},
    ]


def test_build_normalized_payload_merges_scores_and_forward_returns():
    payload = build_normalized_payload(
        trade_day="2026-09-01",
        xsec_rows=_xsec_rows(),
        fusion_rows=_fusion_rows(),
        forward_returns={
            "600001": {"1": 0.03, "5": 0.05},
            "000002": {"1": -0.01, "5": 0.02},
        },
        selector_weights={
            "signal": 0.34,
            "trend": 0.14,
            "govern": 0.16,
            "liquidity": 0.08,
            "vol": 0.10,
            "mom_rev": 0.0,
        },
        factor_weights={"pb_inv": 1.0, "ep": 1.0, "ocf_ps": 1.0, "roe_yy_chg": 1.0},
        factor_directions={"pb_inv": 1, "ep": 1, "ocf_ps": 1, "roe_yy_chg": 1},
        previous_symbols=["600001.SH"],
    )

    assert payload["schema_version"] == 1
    assert payload["trade_day"] == "2026-09-01"
    assert payload["previous_symbols"] == ["600001.SH"]
    assert [row["symbol"] for row in payload["rows"]] == ["600001.SH", "000002.SZ"]
    assert payload["rows"][0]["forward_returns"] == {"1": 0.03, "5": 0.05}
    assert payload["rows"][0]["scores"]["control_prod"] == pytest.approx(
        payload["rows"][0]["scores"]["selector_score"]
    )
    assert "fusion_A" in payload["rows"][0]["scores"]
    assert payload["rows"][0]["scores"]["fusion_rank_on"] == pytest.approx(
        payload["rows"][0]["scores"]["fusion_A"]
    )


def test_build_normalized_payload_rejects_missing_required_xsec_field():
    rows = _xsec_rows()
    rows[0].pop("signal")
    with pytest.raises(ValueError, match="signal"):
        build_normalized_payload(
            trade_day="2026-09-01",
            xsec_rows=rows,
            fusion_rows=_fusion_rows(),
            forward_returns={},
            selector_weights={"signal": 0.34},
            factor_weights={"pb_inv": 1.0},
            factor_directions={"pb_inv": 1},
        )


def test_build_normalized_payload_treats_nan_factor_as_missing():
    fusion = _fusion_rows()
    fusion[0]["ep"] = float("nan")
    payload = build_normalized_payload(
        trade_day="2026-09-01",
        xsec_rows=_xsec_rows(),
        fusion_rows=fusion,
        forward_returns={},
        selector_weights={"signal": 0.34},
        factor_weights={field: 1.0 for field in ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")},
        factor_directions={field: 1 for field in ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")},
    )

    assert payload["rows"][0]["scores"]["fusion_A"] == pytest.approx(
        payload["rows"][0]["scores"]["fusion_rank_on"]
    )


def test_build_normalized_payload_records_symbols_without_fusion_coverage():
    xsec = _xsec_rows()
    xsec.append({**xsec[0], "canon": "300146.SZ"})
    payload = build_normalized_payload(
        trade_day="2026-09-01",
        xsec_rows=xsec,
        fusion_rows=_fusion_rows(),
        forward_returns={},
        selector_weights={"signal": 0.34},
        factor_weights={field: 1.0 for field in ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")},
        factor_directions={field: 1 for field in ("pb_inv", "ep", "ocf_ps", "roe_yy_chg")},
        strict_fusion=False,
    )

    assert [row["symbol"] for row in payload["rows"]] == ["600001.SH", "000002.SZ"]
    assert payload["coverage"] == {
        "xsec_rows": 3,
        "included_rows": 2,
        "excluded_missing_fusion": ["300146.SZ"],
        "fallback_count": 0,
        "fallback_policy": "exclude_missing_fusion",
    }


def test_aggregate_shadow_results_preserves_missing_values():
    results = [
        {
            "trade_day": "2026-09-01",
            "result": {
                "arms": {
                    "control_prod": {
                        "rank_ic": {"1": 0.1, "5": None},
                        "forward_return_mean": {"1": 0.02, "5": None},
                        "turnover": 0.5,
                    }
                },
                "comparisons": {},
            },
        },
        {
            "trade_day": "2026-09-02",
            "result": {
                "arms": {
                    "control_prod": {
                        "rank_ic": {"1": 0.3, "5": 0.2},
                        "forward_return_mean": {"1": 0.04, "5": 0.06},
                        "turnover": None,
                    }
                },
                "comparisons": {},
            },
        },
    ]

    report = aggregate_shadow_results(results, horizons=(1, 5))
    arm = report["arms"]["control_prod"]
    assert arm["rank_ic"]["1"] == {"mean": 0.2, "n": 2}
    assert arm["rank_ic"]["5"] == {"mean": 0.2, "n": 1}
    assert arm["forward_return_mean"]["1"] == {"mean": 0.03, "n": 2}
    assert arm["turnover"] == {"mean": 0.5, "n": 1}


def test_aggregate_shadow_results_includes_excess_and_cost_metrics():
    results = [
        {
            "trade_day": "2026-09-01",
            "result": {
                "benchmark": {
                    "name": "equal_weight_xsec_available",
                    "returns": {"1": 0.01},
                },
                "arms": {
                    "control_prod": {
                        "rank_ic": {"1": 0.1},
                        "forward_return_mean": {"1": 0.02},
                        "forward_observation_coverage": {"1": 0.5},
                        "quantile_forward_return_mean": {
                            "1": {"q1": 0.01, "q2": 0.02}
                        },
                        "quantile_monotonicity": {
                            "1": {
                                "spread_q5_q1": 0.03,
                                "is_non_decreasing": True,
                                "observed_buckets": 2,
                            }
                        },
                        "excess_return_mean": {"1": 0.01},
                        "cost_adjusted_forward_return_mean": {"1": 0.019},
                        "turnover": 0.1,
                    }
                },
                "comparisons": {},
            },
            "coverage": {
                "xsec_rows": 10,
                "included_rows": 9,
                "excluded_missing_fusion": ["x"],
                "fallback_count": 0,
                "fallback_policy": "exclude_missing_fusion",
            },
        }
    ]

    report = aggregate_shadow_results(results, horizons=(1,))
    arm = report["arms"]["control_prod"]
    assert arm["excess_return_mean"]["1"] == {"mean": 0.01, "n": 1}
    assert arm["cost_adjusted_forward_return_mean"]["1"] == {
        "mean": 0.019,
        "n": 1,
    }
    assert arm["forward_observation_coverage"]["1"] == {"mean": 0.5, "n": 1}
    assert arm["quantile_forward_return_mean"]["1"]["q1"] == {
        "mean": 0.01,
        "n": 1,
    }
    assert arm["quantile_monotonicity"]["1"] == {
        "spread_q5_q1": {"mean": 0.03, "n": 1},
        "monotonic_rate": {"mean": 1.0, "n": 1},
        "observed_buckets": {"mean": 2.0, "n": 1},
    }
    assert report["benchmark"] == {
        "name": "equal_weight_xsec_available",
        "returns": {"1": {"mean": 0.01, "n": 1}},
    }
    assert report["coverage"] == {
        "daily": [
            {
                "trade_day": "2026-09-01",
                "xsec_rows": 10,
                "included_rows": 9,
                "excluded_rows": 1,
                "exclusion_rate": 0.1,
                "fallback_count": 0,
                "fallback_policy": "exclude_missing_fusion",
            }
        ],
        "mean_exclusion_rate": 0.1,
        "fallback_count": 0,
    }


def test_benchmark_from_forward_returns_uses_available_finite_observations():
    benchmark = benchmark_from_forward_returns(
        {
            "600001": {"1": 0.01, "5": 0.05},
            "600002": {"1": 0.03, "5": None},
            "600003": {"1": "bad"},
        },
        horizons=(1, 5, 10),
    )

    assert benchmark == {
        "name": "equal_weight_xsec_available",
        "returns": {"1": 0.02, "5": 0.05},
        "n": {"1": 2, "5": 1},
    }


def test_forward_returns_from_bars_compounds_change_pct_and_marks_immature_horizon():
    values = forward_returns_from_bars(
        trading_days=["2026-09-01", "2026-09-02", "2026-09-03"],
        bars=[
            {"d": "2026-09-02", "symbol": "600001", "change_pct": 10.0},
            {"d": "2026-09-03", "symbol": "600001", "change_pct": -5.0},
            {"d": "2026-09-02", "symbol": "000002", "change_pct": 2.0},
        ],
        trade_day="2026-09-01",
        symbols=["600001", "000002"],
        horizons=(1, 2, 5),
    )

    assert values["600001"]["1"] == pytest.approx(0.10)
    assert values["600001"]["2"] == pytest.approx(1.10 * 0.95 - 1.0)
    assert "5" not in values["600001"]
    assert values["000002"]["1"] == pytest.approx(0.02)

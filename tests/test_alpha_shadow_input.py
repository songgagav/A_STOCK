from __future__ import annotations

import pytest

from alpha_shadow_input import aggregate_shadow_results, build_normalized_payload


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

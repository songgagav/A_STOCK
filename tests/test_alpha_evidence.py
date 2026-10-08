from __future__ import annotations

import sys
from datetime import date

import pytest

sys.path.insert(0, "src")

from alpha_evidence import evaluate_alpha_evidence, summarize_forward_windows  # noqa: E402


def _ic_payload(mean: float = 0.04, positive: int = 8, n: int = 12) -> dict:
    return {
        "summary": {
            str(h): {"mean": mean, "median": mean, "pos": positive, "n": n}
            for h in (1, 5, 10, 20, 60, 120)
        }
    }


def _windows(n: int = 10, total_return: float = 0.08) -> list[dict]:
    return [
        {
            "day": f"2020-{i + 1:02d}-03",
            "ok": True,
            "window_mode": "forward",
            "fallback": False,
            "stats": {
                "start_date": f"2020-{i + 1:02d}-03",
                "end_date": f"2020-{i + 1:02d}-28",
                "total_return": total_return,
                "sharpe_ratio": 1.2,
                "max_ddpercent": -8.0,
            },
        }
        for i in range(n)
    ]


def _attribution(n: int = 10) -> list[dict]:
    return [
        {
            "n_sym": 10,
            "to_ratio": 20.0,
            "win_rate": 60.0,
            "pl_ratio": 1.8,
            "basket": 0.08,
            "bt": 0.075,
        }
        for _ in range(n)
    ]


def test_missing_artifacts_are_unavailable_not_pass():
    report = evaluate_alpha_evidence(None, None, None)

    assert report["status"] == "unavailable"
    assert report["next_action"] == "collect_missing_artifacts"
    assert report["checks"]["ic_term_structure"]["status"] == "unavailable"
    assert report["checks"]["forward_windows"]["status"] == "unavailable"
    assert report["checks"]["attribution"]["status"] == "unavailable"


def test_forward_gate_excludes_backward_fallback_and_date_lookahead():
    rows = _windows(2)
    rows.extend(
        [
            {**_windows(1)[0], "window_mode": "backward"},
            {**_windows(1)[0], "fallback": True},
            {
                **_windows(1)[0],
                "stats": {**_windows(1)[0]["stats"], "start_date": "2019-12-31"},
            },
        ]
    )

    result = summarize_forward_windows(rows, min_valid_windows=2)

    assert result["status"] == "pass"
    assert result["valid_window_count"] == 2
    assert result["excluded_count"] == 3
    assert {x["reason"] for x in result["excluded"]} == {
        "wrong_window_mode",
        "fallback_engine",
        "window_starts_before_decision_day",
    }


def test_forward_gate_rejects_malformed_dates():
    row = _windows(1)[0]
    row["day"] = "not-a-date"
    row["stats"]["start_date"] = "not-a-date"

    result = summarize_forward_windows([row], min_valid_windows=1)

    assert result["status"] == "fail"
    assert result["valid_window_count"] == 0
    assert result["excluded"][0]["reason"] == "invalid_date"


def test_immature_forward_window_is_pending_not_failed():
    mature = _windows(1)[0]
    pending = {
        "day": "2026-09-04",
        "ok": False,
        "window_mode": "forward",
        "fallback": False,
        "stats": {},
    }

    result = summarize_forward_windows(
        [mature, pending],
        min_valid_windows=1,
        as_of_date=date(2026, 10, 6),
        horizon_days=120,
    )

    assert result["status"] == "pass"
    assert result["valid_window_count"] == 1
    assert result["pending_maturity_count"] == 1
    assert result["excluded_count"] == 0
    assert result["pending_maturity"][0]["day"] == "2026-09-04"


def test_negative_ic_is_not_promotable_even_with_positive_oos():
    report = evaluate_alpha_evidence(
        _ic_payload(mean=-0.06, positive=3), _windows(), _attribution()
    )

    assert report["status"] == "not_promotable"
    assert report["next_action"] == "inspect_signal"
    assert report["checks"]["ic_term_structure"]["status"] == "fail"
    assert report["checks"]["forward_windows"]["status"] == "pass"


def test_evidence_report_exposes_pending_maturity_without_downgrading_status():
    pending = {
        "day": "2026-09-04",
        "ok": False,
        "window_mode": "forward",
        "fallback": False,
        "stats": {},
    }

    report = evaluate_alpha_evidence(
        _ic_payload(),
        [*_windows(), pending],
        _attribution(),
        as_of_date=date(2026, 10, 6),
        forward_horizon_days=120,
    )

    forward = report["checks"]["forward_windows"]
    assert report["status"] == "evidence_ready"
    assert forward["pending_maturity_count"] == 1
    assert forward["excluded_count"] == 0


def test_complete_positive_evidence_is_only_eligible_for_human_review():
    report = evaluate_alpha_evidence(
        _ic_payload(), _windows(), _attribution()
    )

    assert report["status"] == "evidence_ready"
    assert report["next_action"] == "human_review"
    assert report["execution_change"] is False


def test_malformed_ic_payload_is_invalid_not_unavailable():
    report = evaluate_alpha_evidence({"summary": {"1": {"mean": "bad"}}}, _windows(), _attribution())

    assert report["status"] == "invalid"
    assert report["checks"]["ic_term_structure"]["status"] == "invalid"

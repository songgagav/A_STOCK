"""T10: prove holding-period basis, or disclose conservative approximation."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from paper_book import PaperBook

CANON = "600000.SH"
EX_DATE = "2026-10-09"


def acquired(day="2025-10-09"):
    book = PaperBook(init_capital=100_000)
    book.trade_date = day
    assert book.buy(CANON, 100, 10.0) is not None
    return book


def dividend(**changes):
    event = {"symbol": "600000", "ex_date": EX_DATE,
             "dividend_cash": 10.0, "key": "600000:2026-10-09:dividend"}
    event.update(changes)
    return event


def persisted(book):
    """Exercise the existing JSON position/state format, not a restore stub."""
    return json.loads(json.dumps({
        "day": book.trade_date, "cash": book.cash,
        "positions": book.snapshot()["positions"],
        "buy_fees": book.buy_fees, "sell_fees": book.sell_fees,
        "fees_paid": book.fees_paid,
        "applied_corp_actions": sorted(book.applied_corp),
    }))


@pytest.mark.parametrize("days,rate,net", [
    (0, 0.20, 80.0), (29, 0.20, 80.0), (30, 0.10, 90.0),
    (364, 0.10, 90.0), (365, 0.00, 100.0), (400, 0.00, 100.0),
])
def test_single_date_holding_days_and_existing_tax_boundaries(days, rate, net):
    buy_date = (date(2026, 10, 9) - timedelta(days=days)).isoformat()
    book = acquired(buy_date)
    book.trade_date = "2099-01-01"  # processing day must not change the acquisition period
    assert book._holding_days(CANON, EX_DATE) == days
    before = book.cash
    result = book.apply_corporate_actions([dividend()])[0]
    assert result["holding_days"] == days
    assert result["holding_basis"] == "single_date"
    assert result["holding_status"] == result["tax_status"] == "exact"
    assert result["tax_rate"] == rate
    assert result["tax_policy"] == "holding_period_schedule"
    assert result["dividend_gross"] == 100.0
    assert result["dividend_net"] == net
    assert book.cash - before == pytest.approx(net)


@pytest.mark.parametrize("buy_date,reason", [
    (None, "invalid_buy_date"), ("", "invalid_buy_date"),
    ("bad-date", "invalid_buy_date"), ("2026-02-30", "invalid_buy_date"),
    ("2026-10-10", "future_buy_date"),
])
def test_invalid_or_future_buy_date_never_becomes_exact_zero(buy_date, reason):
    book = acquired()
    book.positions[CANON]["buy_date"] = buy_date
    book.positions[CANON]["acquisition_basis"] = {"status": "single_date", "date": buy_date}
    assert book._holding_days(CANON, EX_DATE) is None
    result = book.apply_corporate_actions([dividend()])[0]
    assert result["holding_days"] is None
    assert result["holding_reason"] == reason
    assert result["holding_basis"] == "unknown"
    assert result["holding_status"] == result["tax_status"] == "approximate"
    assert result["tax_rate"] == 0.20
    assert result["tax_policy"] == "conservative_max_rate"
    assert result["dividend_net"] == 80.0


@pytest.mark.parametrize("ex_date", [None, "bad-date", "2026-02-30"])
def test_invalid_ex_date_never_substitutes_system_today(ex_date):
    book = acquired()
    assert book._holding_days(CANON, ex_date) is None
    result = book.apply_corporate_actions([dividend(ex_date=ex_date)])[0]
    assert result["holding_reason"] == "invalid_ex_date"
    assert result["holding_days"] is None
    assert result["tax_status"] == "approximate"
    assert result["tax_rate"] == 0.20


def test_new_and_same_date_buys_prove_single_date_basis():
    book = acquired()
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "single_date", "date": "2025-10-09",
    }
    assert book.buy(CANON, 100, 10.0) is not None
    assert book.positions[CANON]["acquisition_basis"] == {
        "status": "single_date", "date": "2025-10-09",
    }
    result = book.apply_corporate_actions([dividend()])[0]
    assert (result["holding_days"], result["tax_rate"], result["dividend_net"]) == (365, 0, 200)


def test_cross_date_buys_remain_unknown_instead_of_overwriting_holding_basis():
    book = acquired()
    book.trade_date = "2026-10-01"
    assert book.buy(CANON, 100, 10.0) is not None
    assert book.positions[CANON]["buy_date"] == "2026-10-01"  # existing T+1 field is preserved
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "unknown", "reason": "multiple_acquisition_dates",
    }
    assert book._holding_days(CANON, EX_DATE) is None
    result = book.apply_corporate_actions([dividend()])[0]
    assert result["holding_reason"] == "multiple_acquisition_dates"
    assert result["tax_status"] == "approximate"
    assert result["tax_policy"] == "conservative_max_rate"
    assert result["dividend_net"] == 160.0


@pytest.mark.parametrize("buy_date", ["2025-10-09", None, "bad-date"])
def test_legacy_aggregate_cannot_prove_a_single_acquisition_date(buy_date):
    book = PaperBook()
    book.positions = {CANON: {"qty": 100, "avg_cost": 10.0, "buy_date": buy_date}}
    assert book._holding_days(CANON, EX_DATE) is None
    result = book.apply_corporate_actions([dividend()])[0]
    assert result["holding_reason"] == "legacy_aggregate"
    assert result["holding_status"] == "approximate"
    assert result["dividend_net"] == 80.0


@pytest.mark.parametrize("cross_date", [False, True])
def test_acquisition_basis_survives_json_snapshot_and_restore(cross_date):
    original = acquired()
    if cross_date:
        original.trade_date = "2026-10-01"
        original.buy(CANON, 100, 10.0)
    state = persisted(original)
    restored = PaperBook()
    restored.restore(state)
    expected = ({"status": "unknown", "reason": "multiple_acquisition_dates"}
                if cross_date else {"status": "single_date", "date": "2025-10-09"})
    assert restored.positions[CANON].get("acquisition_basis") == expected
    assert restored._holding_days(CANON, EX_DATE) == (None if cross_date else 365)
    state["positions"][CANON]["acquisition_basis"]["status"] = "mutated"
    assert restored.positions[CANON]["acquisition_basis"] == expected


def test_restore_legacy_missing_date_does_not_prove_todays_acquisition():
    restored = PaperBook()
    restored.restore({"day": "2026-10-09", "buy_fees": 0, "sell_fees": 0,
                      "positions": {CANON: {"qty": 100, "avg_cost": 10}}})
    assert restored.positions[CANON].get("acquisition_basis") == {
        "status": "unknown", "reason": "legacy_aggregate",
    }
    assert restored._holding_days(CANON, EX_DATE) is None


def test_restore_missing_buy_date_with_single_date_tag_cannot_become_exact_today():
    restored = PaperBook()
    restored.trade_date = EX_DATE  # force the existing T+1 restore fallback deterministically
    restored.restore({
        "day": EX_DATE, "buy_fees": 0, "sell_fees": 0,
        "positions": {CANON: {
            "qty": 100, "avg_cost": 10,
            "acquisition_basis": {"status": "single_date", "date": EX_DATE},
        }},
    })
    assert restored._holding_days(CANON, EX_DATE) is None
    result = restored.apply_corporate_actions([dividend()])[0]
    assert result["holding_reason"] == "invalid_buy_date"
    assert result["holding_basis"] == "unknown"
    assert result["tax_status"] == "approximate"
    assert result["tax_rate"] == 0.20


def test_adding_to_legacy_same_date_position_does_not_invent_complete_history():
    book = acquired()
    book.positions[CANON].pop("acquisition_basis", None)
    assert book.buy(CANON, 100, 10.0) is not None
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "unknown", "reason": "legacy_aggregate",
    }
    assert book._holding_days(CANON, EX_DATE) is None


def test_inconsistent_recorded_basis_is_not_an_exact_holding_period():
    book = acquired()
    book.positions[CANON]["acquisition_basis"] = {"status": "single_date", "date": "2026-10-01"}
    assert book._holding_days(CANON, EX_DATE) is None
    result = book.apply_corporate_actions([dividend()])[0]
    assert result["holding_reason"] == "inconsistent_acquisition_date"
    assert result["tax_status"] == "approximate"


def test_rejected_buy_does_not_change_acquisition_basis():
    book = acquired()
    book.cash = 0
    book.trade_date = "2026-10-01"
    assert book.buy(CANON, 100, 10.0) is None
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "single_date", "date": "2025-10-09",
    }


def test_same_date_tplus1_and_cross_date_partial_sale_keep_existing_behavior():
    book = acquired()
    assert book.sell(CANON, 100, 10.0) is None
    book.trade_date = "2026-10-01"
    book.buy(CANON, 100, 10.0)
    assert book.sell(CANON, 200, 10.0) is None  # preserve current aggregated lock behavior
    book.trade_date = "2026-10-02"
    assert book.sell(CANON, 100, 10.0) is not None
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "unknown", "reason": "multiple_acquisition_dates",
    }
    assert book._holding_days(CANON, EX_DATE) is None


def test_full_exit_then_new_position_has_a_new_provable_basis():
    book = acquired()
    book.trade_date = "2026-10-01"
    book.sell(CANON, 100, 10.0)
    book.buy(CANON, 100, 10.0)
    assert book.positions[CANON].get("acquisition_basis") == {
        "status": "single_date", "date": "2026-10-01",
    }
    assert book._holding_days(CANON, EX_DATE) == 8


def test_dividend_idempotency_survives_restore_with_new_basis():
    book = acquired()
    before = book.cash
    first = book.apply_corporate_actions([dividend()])
    assert first[0].get("holding_days") == 365
    assert book.cash - before == pytest.approx(100.0)
    assert book.apply_corporate_actions([dividend()]) == []
    restored = PaperBook()
    restored.restore(persisted(book))
    assert restored.apply_corporate_actions([dividend()]) == []
    assert restored.cash == book.cash


def test_bonus_share_and_cash_entitlement_math_remain_unchanged():
    book = acquired()
    original_cost = book.positions[CANON]["qty"] * book.positions[CANON]["avg_cost"]
    result = book.apply_corporate_actions([dividend(bonus_ratio=1.0)])[0]
    assert book.positions[CANON]["qty"] == 110
    assert book.positions[CANON]["qty"] * book.positions[CANON]["avg_cost"] == pytest.approx(original_cost)
    assert result.get("holding_days") == 365
    assert result["dividend_gross"] == result["dividend_net"] == 110.0

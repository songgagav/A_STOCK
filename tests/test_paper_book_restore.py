from src.paper_book import PaperBook


def test_restore_rehydrates_current_day_trade_ledger():
    trade = {
        "date": "2026-10-26",
        "time": "09:30:11",
        "type": "buy",
        "canon": "601000.SH",
        "qty": 1900,
        "price": 4.733,
        "fee": 4.50,
    }
    state = {
        "day": "2026-10-26",
        "cash": 5440.76,
        "positions": {},
        "trades_history": {"2026-10-26": [trade]},
    }

    book = PaperBook(init_capital=100000)
    book.restore(state)

    assert book.trade_date == "2026-10-26"
    assert book.trades_today == [trade]
    assert book.today_trades == 1
    assert book.snapshot()["trades_today"] == 1


def test_roll_to_new_day_does_not_expose_prior_day_trades():
    trade = {
        "date": "2026-10-26",
        "time": "09:30:11",
        "type": "buy",
        "canon": "601000.SH",
        "qty": 1900,
        "price": 4.733,
        "fee": 4.50,
    }
    book = PaperBook(init_capital=100000)
    book.restore({
        "day": "2026-10-26",
        "cash": 5440.76,
        "positions": {},
        "trades_history": {"2026-10-26": [trade]},
    })

    book.set_trade_date("2026-10-27")

    assert book.trade_date == "2026-10-27"
    assert book.trades_today == []
    assert book.today_trades == 0
    assert book.trades_history["2026-10-26"] == [trade]

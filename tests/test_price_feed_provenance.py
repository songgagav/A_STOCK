import sys
from types import SimpleNamespace

from paper_book import PriceFeed


def test_reference_fallback_keeps_source_attempt_provenance(monkeypatch):
    feed = PriceFeed()
    akshare = SimpleNamespace(stock_zh_a_spot_em=None)
    monkeypatch.setitem(sys.modules, "akshare", akshare)
    monkeypatch.setattr(feed, "watchlist_codes", lambda: ["600000.SH"])
    monkeypatch.setattr(feed, "_fetch_sina_spot", lambda codes: {})
    monkeypatch.setattr(
        akshare,
        "stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    monkeypatch.setattr(
        feed,
        "_fetch_from_duckdb",
        lambda symbols=None: {
            "600000.SH": {
                "price": 10.0,
                "last_close": 10.0,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "fallback": True,
                "price_source": "h5i_reference",
            }
        },
    )

    result = feed._fetch_spot()

    assert result["600000.SH"]["price_source"] == "h5i_reference"
    assert feed.fetch_attempts == [
        {"source": "sina_spot", "status": "empty"},
        {"source": "akshare_spot", "status": "error", "reason": "RuntimeError: network unavailable"},
        {"source": "h5i_reference", "status": "ok", "count": 1},
    ]
    assert "live_source_unavailable" in feed.last_error


def test_timeout_fallback_replaces_stale_attempt_provenance(monkeypatch):
    feed = PriceFeed()
    feed._fetch_attempts = [{"source": "old_refresh", "status": "ok"}]
    monkeypatch.setattr(
        feed,
        "_fetch_spot",
        lambda fetch_all=False: (_ for _ in ()).throw(TimeoutError("hung")),
    )
    monkeypatch.setattr(
        feed,
        "_fetch_from_duckdb",
        lambda symbols=None: {
            "600000.SH": {
                "price": 10.0,
                "last_close": 10.0,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "fallback": True,
                "price_source": "h5i_reference",
            }
        },
    )

    result = feed._fetch_spot_with_timeout()

    assert result["600000.SH"]["price_source"] == "h5i_reference"
    assert feed.fetch_attempts[0]["status"] in {"error", "timeout"}
    assert feed.fetch_attempts[0]["source"] == "spot_fetch"
    assert feed.fetch_attempts[-1] == {
        "source": "h5i_reference", "status": "fallback", "count": 1,
    }

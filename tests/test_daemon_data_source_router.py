from __future__ import annotations

from datetime import date
import inspect


def test_daemon_source_router_is_explicit_opt_in(monkeypatch) -> None:
    import daemon

    monkeypatch.delenv("DATA_SOURCE_ROUTER_ENABLED", raising=False)
    assert daemon._source_router_enabled() is False
    monkeypatch.setenv("DATA_SOURCE_ROUTER_ENABLED", "1")
    assert daemon._source_router_enabled() is True


def test_daemon_runs_router_at_close_and_cleanup_in_watchdog() -> None:
    import daemon

    source = inspect.getsource(daemon.run_loop)
    assert "_run_data_source_router(today)" in source
    assert "_run_staging_cleanup(today)" in source
    assert "last_source_router_day" in source
    assert "last_staging_cleanup_day" in source


def test_disabled_router_does_not_touch_network(monkeypatch) -> None:
    import daemon

    monkeypatch.delenv("DATA_SOURCE_ROUTER_ENABLED", raising=False)
    daemon._state["last_source_router_day"] = None
    result = daemon._run_data_source_router(date(2026, 9, 30))

    assert result == {"status": "disabled", "trade_day": "2026-09-30"}
    assert daemon._state["last_source_router_day"] is None

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import auxiliary_data as A  # noqa: E402
import data_update_daemon as D  # noqa: E402


def test_sync_cli_requires_explicit_day_time_and_output():
    import auxiliary_data_sync as S

    with pytest.raises(SystemExit):
        S.main(["--source", "lhb"])


def test_sync_cli_uses_injected_fetcher_and_emits_bundle(tmp_path, capsys):
    import auxiliary_data_sync as S

    def fetcher(source, trade_day, **_):
        assert source == "lhb"
        assert trade_day == "2026-11-05"
        return A.AuxiliaryFetchResult(
            provider="fixture:lhb",
            publication_state="published",
            records=[{"代码": "600000", "龙虎榜净买额": 2}],
            coverage={"requested": "market-day", "received": 1, "failed": 0},
            reasons=[],
        )

    code = S.main([
        "--source", "lhb",
        "--trade-day", "2026-11-05",
        "--observed-at", "2026-11-05T18:30:00+08:00",
        "--output-root", str(tmp_path / "auxiliary"),
    ], fetcher=fetcher)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["evidence_status"] == "available"
    assert Path(payload["bundle_path"]).is_dir()


def test_sync_cli_rejects_orderbook_outside_live_window(tmp_path, monkeypatch):
    import auxiliary_data_sync as S

    symbols = tmp_path / "symbols.json"
    symbols.write_text('["600000.SH"]', encoding="utf-8")
    called = []
    monkeypatch.setattr(
        S,
        "fetch_source_records",
        lambda *_args, **_kwargs: called.append(True),
    )

    with pytest.raises(ValueError, match="live orderbook window"):
        S.main([
            "--source", "orderbook_snapshot",
            "--trade-day", "2026-11-05",
            "--observed-at", "2026-11-05T14:50:00+08:00",
            "--output-root", str(tmp_path / "auxiliary"),
            "--symbols-json", str(symbols),
        ], clock=lambda: datetime.fromisoformat("2026-11-05T16:00:00+08:00"))

    assert called == []


def test_sync_cli_rejects_stale_observed_time_inside_window(tmp_path):
    import auxiliary_data_sync as S

    symbols = tmp_path / "symbols.json"
    symbols.write_text('["600000.SH"]', encoding="utf-8")
    with pytest.raises(ValueError, match="live orderbook window"):
        S.main([
            "--source", "orderbook_snapshot",
            "--trade-day", "2026-11-05",
            "--observed-at", "2026-11-05T14:49:30+08:00",
            "--output-root", str(tmp_path / "auxiliary"),
            "--symbols-json", str(symbols),
        ], clock=lambda: datetime.fromisoformat("2026-11-05T14:50:10+08:00"))


def test_postclose_auxiliary_stage_is_per_source_idempotent():
    state = {"auxiliary_success_day": {"lhb": "2026-11-05"}}
    calls = []

    def runner(source, trade_day, observed_at):
        calls.append((source, trade_day, observed_at))
        return {"evidence_status": "available", "bundle_id": f"bundle-{source}"}

    result = D.run_auxiliary_stages(
        state=state,
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        stage_runner=runner,
    )

    assert [call[0] for call in calls] == ["block_trade", "stock_news"]
    assert result["lhb"]["status"] == "already_succeeded"
    assert state["auxiliary_success_day"] == {
        "lhb": "2026-11-05",
        "block_trade": "2026-11-05",
        "stock_news": "2026-11-05",
    }


def test_pending_auxiliary_stage_remains_retryable():
    state = {}

    result = D.run_auxiliary_stages(
        state=state,
        trade_day="2026-11-05",
        observed_at="2026-11-05T17:00:00+08:00",
        stage_runner=lambda *_args, **_kwargs: {
            "evidence_status": "pending_publication",
            "bundle_id": "pending-bundle",
        },
    )

    assert state.get("auxiliary_success_day", {}) == {}
    assert set(state["auxiliary_retryable"]) == {"lhb", "block_trade", "stock_news"}
    assert state["auxiliary_pending_days"] == {
        "lhb": ["2026-11-05"],
        "block_trade": ["2026-11-05"],
    }
    assert all(item["retryable"] for item in result.values())


def test_tplus1_backlog_is_replayed_before_current_day():
    state = {"auxiliary_pending_days": {"lhb": ["2026-11-05"]}}
    calls = []

    first = D.run_auxiliary_stages(
        state=state,
        trade_day="2026-11-06",
        observed_at="2026-11-06T18:00:00+08:00",
        stage_runner=lambda source, day, observed_at: calls.append((source, day)) or {
            "evidence_status": "available",
            "bundle_id": f"{source}-{day}",
        },
    )

    assert calls[0] == ("lhb", "2026-11-05")
    assert first["lhb"]["attempted_trade_day"] == "2026-11-05"
    assert state["auxiliary_pending_days"]["lhb"] == []

    calls.clear()
    second = D.run_auxiliary_stages(
        state=state,
        trade_day="2026-11-06",
        observed_at="2026-11-06T18:05:00+08:00",
        stage_runner=lambda source, day, observed_at: calls.append((source, day)) or {
            "evidence_status": "available",
            "bundle_id": f"{source}-{day}",
        },
    )

    assert calls == [("lhb", "2026-11-06")]
    assert second["lhb"]["attempted_trade_day"] == "2026-11-06"


def test_auxiliary_retry_is_throttled_until_interval_elapses():
    state = {
        "auxiliary_retryable": {
            "lhb": "2026-11-05T18:00:00+08:00",
            "block_trade": "2026-11-05T18:00:00+08:00",
            "stock_news": "2026-11-05T18:00:00+08:00",
        }
    }
    calls = []

    result = D.run_auxiliary_stages(
        state=state,
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:05:00+08:00",
        stage_runner=lambda source, *_args, **_kwargs: calls.append(source),
        throttle_seconds=1800,
    )

    assert calls == []
    assert all(item["status"] == "retry_throttled" for item in result.values())


def test_postclose_group_never_runs_orderbook():
    calls = []
    D.run_auxiliary_stages(
        state={},
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        stage_runner=lambda source, *_args, **_kwargs: calls.append(source) or {
            "evidence_status": "available",
            "bundle_id": source,
        },
    )

    assert calls == ["lhb", "block_trade", "stock_news"]


def test_auxiliary_stage_failure_is_contained_from_primary_round():
    result = D.run_auxiliary_stages(
        state={},
        trade_day="2026-11-05",
        observed_at="2026-11-05T18:30:00+08:00",
        stage_runner=lambda source, *_args, **_kwargs: (
            (_ for _ in ()).throw(OSError(f"{source} receipt write failed"))
        ),
    )

    assert set(result) == {"lhb", "block_trade", "stock_news"}
    assert all(item["evidence_status"] == "blocked" for item in result.values())
    assert all(item["affects_strategy"] is False for item in result.values())


def test_orderbook_failure_is_contained_and_non_authoritative():
    result = D.run_orderbook_stage(
        state={},
        trade_day="2026-11-05",
        observed_at="2026-11-05T14:50:00+08:00",
        symbols=["600000.SH"],
        stage_runner=lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(TimeoutError("quote timeout"))
        ),
    )

    assert result["evidence_status"] == "blocked"
    assert result["authoritative"] is False
    assert result["affects_strategy"] is False


def test_auxiliary_subprocess_timeout_is_contained(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["auxiliary"], timeout=12)

    monkeypatch.setattr(D.subprocess, "run", timeout)
    monkeypatch.setattr(D, "AUXILIARY_TIMEOUT_S", 12)
    monkeypatch.setattr(
        A,
        "run_auxiliary_collection",
        lambda **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["legacy-direct"], timeout=12)
        ),
    )

    result = D._run_auxiliary_source(
        "lhb", "2026-11-05", "2026-11-05T18:30:00+08:00"
    )

    assert result["evidence_status"] == "blocked"
    assert result["retryable"] is True
    assert "TimeoutExpired" in result["reasons"][0]


def test_run_round_wires_postclose_auxiliary_group(tmp_path, monkeypatch):
    mark = tmp_path / "data_update_last.json"
    calls = []
    monkeypatch.setattr(D, "MARK", str(mark))
    monkeypatch.setattr(D, "_now_min", lambda: D.T_AUX_RESEARCH)
    monkeypatch.setattr(D, "run_daily_completion", lambda **_: {"ok": True})
    monkeypatch.setattr(D, "_valu_stale_day", lambda: None)
    monkeypatch.setattr(D, "_in_publish_windows", lambda: False)
    monkeypatch.setattr(
        D,
        "_run_once",
        lambda stage, args, day=None: {"ok": True, "stage": stage, "out": ""},
    )
    monkeypatch.setattr(
        D,
        "_run_auxiliary_source",
        lambda source, trade_day, observed_at, symbols=None: calls.append(source) or {
            "evidence_status": "available",
            "bundle_id": source,
        },
    )

    result = D.run_round()

    assert calls == ["lhb", "block_trade", "stock_news"]
    assert set(result["auxiliary"]) == {"lhb", "block_trade", "stock_news"}
    saved = json.loads(mark.read_text(encoding="utf-8"))
    assert saved["auxiliary_success_day"] == {
        "lhb": saved["last_round_at"][:10],
        "block_trade": saved["last_round_at"][:10],
        "stock_news": saved["last_round_at"][:10],
    }


def test_force_round_does_not_bypass_auxiliary_publication_window(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MARK", str(tmp_path / "state.json"))
    monkeypatch.setattr(D, "_now_min", lambda: D.T_AUX_RESEARCH - 1)
    monkeypatch.setattr(D, "run_daily_completion", lambda **_: {"ok": True})
    monkeypatch.setattr(D, "_valu_stale_day", lambda: None)
    monkeypatch.setattr(D, "_in_publish_windows", lambda: False)
    monkeypatch.setattr(
        D, "_run_once",
        lambda stage, args, day=None: {"ok": True, "stage": stage, "out": ""},
    )
    called = []
    monkeypatch.setattr(
        D, "run_auxiliary_stages",
        lambda **kwargs: called.append(kwargs) or {},
    )

    result = D.run_round(force=True)

    assert "auxiliary" not in result
    assert called == []


def test_orderbook_stage_requires_explicit_tracked_symbols():
    calls = []

    missing = D.run_orderbook_stage(
        state={},
        trade_day="2026-11-05",
        observed_at="2026-11-05T14:50:00+08:00",
        symbols=[],
        stage_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert missing["evidence_status"] == "blocked"
    assert missing["coverage"]["requested"] == 0
    assert calls == []

    state = {}
    ready = D.run_orderbook_stage(
        state=state,
        trade_day="2026-11-05",
        observed_at="2026-11-05T14:50:00+08:00",
        symbols=["600000.SH", "600000.SH", "000001.SZ"],
        stage_runner=lambda source, trade_day, observed_at, symbols=None: calls.append(symbols) or {
            "evidence_status": "available",
            "bundle_id": "orderbook-bundle",
        },
    )
    assert calls == [["000001.SZ", "600000.SH"]]
    assert ready["evidence_status"] == "available"
    assert state["auxiliary_success_day"]["orderbook_snapshot"] == "2026-11-05"


def test_tracked_orderbook_symbols_use_same_day_artifacts_only(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "_BASE", str(tmp_path))
    day_dir = tmp_path / "data" / "daily" / "20261105"
    day_dir.mkdir(parents=True)
    (day_dir / "signal_snapshot_20261105.json").write_text(
        json.dumps({"date": "20261105", "weights": {"600000.SH": 0.1}}),
        encoding="utf-8",
    )
    (tmp_path / "data" / "live_state.json").write_text(
        json.dumps({"day": "2026-11-05", "positions": [{"canon": "000001.SZ"}]}),
        encoding="utf-8",
    )

    assert D._tracked_orderbook_symbols("2026-11-05") == ["000001.SZ", "600000.SH"]
    assert D._tracked_orderbook_symbols("2026-11-06") == []


def test_state_save_is_atomic_on_serialization_failure(tmp_path, monkeypatch):
    mark = tmp_path / "state.json"
    mark.write_text('{"stable":true}\n', encoding="utf-8")
    monkeypatch.setattr(D, "MARK", str(mark))

    def broken_dump(_value, handle, **_kwargs):
        handle.write('{"broken":')
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(D.json, "dump", broken_dump)
    with pytest.raises(RuntimeError, match="serialization failed"):
        D._save({"new": True})

    assert json.loads(mark.read_text(encoding="utf-8")) == {"stable": True}


def test_run_round_serializes_concurrent_state_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MARK", str(tmp_path / "state.json"))
    active = 0
    peak = 0
    guard = threading.Lock()

    def body(force=False):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"force": force}

    monkeypatch.setattr(D, "_run_round_body", body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: D.run_round(), range(2)))

    assert results == [{"force": False}, {"force": False}]
    assert peak == 1


def test_long_state_lock_contention_returns_retryable_busy(monkeypatch):
    @contextmanager
    def busy_lock():
        raise D.DataUpdateBusy("another round owns state lock")
        yield

    monkeypatch.setattr(D, "_state_file_lock", busy_lock)

    result = D.run_round()

    assert result == {
        "_status": "already_running",
        "retryable": True,
        "reason": "another round owns state lock",
    }


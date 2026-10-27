"""每日行情自动补全的先行契约测试。"""

import os
import json
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import data_update_daemon as D  # noqa: E402


def _plan(*, engine_day, h5i_watermark, expected_day, enabled=True):
    return D.plan_daily_completion(
        engine_day=engine_day,
        h5i_watermark=h5i_watermark,
        expected_day=expected_day,
        calendar_strength="official",
        enabled=enabled,
        today="2026-10-27",
        lookback_days=10,
    )


def test_whole_missing_trade_day_is_discovered_from_calendar():
    """已有日线没有 10-26 行时，也必须发现该交易日缺口。"""
    result = _plan(
        engine_day="2026-10-23",
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
    )

    assert result["action"] == "trigger"
    assert result["triggered"] is True
    assert result["missing_days"] == ["20261026"]


def test_primary_and_storage_caught_up_does_not_trigger_fallback():
    result = _plan(
        engine_day="2026-10-26",
        h5i_watermark="2026-10-26",
        expected_day="2026-10-26",
    )

    assert result["action"] == "no_gap"
    assert result["triggered"] is False
    assert result["missing_days"] == []


def test_storage_only_gap_does_not_use_backup_source():
    result = _plan(
        engine_day="2026-10-26",
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
    )

    assert result["action"] == "no_gap"
    assert result["triggered"] is False
    assert "摄入链路" in " ".join(result["reasons"])


def test_disabled_completion_does_not_even_calculate_missing_days():
    result = _plan(
        engine_day="2026-10-23",
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
        enabled=False,
    )

    assert result["action"] == "disabled"
    assert result["triggered"] is False
    assert result["missing_days"] == []


def test_weak_calendar_is_not_treated_as_no_gap():
    result = D.plan_daily_completion(
        engine_day="2026-10-23",
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
        calendar_strength="data_derived",
        enabled=True,
        today="2026-10-27",
        lookback_days=10,
    )

    assert result["action"] == "refuse_weak_calendar"
    assert result["triggered"] is False


def test_missing_observation_is_not_guessed():
    result = D.plan_daily_completion(
        engine_day=None,
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
        enabled=True,
        today="2026-10-27",
        lookback_days=10,
    )

    assert result["action"] == "cannot_decide"
    assert result["triggered"] is False


def test_trigger_with_zero_lookback_stays_explicitly_unresolved():
    result = D.plan_daily_completion(
        engine_day="2026-10-23",
        h5i_watermark="2026-10-23",
        expected_day="2026-10-26",
        enabled=True,
        today="2026-10-27",
        lookback_days=0,
    )

    assert result["action"] == "trigger"
    assert result["triggered"] is True
    assert result["missing_days"] == []


def _primary_result(*, engine_day="2026-10-23", h5i_day="2026-10-23"):
    return {
        "ok": True,
        "engine_last_day": engine_day.replace("-", ""),
        "h5i_max_before": h5i_day,
        "h5i_max_after": h5i_day,
        "expected_day": "20261026",
        "calendar_strength": "official",
    }


def test_orchestration_runs_primary_before_fallback_on_double_gap():
    events = []

    def primary():
        events.append("primary")
        return _primary_result()

    def fallback(days):
        events.append(("fallback", days))
        return {
            "ok": True,
            "written": days,
            "verify": {"ok": True, "h5i_advanced": True,
                       "engine_unchanged": True},
        }

    result = D.run_daily_completion(
        primary_runner=primary,
        fallback_runner=fallback,
        enabled=True,
        lookback_days=10,
        today="2026-10-27",
    )

    assert events == ["primary", ("fallback", ["20261026"])]
    assert result["ok"] is True
    assert result["decision"]["triggered"] is True
    assert result["fallback"]["written"] == ["20261026"]


def test_orchestration_does_not_fallback_when_primary_is_caught_up():
    called = []

    def fallback(days):
        called.append(days)
        return {"ok": True, "written": days}

    result = D.run_daily_completion(
        primary_runner=lambda: _primary_result(
            engine_day="2026-10-26", h5i_day="2026-10-26"),
        fallback_runner=fallback,
        enabled=True,
        lookback_days=10,
        today="2026-10-27",
    )

    assert result["ok"] is True
    assert result["decision"]["action"] == "no_gap"
    assert called == []


def test_orchestration_does_not_fallback_for_storage_only_gap():
    called = []

    result = D.run_daily_completion(
        primary_runner=lambda: _primary_result(
            engine_day="2026-10-26", h5i_day="2026-10-23"),
        fallback_runner=lambda days: called.append(days),
        enabled=True,
        lookback_days=10,
        today="2026-10-27",
    )

    assert result["ok"] is True
    assert result["decision"]["action"] == "no_gap"
    assert called == []


def test_orchestration_rejects_partial_fallback_and_keeps_retryable():
    result = D.run_daily_completion(
        primary_runner= lambda: _primary_result(),
        fallback_runner=lambda days: {
            "ok": True,
            "written": [],
            "verify": {"ok": True, "h5i_advanced": True,
                       "engine_unchanged": True},
        },
        enabled=True,
        lookback_days=10,
        today="2026-10-27",
    )

    assert result["ok"] is False
    assert result["retryable"] is True
    assert "written" in result["error"]


def test_run_round_persists_primary_fallback_receipt(tmp_path, monkeypatch):
    mark = tmp_path / "data_update_last.json"
    monkeypatch.setattr(D, "MARK", str(mark))
    monkeypatch.setattr(D, "_now_min", lambda: D.T_CLOSE_HINT)
    monkeypatch.setattr(D, "_primary_daily_sync", lambda: _primary_result())
    monkeypatch.setattr(
        D,
        "_run_backfill_subprocess",
        lambda days: {
            "ok": True,
            "written": days,
            "verify": {"ok": True, "h5i_advanced": True,
                       "engine_unchanged": True},
        },
    )
    monkeypatch.setattr(D, "_valu_stale_day", lambda: None)
    monkeypatch.setattr(
        D,
        "_run_once",
        lambda stage, args, day=None: {
            "ok": True,
            "stage": stage,
            "out": '{"missing_legs": {}}',
        },
    )

    D.run_round()

    saved = mark.read_text(encoding="utf-8")
    assert '"last_round"' in saved
    assert '"daily"' in saved
    assert '"20261026"' in saved


def test_backfill_timeout_preserves_subprocess_output_for_diagnosis(monkeypatch):
    monkeypatch.setattr(D, "_backfill_interpreter", lambda: "python.exe")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output="baostock login blocked")

    monkeypatch.setattr(D.subprocess, "run", timeout)

    result = D._run_backfill_subprocess(["20261026"])

    assert result["ok"] is False
    assert "TimeoutExpired" in result["error"]
    assert "baostock login blocked" in result["output_tail"]
    assert result["timeout_s"] == D.BACKFILL_TIMEOUT_S


def test_daily_failure_receipt_survives_later_round_without_daily_stage(
    tmp_path, monkeypatch
):
    mark = tmp_path / "data_update_last.json"
    monkeypatch.setattr(D, "MARK", str(mark))
    monkeypatch.setattr(D, "_now_min", lambda: D.T_CLOSE_HINT)
    monkeypatch.setattr(D, "_primary_daily_sync", lambda: _primary_result())
    monkeypatch.setattr(
        D,
        "_run_backfill_subprocess",
        lambda days: {"ok": False, "written": [], "error": "login timeout"},
    )
    monkeypatch.setattr(D, "_valu_stale_day", lambda: None)
    monkeypatch.setattr(
        D,
        "_run_once",
        lambda stage, args, day=None: {
            "ok": True,
            "stage": stage,
            "out": '{"missing_legs": {}}',
        },
    )

    first = D.run_round()
    first_saved = json.loads(mark.read_text(encoding="utf-8"))
    assert first["daily"]["ok"] is False
    assert first_saved["daily_last"]["error"]

    second = D.run_round()
    second_saved = json.loads(mark.read_text(encoding="utf-8"))
    assert "daily" not in second
    assert second_saved["daily_last"] == first_saved["daily_last"]

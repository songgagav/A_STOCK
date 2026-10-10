"""Reward config is normalized, scheduled, atomic, and traceable."""

import json
import math

import pytest

import incremental_learn as inc
import run_daily
import trading_calendar as calendar


@pytest.fixture
def reward_path(monkeypatch, tmp_path):
    path = tmp_path / "reward_config.json"
    monkeypatch.setattr(inc, "REWARD_CONFIG_FILE", str(path))
    monkeypatch.setattr(calendar, "CAL_FILE", str(tmp_path / "calendar.json"))
    monkeypatch.setattr(inc, "_generation_day", lambda: inc.dt.date(2026, 10, 9), raising=False)
    return path


def _calendar():
    with open(calendar.CAL_FILE, "w", encoding="utf-8") as handle:
        json.dump({"days": ["20261009", "20261012", "20261014"],
                   "source": "akshare_tool_trade_date_hist_sina",
                   "updated": "2026-10-09 19:00:00"}, handle)


def test_missing_reward_config_normalizes_default(reward_path):
    weights = inc.get_reward_weights()
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["vnpy_weight"] == pytest.approx(12 / 23)
    assert weights["ic_weight"] == pytest.approx(8 / 23)
    assert weights["attr_weight"] == pytest.approx(3 / 23)


@pytest.mark.parametrize("values", [
    {"vnpy_weight": -1, "ic_weight": 1, "attr_weight": 0},
    {"vnpy_weight": math.nan, "ic_weight": 1, "attr_weight": 0},
    {"vnpy_weight": math.inf, "ic_weight": 1, "attr_weight": 0},
    {"vnpy_weight": 0, "ic_weight": 0, "attr_weight": 0},
])
def test_invalid_explicit_weights_are_rejected(values):
    with pytest.raises(ValueError):
        inc.normalize_reward_weights(values)


def test_large_finite_reward_weights_normalize_without_overflow():
    weights = inc.normalize_reward_weights(dict.fromkeys(
        ("vnpy_weight", "ic_weight", "attr_weight"), 1e308))
    assert list(weights.values()) == pytest.approx([1 / 3, 1 / 3, 1 / 3])


@pytest.mark.parametrize("raw", ['{"vnpy_weight":', '{}', '[]',
    '{"schema_version": 99, "vnpy_weight": 0.4, "ic_weight": 0.4, "attr_weight": 0.2}'])
def test_invalid_config_read_is_traced_degraded_default(reward_path, raw):
    reward_path.write_text(raw, encoding="utf-8")
    state = inc.get_reward_weight_state(as_of="2026-10-12")
    assert state["status"] == "degraded_default"
    assert state["reason"]
    assert sum(state["weights"].values()) == pytest.approx(1)


def test_reward_config_takes_effect_on_next_authoritative_session(reward_path):
    _calendar()
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-09")
    saved = json.loads(reward_path.read_text(encoding="utf-8"))
    assert saved["generated_on"] == "2026-10-09"
    assert saved["effective_from"] == "2026-10-12"
    assert saved["calendar_status"] == "resolved"
    assert inc.get_reward_weight_state(as_of="2026-10-09")["status"] == "pending_effective"
    assert inc.get_reward_weights(as_of="2026-10-12")["attr_weight"] == 0.25


def test_unknown_calendar_does_not_guess_weekday(reward_path):
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-09")
    saved = json.loads(reward_path.read_text(encoding="utf-8"))
    assert saved["effective_from"] is None
    assert saved["calendar_status"] == "pending_calendar_resolution"
    state = inc.get_reward_weight_state(as_of="2026-10-14")
    assert state["status"] == "pending_calendar_resolution"
    assert state["weights"]["attr_weight"] == pytest.approx(3 / 23)


def test_future_generated_config_cannot_supply_historical_weights(reward_path):
    _calendar()
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-12")
    state = inc.get_reward_weight_state(as_of="2026-10-09")
    assert state["status"] == "degraded_default"
    assert "future" in state["reason"]


def test_data_derived_calendar_without_future_sessions_is_pending(reward_path):
    with open(calendar.CAL_FILE, "w", encoding="utf-8") as handle:
        json.dump({"days": ["20250929", "20250930"], "source": "h5i:daily_bars"}, handle)
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2025-09-29")
    assert json.loads(reward_path.read_text(encoding="utf-8"))["effective_from"] is None


def test_attr_weight_survives_parse_and_daily_update(reward_path):
    _calendar()
    parsed = inc._parse_response({"content": [{"type": "text", "text": json.dumps({
        "reward_rebalance": {"vnpy_weight": 0.5, "ic_weight": 0.25, "attr_weight": 0.25},
    })}]})
    assert parsed["reward_rebalance"]["attr_weight"] == 0.25
    result = run_daily.apply_incremental_reward_update(
        {"triggered": True, "optimization": parsed}, "2026-10-09")
    assert result["reward_config_written"] is True
    assert inc.get_reward_weights(as_of="2026-10-12")["attr_weight"] == 0.25


def test_incremental_learn_result_preserves_attr_to_actual_config(reward_path, monkeypatch):
    import config, degradation, arctic_store
    from types import SimpleNamespace
    monkeypatch.setattr(config, "DATA_DIR", str(reward_path.parent))
    monkeypatch.setattr(inc, "_load_dotenv", lambda: None)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setattr(degradation, "run_full_check", lambda **kwargs: {
        "incremental_samples": [{"day": "2026-10-09"}] * 3,
        "degradation_index": {"worst_level": "P1", "overall_score": 0.5}})
    monkeypatch.setattr(inc, "_post_anthropic", lambda *args, **kwargs: {
        "body": {"content": [{"type": "text", "text": json.dumps({
            "reward_rebalance": {"vnpy_weight": 0.5, "ic_weight": 0.25, "attr_weight": 0.25}})}]},
        "latency": 0})
    monkeypatch.setattr(arctic_store, "get_store", lambda: SimpleNamespace(_lib=lambda name: None))
    _calendar()
    result = inc.run_incremental_learn("2026-10-09")
    assert result["triggered"] is True, result
    assert result["optimization"]["reward_rebalance"]["attr_weight"] == 0.25
    receipt = run_daily.apply_incremental_reward_update(result, "2026-10-09")
    assert receipt["reward_config_written"] is True
    assert inc.get_reward_weights(as_of="2026-10-12")["attr_weight"] == 0.25


def test_failed_atomic_write_preserves_config_and_daily_receipt_is_false(reward_path, monkeypatch):
    _calendar()
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-09")
    before = reward_path.read_bytes()
    def fail_write(path, payload):
        raise OSError("simulated replacement failure")
    monkeypatch.setattr(inc, "atomic_write_json", fail_write)
    result = run_daily.apply_incremental_reward_update({"triggered": True, "optimization": {
        "reward_rebalance": {"vnpy_weight": 0.7, "ic_weight": 0.2, "attr_weight": 0.1},
    }}, "2026-10-12")
    assert result["reward_config_written"] is False
    assert result["error"]
    assert reward_path.read_bytes() == before


def test_backfill_processing_day_does_not_backdate_generation(reward_path):
    _calendar()
    receipt = run_daily.apply_incremental_reward_update({"triggered": True, "optimization": {
        "reward_rebalance": {"vnpy_weight": 0.5, "ic_weight": 0.25, "attr_weight": 0.25},
    }}, "2026-09-01")
    assert receipt["reward_config_written"] is True
    saved = json.loads(reward_path.read_text(encoding="utf-8"))
    assert saved["generated_on"] == "2026-10-09"
    assert saved["processing_day"] == "2026-09-01"
    assert inc.get_reward_weight_state(as_of="2026-09-02")["status"] == "degraded_default"


@pytest.mark.parametrize("change", ["saturday", "missing_effective", "missing_calendar"])
def test_invalid_schedule_claims_degrade(reward_path, change):
    _calendar()
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-09")
    saved = json.loads(reward_path.read_text(encoding="utf-8"))
    if change == "saturday":
        saved["effective_from"] = "2026-10-10"
    elif change == "missing_calendar":
        saved.pop("calendar_identity")
    else:
        saved["calendar_status"] = "pending_calendar_resolution"
        saved.pop("effective_from")
    reward_path.write_text(json.dumps(saved), encoding="utf-8")
    assert inc.get_reward_weight_state(as_of="2026-10-12")["status"] == "degraded_default"


def test_corrupt_previous_config_degradation_remains_active_provenance(reward_path):
    _calendar()
    reward_path.write_text("{bad", encoding="utf-8")
    receipt = run_daily.apply_incremental_reward_update({"triggered": True, "optimization": {
        "reward_rebalance": {"vnpy_weight": 0.5, "ic_weight": 0.25, "attr_weight": 0.25},
    }}, "2026-10-09")
    assert receipt["reward_config_written"] is True
    state = receipt["reward_config_state"]
    assert state["status"] == "pending_effective"
    assert state["active_provenance"]["status"] == "degraded_default"
    assert "JSONDecodeError" in state["active_provenance"]["reason"]
    assert json.loads(reward_path.read_text(encoding="utf-8"))["previous_provenance"] == state["active_provenance"]


def test_resolved_calendar_identity_must_match_current_authoritative_cache(reward_path):
    _calendar()
    assert inc.set_reward_weights(0.5, 0.25, 0.25, generated_on="2026-10-09")
    with open(calendar.CAL_FILE, "w", encoding="utf-8") as handle:
        json.dump({"days": ["20261009", "20261012", "20261014"],
                   "source": "akshare_tool_trade_date_hist_sina",
                   "updated": "2026-10-09 20:00:00"}, handle)
    state = inc.get_reward_weight_state(as_of="2026-10-12")
    assert state["status"] == "degraded_default"
    assert "authoritative calendar evidence" in state["reason"]


def test_parser_rejects_boolean_reward_weight():
    with pytest.raises(RuntimeError, match="invalid reward"):
        inc._parse_response({"content": [{"text": json.dumps({"reward_rebalance": {
            "vnpy_weight": True, "ic_weight": 0.25, "attr_weight": 0.25}})}]})


def test_parser_normalizes_explicit_three_weight_recommendation():
    parsed = inc._parse_response({"content": [{"text": json.dumps({"reward_rebalance": {
        "vnpy_weight": 4, "ic_weight": 2, "attr_weight": 2}})}]})["reward_rebalance"]
    assert [parsed[key] for key in ("vnpy_weight", "ic_weight", "attr_weight")] == pytest.approx(
        [0.5, 0.25, 0.25])

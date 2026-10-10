"""Today's brief cannot become a feature or prior in historical replay."""

import datetime as dt
import json
from types import SimpleNamespace

import numpy as np
import pytest

import drl_train as drl


def _brief(sign):
    return {
        "stance": "加仓" if sign > 0 else "减仓",
        "sentiment_factors": dict.fromkeys(
            ("risk_on_off", "rotation_intensity", "liquidity_stress", "policy_catalyst"), sign),
        "factor_recommendations": {drl.SCORE_FACTORS[0]: 1.5 if sign > 0 else 0.5},
        "provenance": {"generated_on": "2026-10-09", "source": "injected-fixture"},
    }


def test_loaded_brief_retains_generation_and_effective_provenance(monkeypatch, tmp_path):
    import pre_drl_brief as loader
    monkeypatch.setattr(loader, "DATA_DIR", str(tmp_path))
    directory = tmp_path / "drl" / "20261009"
    directory.mkdir(parents=True)
    (directory / "pre_drl_brief.json").write_text(json.dumps({
        "ok": True, "brief": {"stance": "维持"},
        "meta": {"generated_at": "2026-10-09 19:00:00",
                 "effective_day": {"requested": "2026-10-09", "market": "2026-10-09"}},
    }), encoding="utf-8")
    brief = loader.load_pre_drl_brief("20261009")
    assert brief["provenance"]["requested_day"] == "2026-10-09"
    assert brief["provenance"]["generated_at"] == "2026-10-09 19:00:00"
    assert brief["provenance"]["effective_day"]["market"] == "2026-10-09"


@pytest.mark.parametrize("kind", ["weight", "value"])
def test_historical_environments_ignore_todays_brief(kind):
    states, weights = [], []
    for sign in (1, -1):
        if kind == "weight":
            env = drl.FactorWeightEnv(np.full((16, 2), 0.1, dtype=np.float32),
                                     np.array([0.5, 0.5], dtype=np.float32),
                                     lookback=3, brief=_brief(sign))
        else:
            env = drl.FactorValueEnv(np.full((16, 2), 0.1, dtype=np.float32),
                                    np.full((16, 2), 0.02, dtype=np.float32),
                                    np.full(16, 0.01, dtype=np.float32),
                                    lookback=3, brief=_brief(sign))
        observation, _ = env.reset(seed=7)
        states.append(observation)
        _, _, _, _, info = env.step(np.array([0.5, -0.2], dtype=np.float32))
        weights.append(info["weights"])
    np.testing.assert_array_equal(states[0], states[1])
    np.testing.assert_array_equal(states[0][-8:-4], np.zeros(4, dtype=np.float32))
    assert states[0][-4] == 1.0
    np.testing.assert_array_equal(weights[0], weights[1])


def test_orchestration_keeps_historical_prior_and_records_separate_inference(monkeypatch, tmp_path):
    n_factors = len(drl.SCORE_FACTORS)
    base = np.full(n_factors, 1 / n_factors, dtype=np.float32)
    history = np.array([[np.sin((i + 1) * (j + 1)) * 0.1 for j in range(n_factors)]
                        for i in range(40)], dtype=np.float32)
    trained_envs = []

    class ModelBoundary:
        """Replace expensive PPO training, preserving the real replay environment."""
        def __init__(self, policy, env, **kwargs):
            self.env = env
            self.num_timesteps = 0
            trained_envs.append(env)

        def learn(self, total_timesteps):
            self.num_timesteps = total_timesteps
            return self

        def predict(self, obs, deterministic):
            return np.zeros(n_factors, dtype=np.float32), None

        def save(self, path):
            pass  # Model file eligibility is an external degradation boundary below.

    monkeypatch.setattr(drl, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(drl, "CVaR_PPO", ModelBoundary)
    monkeypatch.setattr(drl, "_load_true_factor_state", lambda day, count: (
        history.copy(), np.full(40, 0.01, dtype=np.float32),
        [dt.date(2026, 8, 1) + dt.timedelta(days=i) for i in range(40)]))
    monkeypatch.setattr(drl, "_load_base_weights", lambda: base.copy())
    monkeypatch.setattr(drl, "_load_perf_report", lambda: {})
    monkeypatch.setattr(drl, "_build_target_plan", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(drl.drl_degrade, "load_pointer", lambda: {})
    monkeypatch.setattr(drl.drl_degrade, "resolve", lambda day, **kwargs: {
        "ok": True, "halt": False, "level": 0, "source_day": day,
        "effective_weights": kwargs["final_weights"],
    })
    import drl_precheck, neural_ta, arctic_store, drl_drift
    monkeypatch.setattr(drl_precheck, "load_net_values", lambda day: None)
    monkeypatch.setattr(neural_ta, "compute_tuning", lambda *args, **kwargs: {"mode": "fallback"})
    monkeypatch.setattr(arctic_store, "get_store", lambda: SimpleNamespace(append_reward_curve=lambda *args: False))
    monkeypatch.setattr(drl_drift, "check_weight_drift", lambda *args, **kwargs: {})
    monkeypatch.setattr(drl_drift, "check_extreme_weights", lambda *args, **kwargs: {})
    results = [drl.run_drl_train("2026-10-09", total_timesteps=8, use_vnpy_reward=False,
                                brief=_brief(sign)) for sign in (1, -1)]
    assert all(result["ok"] for result in results), results
    assert results[0]["prior_weights"] == results[1]["prior_weights"]
    assert results[0]["training_weights"] == results[1]["training_weights"]
    np.testing.assert_array_equal(trained_envs[0].reset(seed=7)[0], trained_envs[1].reset(seed=7)[0])
    for result in results:
        assert result["training_brief_used"] is False
        assert result["final_weights"] == result["training_weights"]
        assert result["current_inference_brief"]["provenance"]["generated_on"] == "2026-10-09"
    assert results[0]["inference_weights"] != results[1]["inference_weights"]
    saved = json.loads((tmp_path / "drl" / "20261009" / "train_meta.json").read_text(encoding="utf-8"))
    assert saved["training_brief_used"] is False
    assert saved["training_weights"] != saved["inference_weights"]
    value_result = drl.run_factor_value_drl(
        "20261009", factor_history=history.copy(), future_returns=history.copy(),
        returns=np.linspace(0.001, 0.01, 40), brief=_brief(1),
        regime_features=np.full((40, 3), 0.5, dtype=np.float32),
        total_timesteps=8, use_risk_factors=False,
    )
    assert value_result["ok"], value_result
    assert value_result["training_brief_used"] is False
    assert value_result["training_weights"] == value_result["final_weights"]
    assert value_result["current_inference_brief"]["applied"] is False

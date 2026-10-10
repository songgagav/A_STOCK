# -*- coding: utf-8 -*-
"""Phase C1: temporal and reward-contract tests (red before implementation)."""

from __future__ import annotations

import numpy as np
import pytest

from drl_train import FactorValueEnv, FactorWeightEnv
from drl_v2_contract import (
    build_temporal_split,
    combine_reward_components,
    cross_sectional_rank_ic,
    factor_long_short_return,
)
import drl_post


def test_temporal_split_has_disjoint_train_purge_validation_ranges():
    split = build_temporal_split(
        n_rows=30,
        lookback=5,
        val_days=4,
        purge_days=3,
    )

    assert split.train == tuple(range(5, 23))
    assert split.purge == (23, 24, 25)
    assert split.validation == (26, 27, 28, 29)
    assert not set(split.train) & set(split.purge)
    assert not set(split.train) & set(split.validation)
    assert not set(split.purge) & set(split.validation)


def test_validation_report_exposes_purge_without_training_on_it():
    split = drl_post.split_validation(30, lookback=5, val_days=4, purge_days=3)

    assert split["train"] == list(range(5, 23))
    assert split["purge"] == [23, 24, 25]
    assert split["val"] == [26, 27, 28, 29]


def test_reward_components_are_explicitly_aligned_and_weighted():
    components = {
        "ic": np.array([0.0, 0.1, 0.2]),
        "vnpy": np.array([0.3, 0.4, 0.5]),
        "attr": np.array([-0.1, 0.0, 0.1]),
    }

    rewards = combine_reward_components(
        components,
        {"ic": 0.5, "vnpy": 0.25, "attr": 0.25},
        expected_length=3,
    )

    np.testing.assert_allclose(rewards, [0.05, 0.15, 0.25])


def test_finite_large_reward_weights_are_normalized_before_multiplication():
    components = {"ic": np.full(10, 0.1), "vnpy": np.full(10, 0.3), "attr": np.full(10, -0.1)}
    weights = dict.fromkeys(components, 1e308)
    np.testing.assert_allclose(combine_reward_components(components, weights, expected_length=10), 0.1)
    env = FactorWeightEnv(np.full((10, 2), 0.1), np.array([0.5, 0.5]),
                          lookback=3, reward_components=components, reward_weights=weights)
    env.reset(seed=7)
    _, reward, _, _, _ = env.step(np.zeros(2))
    assert np.isfinite(reward)


def test_reward_weights_without_aligned_components_are_rejected():
    with pytest.raises(ValueError, match="without components"):
        combine_reward_components({}, {"ic": 1.0}, expected_length=1)


def test_rank_ic_uses_average_ties_and_drops_nonfinite_pairs():
    scores = np.array([1.0, 1.0, 2.0, np.nan, 4.0])
    future = np.array([0.1, 0.2, 0.3, 0.4, np.inf])
    assert cross_sectional_rank_ic(scores, future, min_samples=3) == pytest.approx(
        0.8660254, rel=1e-6)


def test_long_short_return_respects_direction_and_quantile():
    scores = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    future = np.array([-0.05, -0.02, 0.0, 0.03, 0.06])
    assert factor_long_short_return(scores, future, quantile=0.2) == pytest.approx(0.11)
    assert factor_long_short_return(scores, future, direction=-1, quantile=0.2) == pytest.approx(-0.11)


def test_factor_metrics_return_nan_when_label_maturity_is_insufficient():
    scores = np.array([1.0, 2.0, 3.0, np.nan])
    future = np.array([0.1, np.nan, np.nan, 0.2])
    assert np.isnan(cross_sectional_rank_ic(scores, future, min_samples=3))
    assert np.isnan(factor_long_short_return(scores, future, min_samples=3))


def test_factor_weight_env_does_not_expose_current_regime_to_current_reward():
    ic = np.zeros((8, 2), dtype=np.float32)
    base = np.array([0.5, 0.5], dtype=np.float32)
    regime = np.arange(24, dtype=np.float32).reshape(8, 3)
    env = FactorWeightEnv(ic, base, lookback=3, regime_features=regime)

    obs, _ = env.reset(seed=7)

    # The first action is evaluated at t=lookback.  The state may only expose
    # information through t-1, never regime_features[t].
    np.testing.assert_array_equal(obs[-3:], regime[2])


def test_factor_weight_env_external_reward_enters_step_reward():
    ic = np.zeros((8, 2), dtype=np.float32)
    base = np.array([0.5, 0.5], dtype=np.float32)
    external = {
        "ic": np.zeros(8, dtype=np.float32),
        "vnpy": np.arange(8, dtype=np.float32),
        "attr": np.zeros(8, dtype=np.float32),
    }
    env = FactorWeightEnv(
        ic,
        base,
        lookback=3,
        reward_components=external,
        reward_weights={"ic": 0.5, "vnpy": 0.5, "attr": 0.0},
    )

    env.reset(seed=7)
    _, reward, _, _, info = env.step(np.zeros(2, dtype=np.float32))

    assert reward == 1.5
    assert info["reward_components"]["external"] == 1.5


def test_factor_weight_env_can_stop_learning_before_validation_tail():
    ic = np.zeros((10, 2), dtype=np.float32)
    base = np.array([0.5, 0.5], dtype=np.float32)
    env = FactorWeightEnv(ic, base, lookback=3, episode_end=6)

    env.reset(seed=7)
    done = False
    steps = 0
    while not done:
        _, _, done, _, _ = env.step(np.zeros(2, dtype=np.float32))
        steps += 1

    assert steps == 3


def test_factor_weight_env_seed_reproduces_stochastic_transition():
    ic = np.zeros((8, 2), dtype=np.float32)
    base = np.array([0.5, 0.5], dtype=np.float32)
    kwargs = {"temperature": 1.5}

    first = FactorWeightEnv(ic, base, lookback=3, tuning=kwargs)
    second = FactorWeightEnv(ic, base, lookback=3, tuning=kwargs)
    first.reset(seed=11)
    second.reset(seed=11)
    first_step = first.step(np.array([0.2, -0.3], dtype=np.float32))
    second_step = second.step(np.array([0.2, -0.3], dtype=np.float32))

    np.testing.assert_allclose(first_step[0], second_step[0])
    assert first_step[1] == second_step[1]


def test_factor_value_env_regime_is_lagged_and_seeded():
    factor_history = np.zeros((10, 2), dtype=np.float32)
    future_returns = np.zeros((10, 2), dtype=np.float32)
    returns = np.zeros(10, dtype=np.float32)
    regime = np.arange(30, dtype=np.float32).reshape(10, 3)
    env = FactorValueEnv(
        factor_history, future_returns, returns, lookback=3,
        regime_features=regime,
    )
    obs, _ = env.reset(seed=11)
    np.testing.assert_array_equal(obs[-3:], regime[2])


def test_factor_value_env_episode_end_is_explicit():
    factor_history = np.zeros((10, 2), dtype=np.float32)
    future_returns = np.zeros((10, 2), dtype=np.float32)
    returns = np.zeros(10, dtype=np.float32)
    env = FactorValueEnv(
        factor_history, future_returns, returns, lookback=3,
        episode_end=6,
    )
    env.reset(seed=3)
    done = False
    steps = 0
    while not done:
        _, _, done, _, _ = env.step(np.zeros(2, dtype=np.float32))
        steps += 1
    assert steps == 3

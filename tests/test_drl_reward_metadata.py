# -*- coding: utf-8 -*-
"""Reward metadata must distinguish diagnostics from aligned PPO inputs."""

from pathlib import Path


def test_drl_train_labels_execution_and_attribution_as_diagnostics():
    source = (Path(__file__).parents[1] / "src" / "drl_train.py").read_text(encoding="utf-8")

    assert '"diagnostic_metrics"' in source
    assert '"execution_diagnostic"' in source
    assert '"attribution_diagnostic"' in source
    assert '"vnpy_reward":' not in source
    assert '"attr_reward":' not in source
    assert '"sortino_reward":' not in source

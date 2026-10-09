"""Regression tests for collection-level core/DRL test partitioning."""

from __future__ import annotations

import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
MANIFEST = REPO / "tests" / "ci_test_partitions.json"


EXPECTED_DRL_TESTS = {
    "tests/test_cvar_config.py",
    "tests/test_drl_factor_state_h5i.py",
    "tests/test_drl_target_plan_h5i.py",
    "tests/test_drl_true_factor_state.py",
    "tests/test_drl_v2_contract.py",
    "tests/test_drl_weights_contract.py",
    "tests/test_execution_decomposition.py",
    "tests/test_factor_dynamic_weights.py",
    "tests/test_factor_value_env.py",
    "tests/test_factor_weight_env_bounds.py",
    "tests/test_llm_drl_integration.py",
    "tests/test_regime_aware.py",
    "tests/test_risk_factor_integration.py",
    "tests/test_risk_factor_optimizer.py",
    "tests/test_run_factor_value_drl_risk.py",
}


def test_ci_workflow_selects_partition_without_manual_ignore_arguments():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "CI_TEST_PARTITION: core" in text
    assert "CI_TEST_PARTITION: drl" in text
    assert "--ignore=" not in text


def test_partition_manifest_covers_every_drl_collection_only_module():
    assert MANIFEST.exists(), "the collection partition must have one declarative source"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert set(manifest["drl"]) == EXPECTED_DRL_TESTS


def test_collection_filter_is_symmetric_for_core_and_drl():
    from ci_test_partition import should_ignore

    drl_test = REPO / "tests" / "test_drl_v2_contract.py"
    core_test = REPO / "tests" / "test_alpha_evidence.py"

    assert should_ignore(drl_test, "core", REPO)
    assert not should_ignore(drl_test, "drl", REPO)
    assert not should_ignore(core_test, "core", REPO)
    assert should_ignore(core_test, "drl", REPO)


def test_drl_weight_contract_is_excluded_from_core_collection():
    from ci_test_partition import should_ignore

    drl_test = REPO / "tests" / "test_drl_weights_contract.py"

    assert should_ignore(drl_test, "core", REPO)
    assert not should_ignore(drl_test, "drl", REPO)

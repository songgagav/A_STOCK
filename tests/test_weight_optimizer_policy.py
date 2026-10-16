"""Optimizer persistence respects the selector-weight authority boundary."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import selector_weight_policy as policy


OPTIMIZER_RESULT = {
    "weights": {"signal": 0.7, "trend": 0.3},
    "meta": {
        "generated": "2026-10-15T16:00:00+08:00", "icir": {},
        "window": 40, "alpha_share": 0.28, "static_base": {},
    },
}


def test_frozen_optimizer_persists_proposal_without_touching_weights_json(
    tmp_path, monkeypatch,
):
    authoritative = tmp_path / "data" / "weights.json"
    authoritative.parent.mkdir(parents=True)
    authoritative.write_bytes(b'{"weights":{"signal":0.5}}\n')
    snapshot = tmp_path / "data" / "evolution" / "selector_weights" / ("a" * 64 + ".json")
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"immutable snapshot bytes")
    weights_before = authoritative.read_bytes()
    snapshot_before = snapshot.read_bytes()
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")

    result = policy.persist_optimizer_weights("2026-10-15", OPTIMIZER_RESULT)

    proposal_path = Path(result["path"])
    proposal_bytes = proposal_path.read_bytes()
    assert result["mode"] == "frozen"
    assert result["status"] == "proposal_only"
    assert result["authoritative"] is False
    assert result["sha256"] == hashlib.sha256(proposal_bytes).hexdigest()
    assert proposal_path.parent == tmp_path / "data" / "weight_proposals" / "2026-10-15"
    proposal = json.loads(proposal_bytes.decode("utf-8"))
    assert proposal["status"] == "proposal_only"
    assert proposal["optimizer_result"] == OPTIMIZER_RESULT
    assert authoritative.read_bytes() == weights_before
    assert snapshot.read_bytes() == snapshot_before


def test_frozen_reset_is_rejected_without_deleting_inputs(tmp_path, monkeypatch):
    import weight_optimizer

    authoritative = tmp_path / "weights.json"
    authoritative.write_bytes(b"authoritative sentinel")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(b"frozen sentinel")
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setattr(weight_optimizer, "WEIGHTS_FILE", str(authoritative))
    monkeypatch.setattr(sys, "argv", ["weight_optimizer.py", "--reset"])

    code = weight_optimizer.main()

    assert code != 0
    assert authoritative.read_bytes() == b"authoritative sentinel"
    assert snapshot.read_bytes() == b"frozen sentinel"


def test_frozen_cli_write_uses_proposal_only_path(tmp_path, monkeypatch):
    import weight_optimizer

    authoritative = tmp_path / "data" / "weights.json"
    authoritative.parent.mkdir(parents=True)
    authoritative.write_bytes(b"do not replace")
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(weight_optimizer, "WEIGHTS_FILE", str(authoritative))
    monkeypatch.setattr(weight_optimizer, "optimize_weights", lambda **_kwargs: OPTIMIZER_RESULT)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setattr(
        sys, "argv", ["weight_optimizer.py", "--write", "--day", "2026-10-15"],
    )

    assert weight_optimizer.main() == 0
    assert authoritative.read_bytes() == b"do not replace"
    proposals = list((tmp_path / "data" / "weight_proposals" / "2026-10-15").glob("*.json"))
    assert len(proposals) == 1
    assert json.loads(proposals[0].read_text(encoding="utf-8"))["status"] == "proposal_only"


def test_adaptive_optimizer_write_preserves_weights_json_contract(tmp_path, monkeypatch):
    target = tmp_path / "data" / "weights.json"
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "adaptive")

    result = policy.persist_optimizer_weights(
        "2026-10-15", OPTIMIZER_RESULT, authoritative_path=target,
    )

    assert result["mode"] == "adaptive"
    assert result["status"] == "written"
    assert result["authoritative"] is True
    assert result["path"] == str(target)
    assert json.loads(target.read_text(encoding="utf-8")) == OPTIMIZER_RESULT


def test_frozen_proposal_rejects_noncanonical_or_path_like_day(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")

    with pytest.raises(policy.SelectorWeightPolicyError):
        policy.persist_optimizer_weights("../2026-10-15", OPTIMIZER_RESULT)

    assert not (tmp_path / "data").exists()


def test_proposal_write_failure_is_nonblocking_in_daily_feedback(tmp_path, monkeypatch):
    import run_daily
    import weight_optimizer

    class FakeICTrack:
        @staticmethod
        def settle_mature_days(*_args, **_kwargs):
            return 0

        @staticmethod
        def compute_day_ic(*_args, **_kwargs):
            return {}

    monkeypatch.setitem(sys.modules, "ic_track", FakeICTrack)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")
    monkeypatch.setattr(run_daily, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(weight_optimizer, "WEIGHTS_FILE", str(tmp_path / "weights.json"))
    monkeypatch.setattr(weight_optimizer, "optimize_weights", lambda **_kwargs: OPTIMIZER_RESULT)

    def fail_persist(*_args, **_kwargs):
        raise OSError("proposal storage unavailable")

    monkeypatch.setattr(policy, "persist_optimizer_weights", fail_persist, raising=False)
    feedback = run_daily.self_closed_loop(
        "2026-10-15", str(tmp_path / "daily" / "2026-10-15"), object(),
    )

    assert "proposal storage unavailable" in feedback["error"]
    assert "已跳过" in feedback["note"]
    assert not (tmp_path / "weights.json").exists()


def test_daily_feedback_labels_frozen_optimizer_output_as_proposal(tmp_path, monkeypatch):
    import run_daily
    import weight_optimizer

    class FakeICTrack:
        @staticmethod
        def settle_mature_days(*_args, **_kwargs):
            return 0

        @staticmethod
        def compute_day_ic(*_args, **_kwargs):
            return {}

    authoritative = tmp_path / "weights.json"
    authoritative.write_bytes(b"authoritative sentinel")
    monkeypatch.setitem(sys.modules, "ic_track", FakeICTrack)
    monkeypatch.setattr(policy, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_daily, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(weight_optimizer, "WEIGHTS_FILE", str(authoritative))
    monkeypatch.setattr(weight_optimizer, "optimize_weights", lambda **_kwargs: OPTIMIZER_RESULT)
    monkeypatch.setenv("SELECTOR_WEIGHT_MODE", "frozen")

    feedback = run_daily.self_closed_loop(
        "2026-10-15", str(tmp_path / "daily" / "2026-10-15"), object(),
    )

    assert feedback["weights"] is None
    assert feedback["weight_proposal"]["status"] == "proposal_only"
    assert Path(feedback["weight_proposal"]["path"]).is_file()
    assert authoritative.read_bytes() == b"authoritative sentinel"

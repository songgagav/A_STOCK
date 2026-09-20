# -*- coding: utf-8 -*-
"""DRL 学习后检查（DRL-3）的回归（2026-09-20）.

**轻量**（不依赖 torch / gymnasium / h5i_db）⇒ 在 CI 的 regression-core job 也能跑。

锁定两类事情:
  ① 计算正确性 —— 滚动窗口收益/夏普/最大回撤、相邻日 top_n 的 Jaccard 重合度；
  ② METHOD-1 纪律 —— **只记录数值, 不产出任何布尔判定**（无 passed/ok_model/converged）,
     且"当前算不了"的两项（新旧模型对比 / 验证集）必须**显式落盘原因**,
     而不是留空 —— 否则复盘无法区分"检查通过"与"根本没有数据"(DRL-2 的教训)。

★ 另有一条**从真实数据里发现的缺陷**回归: 生产 `data/daily/` 下存在游离目录
  `day/`（`daily_summary.json` 的 `day` 字面量为 `--day`、equity=100000.0）,
  若 glob 不过滤会污染滚动窗口 —— 实测把 return 从 0.13% 抬到 0.83%、
  夏普从 0.53 抬到 2.69（约 5 倍高估）。见 TestStrayDirDefence。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import config  # noqa: E402
import drl_post as P  # noqa: E402  (轻量模块, 无 torch 依赖)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DRL_POST_LEDGER", raising=False)
    yield tmp_path


def _mk_summary(root, day_dir, equity, day_field=None):
    d = os.path.join(str(root), "daily", day_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "daily_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"day": day_field if day_field is not None else day_dir,
                   "steps": {}, "summary": {"equity": equity}}, f)


def _mk_plan(root, day_dir, canons):
    d = os.path.join(str(root), "drl", day_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "target_plan.json"), "w", encoding="utf-8") as f:
        json.dump({"day": day_dir,
                   "top_n": [{"canon": c, "target_weight": 0.1} for c in canons]}, f)


class TestStrayDirDefence:
    def test_non_date_dir_is_ignored(self, tmp_path):
        """★ 真实缺陷回归: `data/daily/day/`（day 字面量 `--day`）必须被过滤。"""
        _mk_summary(tmp_path, "20260908", 99306.11)
        _mk_summary(tmp_path, "day", 100000.0, day_field="--day")
        rows = P._daily_rows()
        assert [d for d, _e in rows] == ["20260908"], \
            "游离目录 day/ 不得进入权益序列"

    def test_stray_dir_does_not_change_metrics(self, tmp_path):
        """过滤与否会**显著改变**结论 —— 这就是该缺陷的危害。"""
        for d, e in (("20260824", 99174.45), ("20260825", 99587.18),
                     ("20260826", 99638.12)):
            _mk_summary(tmp_path, d, e)
        clean = P.rolling_window_metrics()
        _mk_summary(tmp_path, "day", 100000.0, day_field="--day")   # 注入游离目录
        dirty = P.rolling_window_metrics()
        assert clean == dirty, "注入游离目录后数值不得改变"
        assert clean["to"] == "20260826"
        assert isinstance(clean["return_pct"], float)


class TestRollingWindow:
    def test_known_series_exact(self, tmp_path):
        # 100 -> 110 -> 100 -> 110：日收益 +10%, -9.09%, +10%
        import math
        eqs = [100.0, 110.0, 100.0, 110.0]
        for i, e in enumerate(eqs):
            _mk_summary(tmp_path, f"2026090{i + 1}", e)
        r = P.rolling_window_metrics()
        assert r["n"] == 4
        assert r["return_pct"] == pytest.approx(10.0)
        assert r["max_dd_pct"] == pytest.approx((100.0 / 110.0 - 1) * 100, abs=1e-6)
        rets = [0.10, 100 / 110 - 1, 0.10]
        mu = sum(rets) / 3
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / 3)
        assert r["sharpe"] == pytest.approx(mu / sd * math.sqrt(252), abs=1e-4)

    def test_flat_equity_sharpe_is_none_not_zero(self, tmp_path):
        """无波动时夏普记 None 并给出 note —— 用 0 冒充会被误读为'表现中性'。"""
        for i in range(3):
            _mk_summary(tmp_path, f"2026090{i + 1}", 100000.0)
        r = P.rolling_window_metrics()
        assert r["sharpe"] is None
        assert r["max_dd_pct"] == 0.0
        assert any("无波动" in n for n in r["notes"])

    def test_single_day_is_insufficient(self, tmp_path):
        _mk_summary(tmp_path, "20260901", 100000.0)
        r = P.rolling_window_metrics()
        assert r["n"] == 1 and r["return_pct"] is None
        assert any("不足" in n for n in r["notes"])

    def test_missing_equity_skipped(self, tmp_path):
        _mk_summary(tmp_path, "20260901", 100000.0)
        _mk_summary(tmp_path, "20260902", None)
        _mk_summary(tmp_path, "20260903", 101000.0)
        r = P.rolling_window_metrics()
        assert r["n"] == 2, "equity 缺失的日子应跳过而不是当 0"

    def test_window_truncates_to_tail(self, tmp_path):
        for i in range(6):
            _mk_summary(tmp_path, f"2026090{i + 1}", 100.0 + i)
        r = P.rolling_window_metrics(days=3)
        assert r["n"] == 3
        assert r["from"] == "20260904" and r["to"] == "20260906"


class TestDecisionConsistency:
    def test_identical_plans(self, tmp_path):
        _mk_plan(tmp_path, "20260901", ["A", "B"])
        _mk_plan(tmp_path, "20260902", ["A", "B"])
        c = P.decision_consistency()
        assert c["pairs"] == 1 and c["identical_pairs"] == 1
        assert c["jaccard_mean"] == pytest.approx(1.0)

    def test_disjoint_plans(self, tmp_path):
        _mk_plan(tmp_path, "20260901", ["A", "B"])
        _mk_plan(tmp_path, "20260902", ["C", "D"])
        c = P.decision_consistency()
        assert c["jaccard_mean"] == pytest.approx(0.0)
        assert c["identical_pairs"] == 0

    def test_partial_overlap_exact(self, tmp_path):
        # {A,B,C} vs {B,C,D}: 交 2 / 并 4 = 0.5
        _mk_plan(tmp_path, "20260901", ["A", "B", "C"])
        _mk_plan(tmp_path, "20260902", ["B", "C", "D"])
        c = P.decision_consistency()
        assert c["jaccard_mean"] == pytest.approx(0.5)

    def test_single_plan_insufficient(self, tmp_path):
        _mk_plan(tmp_path, "20260901", ["A"])
        c = P.decision_consistency()
        assert c["pairs"] == 0 and c["jaccard_mean"] is None
        assert any("不足" in n for n in c["notes"])

    def test_stray_drl_dir_ignored(self, tmp_path):
        _mk_plan(tmp_path, "day", ["X", "Y"])       # 非 8 位目录
        _mk_plan(tmp_path, "20260901", ["A"])
        c = P.decision_consistency()
        assert c["n_plans"] == 1, "非日期目录不得计入"


class TestMethod1Discipline:
    def test_threshold_not_applied(self, tmp_path):
        _mk_summary(tmp_path, "20260901", 100.0)
        rec = P.summarize("2026-09-20")
        assert rec["threshold_applied"] is False
        assert "METHOD-1" in rec["note"]

    def test_no_boolean_verdict_anywhere(self, tmp_path):
        _mk_summary(tmp_path, "20260901", 100.0)
        _mk_summary(tmp_path, "20260902", 101.0)
        _mk_plan(tmp_path, "20260901", ["A"])
        _mk_plan(tmp_path, "20260902", ["A"])
        rec = P.summarize("2026-09-20")
        txt = json.dumps(rec, ensure_ascii=False)
        for bad in ("passed", "ok_model", "converged", "should_deploy",
                    "consistent\": true", "degraded\": true"):
            assert bad not in txt, f"不得出现判定字段 {bad}"

    def test_unavailable_items_carry_reasons(self, tmp_path):
        rec = P.summarize("2026-09-20")
        un = rec["unavailable"]
        assert un["new_vs_old"]["available"] is False
        assert len(un["new_vs_old"]["reason"]) > 20
        # 切分口径已定义 -> 不再属于"不可用", 而是随记录一起落盘的口径说明
        assert "validation_set" not in un, "验证集切分口径已定义, 不应再标为不可用"
        assert rec["validation_split"]["method"] == "temporal_holdout_tail"
        assert "噪声" in rec["validation_split"]["limitation"]

    def test_params_drift_points_to_drl7(self, tmp_path):
        rec = P.summarize("2026-09-20")
        assert "drl_weight_drift" in rec["params_drift"]["see"]


class TestValidationSplit:
    def test_scored_range_matches_env_steps(self):
        """评分区间必须与 FactorWeightEnv 可评步一致: t ∈ [lookback, n-1]。"""
        sp = P.split_validation(45, lookback=10, val_days=5)
        assert sp["scored"] == list(range(10, 45))
        assert sp["val"] == [40, 41, 42, 43, 44]
        assert sp["train"] == list(range(10, 40))

    def test_no_shuffle_keeps_temporal_order(self):
        sp = P.split_validation(30, lookback=5, val_days=3)
        assert sp["train"] == sorted(sp["train"])
        assert sp["val"] == sorted(sp["val"])
        assert max(sp["train"]) < min(sp["val"]), "训练段必须全部早于验证段"

    def test_insufficient_data_leaves_val_empty_with_note(self):
        sp = P.split_validation(12, lookback=10, val_days=5)
        assert sp["val"] == []
        assert any("不足" in n for n in sp["notes"])

    def test_zero_val_days(self):
        sp = P.split_validation(45, lookback=10, val_days=0)
        assert sp["val"] == []


class TestScoreWeights:
    def test_base_weights_score_exactly_zero(self):
        """自洽性检查: 奖励式是 (w·ic − base·ic)·…, 故 w==base 时恒为 0。"""
        ic = np.random.RandomState(1).randn(30, 6) * 0.05
        base = np.ones(6) / 6
        s = P.score_weights(ic, base, base, [12, 15, 20])
        assert s["n"] == 3 and s["mean"] == 0.0 and s["std"] == 0.0

    def test_matches_env_formula_on_one_day(self):
        """逐字复核 env 的式子: (w·ic[t] − base·ic[t])·10/(1+ic_vol·10)。"""
        ic = np.random.RandomState(2).randn(40, 6) * 0.05
        base, w = np.ones(6) / 6, np.array([0.5, 0.1, 0.1, 0.1, 0.1, 0.1])
        t = 25
        win = ic[max(0, t - 20):t]
        vol = max(0.001, float(np.std(win)))
        expect = (float(np.dot(w, ic[t])) - float(np.dot(base, ic[t]))) * 10.0 / (1 + vol * 10)
        assert P.score_weights(ic, base, w, [t])["mean"] == pytest.approx(expect, abs=1e-9)

    def test_empty_index_gives_none(self):
        ic = np.zeros((5, 6))
        s = P.score_weights(ic, np.ones(6) / 6, np.ones(6) / 6, [])
        assert s["n"] == 0 and s["mean"] is None

    def test_out_of_range_index_skipped(self):
        ic = np.zeros((5, 6))
        assert P.score_weights(ic, np.ones(6) / 6, np.ones(6) / 6, [99, -1])["n"] == 0


class TestValidationCompare:
    def _ic(self, n=45, seed=0):
        return np.random.RandomState(seed).randn(n, 6) * 0.05

    def test_delta_is_new_minus_old(self):
        ic = self._ic()
        base = np.ones(6) / 6
        new, old = np.array([0.5, .1, .1, .1, .1, .1]), np.array([.1, .5, .1, .1, .1, .1])
        v = P.validation_compare(ic, base, new, old, lookback=10, val_days=5)
        assert v["delta_new_minus_old"] == pytest.approx(v["new"]["mean"] - v["old"]["mean"],
                                                        abs=1e-9)
        assert v["n_train"] == 30 and v["val_days"] == 5
        assert v["val_index"] == (40, 44)

    def test_no_old_weights_yields_none_delta_with_note(self):
        v = P.validation_compare(self._ic(), np.ones(6) / 6, np.ones(6) / 6, None)
        assert v["old"] is None and v["delta_new_minus_old"] is None
        assert any("未提供上一版" in n for n in v["notes"])

    def test_split_spec_is_recorded(self):
        v = P.validation_compare(self._ic(), np.ones(6) / 6, np.ones(6) / 6)
        assert v["split"]["method"] == "temporal_holdout_tail"
        assert "shuffle" in v["split"]["desc"]
        assert "噪声" in v["split"]["limitation"]

    def test_no_boolean_verdict(self):
        v = P.validation_compare(self._ic(), np.ones(6) / 6,
                                 np.array([.5, .1, .1, .1, .1, .1]))
        txt = json.dumps(v, ensure_ascii=False)
        for bad in ("passed", "should_deploy", "ok_model", "\"better\""):
            assert bad not in txt
        assert v["threshold_applied"] is False

    def test_insufficient_data_is_explicit(self):
        v = P.validation_compare(np.zeros((12, 6)), np.ones(6) / 6, np.ones(6) / 6)
        assert v["new"]["n"] == 0
        assert any("验证段为空" in n for n in v["notes"])


class TestValidationFromTrainMeta:
    def test_reads_post_train_validation(self, tmp_path):
        import config as _c
        d = os.path.join(str(tmp_path), "drl", "20260905")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"day": "2026-09-05",
                       "post_train_validation": {"val_days": 5, "delta_new_minus_old": 0.1}}, f)
        v = P.validation_from_train_meta("2026-09-05")
        assert v == {"val_days": 5, "delta_new_minus_old": 0.1}

    def test_missing_or_bad_day_returns_none(self, tmp_path):
        assert P.validation_from_train_meta("2026-09-06") is None
        assert P.validation_from_train_meta(None) is None
        assert P.validation_from_train_meta("day") is None

    def test_record_picks_it_up_automatically(self, tmp_path):
        d = os.path.join(str(tmp_path), "drl", "20260905")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "train_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"post_train_validation": {"val_days": 5}}, f)
        rec = P.record_post_metrics("2026-09-05")
        assert rec["new_vs_old"] == {"val_days": 5}
        assert "new_vs_old" not in rec["unavailable"], "已取到就不该再标为不可用"

    def test_record_without_validation_marks_unavailable(self, tmp_path):
        rec = P.record_post_metrics("2026-09-05")
        assert rec["new_vs_old"] is None
        assert rec["unavailable"]["new_vs_old"]["available"] is False
        assert rec["validation_split"]["method"] == "temporal_holdout_tail"


class TestLedger:
    def test_appends_one_json_line(self, tmp_path):
        _mk_summary(tmp_path, "20260901", 100.0)
        P.record_post_metrics("2026-09-20")
        P.record_post_metrics("2026-09-21")
        p = P.ledger_path()
        with open(p, encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        assert len(lines) == 2
        for ln in lines:
            json.loads(ln)          # 每行都是合法 JSON

    def test_env_override(self, tmp_path, monkeypatch):
        custom = str(tmp_path / "elsewhere" / "post.jsonl")
        monkeypatch.setenv("DRL_POST_LEDGER", custom)
        P.record_post_metrics("2026-09-20")
        assert os.path.isfile(custom)

    def test_never_raises_on_unwritable(self, tmp_path, monkeypatch):
        blocker = tmp_path / "b"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("DRL_POST_LEDGER", str(blocker / "s" / "p.jsonl"))
        rec = P.record_post_metrics("2026-09-20")     # 不得抛
        assert rec["threshold_applied"] is False

    def test_json_safe_with_empty_data(self, tmp_path):
        rec = P.record_post_metrics("2026-09-20")
        json.dumps(rec)          # 无数据时也必须可序列化(无 NaN)
        assert rec["rolling_window"]["n"] == 0

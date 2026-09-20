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
        assert un["validation_set"]["available"] is False
        assert "验证集" in un["validation_set"]["reason"]

    def test_params_drift_points_to_drl7(self, tmp_path):
        rec = P.summarize("2026-09-20")
        assert "drl_weight_drift" in rec["params_drift"]["see"]


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

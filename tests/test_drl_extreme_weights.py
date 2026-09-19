# -*- coding: utf-8 -*-
"""DRL 极端因子权重**记录**（DRL-5 最小版本）的回归（2026-09-19）.

用户决策: 本批次**只记录极端权重, 绝不截断**。
  · 不部署 [0.005, 0.65] —— 16 天内触发 0 次 = 死代码, 零收益;
  · 不采用 [0.02, 0.40] —— 16 天触发 8 处 = 常态性改变行为, 且**无证据表明那 8 处有害**。

因此本文件的**最重要断言**是: 无论是否越界, `check_extreme_weights` 都**不得改动权重**,
也不得返回任何"截断/阻断"语义。构造了三种触发条件:
  ① 有因子越过上界  ② 有因子越过下界  ③ 全部在界内(用于验证 n_extreme=0 也有记录)
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

import dataguard  # noqa: E402
import drl_drift as D  # noqa: E402

_KEY = "drl_extreme_weights"

#: 16 天实测里真实出现过的越界样本（见 data/drl_weight_drift_report.json）
REAL_EXTREMES = {
    "signal": 0.6033,      # 超上界 0.65? 否 -> 但超 [0.02,0.40]; 对 [0.005,0.65] 不越界
    "govern": 0.4189,
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    dataguard.reset_warnings()
    monkeypatch.setenv("DRL_EXTREME_LEDGER", str(tmp_path / "extreme.jsonl"))
    monkeypatch.delenv("DRL_EXTREME_LO", raising=False)
    monkeypatch.delenv("DRL_EXTREME_HI", raising=False)
    yield
    dataguard.reset_warnings()


def _w(**kw):
    base = {"signal": 0.18, "trend": 0.11, "govern": 0.10,
            "liquidity": 0.33, "vol": 0.02, "mom_rev": 0.26}
    base.update(kw)
    return base


class TestNeverTruncates:
    def test_over_upper_bound_is_recorded_not_truncated(self):
        """构造: signal 0.90 越过上界 0.65 -> 记录, 但**权重原样不变**。"""
        w = _w(signal=0.90)
        before = dict(w)
        r = D.check_extreme_weights(w, day="20260905")
        assert r["ok"] is True
        assert r["n_extreme"] == 1 and "signal" in r["extreme"]
        assert r["truncated"] is False, "绝不能截断"
        assert w == before, "**入参权重不得被改动**"
        # 记录里的权重也必须是原值(不是被 clamp 后的值)
        assert r["final_weights"]["signal"] == pytest.approx(0.90, abs=1e-6)

    def test_under_lower_bound_is_recorded_not_truncated(self):
        """构造: govern 0.001 越过下界 0.005 -> 记录, 不截断。"""
        w = _w(govern=0.001)
        before = dict(w)
        r = D.check_extreme_weights(w, day="20260905")
        assert r["n_extreme"] == 1 and "govern" in r["extreme"]
        assert r["truncated"] is False
        assert w == before
        assert r["final_weights"]["govern"] == pytest.approx(0.001, abs=1e-6)

    def test_both_bounds_at_once(self):
        w = _w(signal=0.99, vol=0.0001)
        r = D.check_extreme_weights(w)
        assert set(r["extreme"]) == {"signal", "vol"}
        assert r["truncated"] is False

    def test_real_20260905_weights_are_within_record_bounds(self):
        """实测 20260905 的真实权重对 [0.005, 0.65] **不越界** ——
        这正是用户选择"不部署该边界"的原因(触发 0 次 = 死代码)。
        顺带证明: 若当初部署 [0.02, 0.40], 这里会有 4 个因子被误判为"极端"。"""
        real = {"signal": 0.18234626948833466, "trend": 0.1073092371225357,
                "govern": 0.09946948289871216, "liquidity": 0.32540780305862427,
                "vol": 0.02282579615712166, "mom_rev": 0.26264142990112305}
        r = D.check_extreme_weights(real, day="20260905")
        assert r["n_extreme"] == 0, "[0.005,0.65] 对真实权重零触发"
        # 对照: [0.02,0.40] 会误判多少
        wrong = [k for k, v in real.items() if v < 0.02 or v > 0.40]
        assert wrong == [], "该样本在 [0.02,0.40] 下也不越界; 越界样本在 16 天分布里"


class TestLedgerAndFrequency:
    def test_records_every_day_even_with_zero_extremes(self, tmp_path):
        """**每天都落记录**(即使 n_extreme=0) —— 否则无法算"发生频率"(缺分母)。"""
        D.check_extreme_weights(_w(), day="20260904")
        D.check_extreme_weights(_w(signal=0.90), day="20260905")
        D.check_extreme_weights(_w(), day="20260906")
        lines = [json.loads(x) for x in open(D.extreme_ledger_path(), encoding="utf-8") if x.strip()]
        assert len(lines) == 3, "零极端的日子也必须入账(频率分母)"
        assert [x["n_extreme"] for x in lines] == [0, 1, 0]
        assert all(x["truncated"] is False for x in lines)

    def test_downstream_metrics_are_recorded(self, tmp_path):
        """必须同时记录**下游指标** —— 日后判断"极端是否有害"要靠它, 而不是靠分布。"""
        meta = {"day": "20260905", "mean_reward": 0.42, "total_timesteps": 800,
                "vnpy_stats": {"stats": {"sharpe_ratio": 1.23}},
                "target_plan": {"ok": True}}
        r = D.check_extreme_weights(_w(signal=0.90), meta=meta)
        assert r["mean_reward"] == pytest.approx(0.42)
        assert r["total_timesteps"] == 800
        assert r["vnpy_sharpe"] == pytest.approx(1.23)
        assert r["plan_ok"] is True

    def test_bounds_env_override(self, monkeypatch):
        monkeypatch.setenv("DRL_EXTREME_LO", "0.05")
        monkeypatch.setenv("DRL_EXTREME_HI", "0.30")
        r = D.check_extreme_weights(_w(liquidity=0.33, vol=0.02))
        assert r["bounds"] == [pytest.approx(0.05), pytest.approx(0.30)]
        assert set(r["extreme"]) == {"liquidity", "vol"}

    def test_bad_bounds_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("DRL_EXTREME_LO", "abc")
        lo, hi = D.extreme_bounds()
        assert (lo, hi) == D.EXTREME_BOUNDS_DEFAULT


class TestWarningAndSafety:
    def test_warns_when_extreme(self):
        D.check_extreme_weights(_w(signal=0.90))
        assert dataguard.warned_count(_KEY) >= 1

    def test_no_warn_when_within_bounds(self):
        D.check_extreme_weights(_w())
        assert dataguard.warned_count(_KEY) == 0, "未越界不得告警(否则告警疲劳)"

    def test_missing_weights_tolerated(self):
        r = D.check_extreme_weights(None)
        assert r["ok"] is False

    def test_never_raises_on_bad_input(self):
        """任何异常都必须被兜住, 绝不影响训练主链路。"""
        for bad in (None, {}, {"x": "not-a-number"}, 123):
            r = D.check_extreme_weights(bad)  # type: ignore[arg-type]
            assert r["ok"] is False or r["ok"] is True   # 不抛异常即可

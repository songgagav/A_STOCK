# -*- coding: utf-8 -*-
"""DRL 权重漂移检查的回归（2026-09-19）.

被测: `drl_train._check_weight_drift` —— 用户要求「**只告警, 不阻断**」, 阈值先设 0.3。

必须**构造触发条件**验证(不能只看代码跑通):
  · 漂移 > 阈值  -> `warn_once('drl_weight_drift')` 计数 +1, 且 action='warn_only'
  · 漂移 <= 阈值 -> **不告警**, action='ok'
  · 无论哪种, 都**不得抛异常**、不得改动 final_weights、不得阻断(返回值里没有"阻断"语义)
  · 每条都追加到 data/drl_weight_drift.jsonl(供 1-2 周后标定阈值)
  · 日间漂移: 与**上一个更早交易日**的 final_weights 比; 无更早数据时为 None(而非 0/报错)

注意: `dataguard` 与账本路径都指向生产 data/。用例通过 monkeypatch 把账本指到 tmp_path,
并只读生产目录(不写), 以免测试污染真实记账。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, _SRC)

import dataguard  # noqa: E402
import drl_drift as D  # noqa: E402  (轻量模块, 无 torch 依赖 -> core CI 也能跑)

_KEY = "drl_weight_drift"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例: 清告警计数 + 把漂移账本指到临时文件(不污染生产记账)。"""
    dataguard.reset_warnings()
    monkeypatch.setenv("DRL_DRIFT_LEDGER", str(tmp_path / "drift.jsonl"))
    monkeypatch.delenv("DRL_DRIFT_THRESHOLD", raising=False)
    yield
    dataguard.reset_warnings()


def _meta(base, final, day="20260905", **kw):
    m = {"day": day, "base_weights": base, "final_weights": final}
    m.update(kw)
    return m


def _uniform(v=1 / 6):
    return {k: v for k in D.DRIFT_FACTORS}


class TestThresholdTrigger:
    def test_over_threshold_warns(self):
        """构造: 某因子从 1/6 被推到 0.55 -> 差值 0.383 > 0.3 -> 必须告警。"""
        final = _uniform()
        final["liquidity"] = 0.55
        r = D.check_weight_drift(_meta(_uniform(), final))
        assert r["ok"] is True
        assert r["drift_base_to_final"] == pytest.approx(0.55 - 1 / 6, abs=1e-6)
        assert r["over_threshold"] is True
        assert r["action"] == "warn_only"
        assert dataguard.warned_count(_KEY) >= 1, "超阈值必须告警"

    def test_under_threshold_silent(self):
        """构造: 实测量级的漂移(vol 0.0228) -> 差值 0.144 < 0.3 -> **不得告警**。"""
        final = _uniform()
        final["vol"] = 0.0228
        final["liquidity"] = 0.3254
        r = D.check_weight_drift(_meta(_uniform(), final))
        assert r["over_threshold"] is False
        assert r["action"] == "ok"
        assert dataguard.warned_count(_KEY) == 0, "未超阈值不得告警(否则告警疲劳)"

    def test_actual_observed_weights_are_within_threshold(self):
        """用 train_meta 20260905 的**真实** final_weights 验证当前模型不会被误报。"""
        real = {"signal": 0.18234626948833466, "trend": 0.1073092371225357,
                "govern": 0.09946948289871216, "liquidity": 0.32540780305862427,
                "vol": 0.02282579615712166, "mom_rev": 0.26264142990112305}
        r = D.check_weight_drift(_meta(_uniform(), real))
        assert r["over_threshold"] is False, "真实权重不应触发告警"
        assert r["drift_base_to_final"] < 0.3

    def test_threshold_env_override(self, monkeypatch):
        """阈值必须可被环境变量覆盖(便于标定后调整, 无需改代码)。"""
        monkeypatch.setenv("DRL_DRIFT_THRESHOLD", "0.01")
        final = _uniform()
        final["vol"] = 0.0228
        r = D.check_weight_drift(_meta(_uniform(), final))
        assert r["threshold"] == pytest.approx(0.01)
        assert r["over_threshold"] is True
        assert dataguard.warned_count(_KEY) >= 1

    def test_warn_once_dedupes_output_but_counts_every_call(self, capsys):
        """`warn_once` 的真实语义(**实测其 docstring**): **只有打印去重, 计数不去重**。

            "同一 key 只告警一次(避免逐标的刷屏), 但**每次**都会记入计数便于审计"
            _WARNED[key] += 1
            if _WARNED[key] == 1: print(msg, file=sys.stderr)

        故两条都要断言:
          · 计数 == 调用次数(审计口径, 说明"发生过几次")
          · **stderr 里消息只出现一次**(去重口径, 说明"没有刷屏")
        我初版把"去重"误解成"计数也为 1", 是错的 —— 保留此用例以免再犯。
        """
        final = _uniform()
        final["liquidity"] = 0.55
        for _ in range(3):
            D.check_weight_drift(_meta(_uniform(), final))
        assert dataguard.warned_count(_KEY) == 3, "计数按调用次数累加(审计口径)"
        err = capsys.readouterr().err
        assert err.count("DRL 权重漂移") == 1, "打印必须去重, 否则会刷屏"


class TestNonBlocking:
    def test_never_raises_and_never_blocks(self):
        """**只告警不阻断** 的硬性体现: 超阈值时也不抛异常、不改 final_weights。"""
        final = _uniform()
        final["liquidity"] = 0.99
        before = dict(final)
        r = D.check_weight_drift(_meta(_uniform(), final))
        assert r["ok"] is True                 # 未抛异常
        assert final == before                 # 未改动权重
        assert r["action"] == "warn_only"      # 动作为"仅告警"
        assert "block" not in json.dumps(r).lower()

    def test_missing_final_weights_is_tolerated(self):
        r = D.check_weight_drift({"day": "20260905"})
        assert r["ok"] is False and "final_weights" in r["error"]

    def test_bad_threshold_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("DRL_DRIFT_THRESHOLD", "not-a-number")
        r = D.check_weight_drift(_meta(_uniform(), _uniform()))
        assert r["ok"] is True
        assert r["threshold"] == D.DRIFT_THRESHOLD_DEFAULT


class TestLedgerAndDayOverDay:
    def test_ledger_appended(self, tmp_path):
        """每条记录必须追加到账本 —— 这是"积累数据以便标定阈值"的载体。"""
        final = _uniform()
        final["vol"] = 0.0228
        D.check_weight_drift(_meta(_uniform(), final))
        D.check_weight_drift(_meta(_uniform(), final, day="20260906"))
        lines = [json.loads(x) for x in open(D.ledger_path(), encoding="utf-8") if x.strip()]
        assert len(lines) == 2
        assert lines[0]["threshold_is_calibrated"] is False, "必须标注阈值未标定"
        assert lines[0]["final_weights"]["vol"] == pytest.approx(0.0228, abs=1e-6)

    def test_day_over_day_diff_present_when_prev_exists(self, tmp_path, monkeypatch):
        """构造"上一交易日": 日间漂移必须被算出来。"""
        prev_dir = tmp_path / "data" / "drl" / "20260904"
        prev_dir.mkdir(parents=True)
        prev_final = _uniform()
        prev_final["vol"] = 0.30
        (prev_dir / "train_meta.json").write_text(
            json.dumps({"day": "20260904", "final_weights": prev_final}),
            encoding="utf-8")
        monkeypatch.setattr(D.config, "DATA_DIR", str(tmp_path / "data"), raising=False)

        final = _uniform()
        final["vol"] = 0.0228
        r = D.check_weight_drift(_meta(_uniform(), final, day="20260905"))
        assert r["prev_day"] == "20260904"
        assert r["drift_day_over_day"] == pytest.approx(0.30 - 0.0228, abs=1e-6)

    def test_day_over_day_none_when_no_prev(self, tmp_path, monkeypatch):
        """无更早数据时必须为 None(而非 0, 也非报错) —— 0 会被误读成"没有漂移"。"""
        monkeypatch.setattr(D.config, "DATA_DIR", str(tmp_path / "data"), raising=False)
        r = D.check_weight_drift(_meta(_uniform(), _uniform(), day="20260905"))
        assert r["drift_day_over_day"] is None
        assert r["prev_day"] is None

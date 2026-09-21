# -*- coding: utf-8 -*-
"""DRL 学习前检查的回归测试（DRL-1）.

重点: 阈值**沿用仓库既有 15 日判据**(非新造); 净值检查只做**无争议的结构校验**;
拿不到净值数据时**如实记为未判定**, 不假装通过也不假装失败。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

from drl_precheck import (  # noqa: E402
    MIN_TRAIN_DAYS, check_min_samples, check_net_value_continuity, evaluate,
    load_net_values, record,
)


class TestMinSamples:
    def test_threshold_is_the_repo_existing_15(self):
        """阈值不是我定的: 仓库既有 `len(rets) < 15`(drl_train.py:1034 与 :517)。"""
        assert MIN_TRAIN_DAYS == 15

    def test_below_floor_is_critical(self):
        it = check_min_samples(14)
        assert it and it["severity"] == "CRITICAL" and "14" in it["detail"]

    def test_at_floor_passes(self):
        assert check_min_samples(15) is None

    def test_real_run_value_passes(self):
        """实测一次真实训练 n_dates=39, 必须通过。"""
        assert check_min_samples(39) is None

    def test_unparsable_is_critical_not_silent(self):
        assert check_min_samples("abc")["code"] == "samples_unknown"
        assert check_min_samples(None)["code"] == "samples_unknown"


class TestNetValueContinuity:
    def test_healthy_series_is_clean(self):
        assert check_net_value_continuity([1.0, 1.02, 0.99, 1.05]) == []

    def test_nan_flagged(self):
        assert any(i["code"] == "net_nan_inf"
                   for i in check_net_value_continuity([1.0, float("nan")]))

    def test_inf_flagged(self):
        assert any(i["code"] == "net_nan_inf"
                   for i in check_net_value_continuity([1.0, float("inf")]))

    def test_non_positive_flagged(self):
        """净值 ≤0 是不可能的形态 —— 一定意味着上游算错或数据坏了。"""
        assert any(i["code"] == "net_non_positive"
                   for i in check_net_value_continuity([1.0, 0.0]))

    def test_empty_flagged(self):
        assert check_net_value_continuity([])[0]["code"] == "net_empty"

    def test_non_numeric_flagged(self):
        assert any(i["code"] == "net_non_numeric"
                   for i in check_net_value_continuity([1.0, "x"]))

    def test_length_mismatch_flagged(self):
        assert any(i["code"] == "net_len_mismatch"
                   for i in check_net_value_continuity([1.0, 1.1], ["d1"]))

    def test_duplicate_dates_flagged(self):
        assert any(i["code"] == "net_dup_dates"
                   for i in check_net_value_continuity([1.0, 1.1], ["d1", "d1"]))

    def test_unsorted_dates_flagged(self):
        assert any(i["code"] == "net_unsorted_dates"
                   for i in check_net_value_continuity([1.0, 1.1], ["d2", "d1"]))

    def test_none_means_not_judged_not_passed(self):
        """`None` 不是"通过" —— 它什么都不报, 由 evaluate 记入 not_judged。"""
        assert check_net_value_continuity(None) == []

    def test_does_not_judge_returns_goodness(self):
        """**刻意不做**的事: 不报"某天涨跌多少" —— 那需要阈值, 而阈值须由下游表现定。"""
        assert check_net_value_continuity([1.0, 3.0]) == []      # +200% 也不报
        assert check_net_value_continuity([1.0, 0.01]) == []     # -99% 也不报


class TestEvaluate:
    def test_ok_when_samples_sufficient_and_net_absent(self):
        r = evaluate(n_dates=39, net_values=None)
        assert r["ok"] is True and r["net_checked"] is False
        assert any("净值连续性" == n["what"] for n in r["not_judged"])

    def test_fails_when_samples_too_few(self):
        r = evaluate(n_dates=3)
        assert r["ok"] is False and r["issues"][0]["code"] == "samples_too_few"

    def test_net_checked_flag_reflects_reality(self):
        assert evaluate(n_dates=39, net_values=[1.0, 1.1])["net_checked"] is True

    def test_bad_net_values_fail_even_with_enough_samples(self):
        r = evaluate(n_dates=39, net_values=[1.0, 0.0])
        assert r["ok"] is False and any(i["code"] == "net_non_positive" for i in r["issues"])

    def test_verdict_has_timestamp_and_thresholds(self):
        r = evaluate(n_dates=39)
        assert r["min_days"] == MIN_TRAIN_DAYS and r["checked_at"]


class TestLoadAndRecord:
    def test_load_from_net_values_json(self, tmp_path):
        (tmp_path / "net_values.json").write_text(json.dumps({"net_values": [1.0, 1.1]}),
                                                  encoding="utf-8")
        assert load_net_values(str(tmp_path)) == [1.0, 1.1]

    def test_load_from_train_meta_vnpy_stats(self, tmp_path):
        (tmp_path / "train_meta.json").write_text(
            json.dumps({"vnpy_stats": {"daily_returns": [0.01, -0.02]}}), encoding="utf-8")
        assert load_net_values(str(tmp_path)) == [0.01, -0.02]

    def test_returns_none_when_absent(self, tmp_path):
        """实测 vnpy_stats 是空字典 => 这里必须返回 None(而不是编一个空序列)。"""
        assert load_net_values(str(tmp_path)) is None

    def test_empty_dict_is_none_not_empty_list(self, tmp_path):
        (tmp_path / "train_meta.json").write_text(json.dumps({"vnpy_stats": {}}), encoding="utf-8")
        assert load_net_values(str(tmp_path)) is None

    def test_record_writes_atomically(self, tmp_path):
        fp = record(str(tmp_path), {"ok": True})
        assert fp and json.load(open(fp, encoding="utf-8"))["ok"] is True
        assert not os.path.exists(fp + ".tmp")

    def test_record_creates_directory(self, tmp_path):
        """目录不存在是**正常**场景(当天目录可能还没建) —— 应当主动创建并写入。"""
        fp = record(str(tmp_path / "no" / "such"), {"ok": True})
        assert fp and os.path.isfile(fp)

    def test_record_failure_does_not_raise(self, tmp_path):
        """真失败路径: 目标位置被一个**文件**占住, makedirs 必失败 => 返回 None 且不抛。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        assert record(str(blocker / "sub"), {"ok": True}) is None

    def test_real_day_dir_yields_39_dates_and_no_net(self):
        """对真实产物跑一遍: n_dates 从 train_meta 读得到, 净值取不到(如实记为未判定)。"""
        d = os.path.join(_REPO, "data", "drl", "20181019")
        if not os.path.isdir(d):
            pytest.skip("无该样例目录")
        meta = json.load(open(os.path.join(d, "train_meta.json"), encoding="utf-8-sig"))
        r = evaluate(n_dates=meta.get("n_dates"), net_values=load_net_values(d))
        assert r["ok"] is True
        assert r["n_dates"] == 39 and r["net_checked"] is False

# -*- coding: utf-8 -*-
"""DRL 学习中断指标（DRL-2）的回归（2026-09-20）.

被测: `src/drl_metrics.py`（采集 + 汇总）+ `drl_train.py` 的接线。
**轻量**: 只 import `drl_metrics`, 不 import `drl_train`
（后者拖入 torch, 会打挂 CI core job）。对 `drl_train` 的接线用**源码级断言**验证。

DRL-2 的现状是「学习中指标**结构性不可验证**」—— 实测 16 份 `train_meta.json` 共 23 个键,
其中没有 entropy / policy_loss / value_loss / grad_norm / 实际步数 / 时长。
所以这里必须**构造数据**验证"真的能落盘", 而不是只看代码存在:

  · 采集: 逐迭代累积; 缺失/NaN/Inf 键**跳过并计数**, 不写非法值
  · 汇总: `{n, first, last, min, max, mean, delta, rel_delta, slope}` 数值正确
  · ★ **不判定收敛**: 输出里**不得有任何布尔判定**, 且 `threshold_applied is False`
    （METHOD-1: 阈值须基于下游表现而非分布范围; 本批次只记录）
  · 实际步数与配置步数**分开记录**（原先混为一谈, 是"步数无从验证"的根源之一）
  · JSON 安全: 汇总与序列里**不得出现 NaN / Inf**（`json.dump` 默认会写出非法 JSON）
"""
from __future__ import annotations

import json
import math
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
sys.path.insert(0, _SRC)

import drl_metrics as M  # noqa: E402  (轻量模块, 无 torch 依赖 -> core CI 也能跑)


class _FakeModel:
    def __init__(self, n):
        self.num_timesteps = n


def _assert_json_safe(obj, path="root"):
    """递归断言无 NaN/Inf（非法 JSON）—— 落盘前必须过这一关。"""
    if isinstance(obj, float):
        assert math.isfinite(obj), f"{path} 出现非有限值 {obj}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _assert_json_safe(v, f"{path}[{i}]")


# =====================================================================
# 采集
# =====================================================================

class TestMetricsHistory:
    def test_records_each_metric_in_order(self):
        h = M.MetricsHistory()
        h.record({"entropy_loss": 1.0, "value_loss": 10.0})
        h.record({"entropy_loss": 0.5, "value_loss": 8.0})
        assert h.history["entropy_loss"] == [1.0, 0.5]
        assert h.history["value_loss"] == [10.0, 8.0]
        assert h.n_records == 2

    def test_missing_keys_are_skipped_and_counted(self):
        """缺失键**不写占位值**（否则序列会掺入假 0 而污染 slope/mean）。"""
        h = M.MetricsHistory()
        h.record({"entropy_loss": 1.0})
        h.record({"entropy_loss": 2.0})
        assert h.history["entropy_loss"] == [1.0, 2.0]
        assert h.history["value_loss"] == []
        assert h.skipped["value_loss"] == 2
        assert "entropy_loss" not in h.skipped

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None,
                                     "abc", [1], {}])
    def test_non_finite_and_non_numeric_are_skipped(self, bad):
        h = M.MetricsHistory()
        h.record({"entropy_loss": bad})
        assert h.history["entropy_loss"] == []
        assert h.skipped["entropy_loss"] == 1

    def test_bool_is_not_treated_as_number(self):
        """`True` 是 int 的子类, 若不当心会变成 1.0 混进损失序列。"""
        assert M._finite(True) is None
        assert M._finite(False) is None
        assert M._finite(0) == 0.0

    def test_record_tolerates_none_and_empty(self):
        h = M.MetricsHistory()
        h.record(None)
        h.record({})
        assert h.n_records == 2
        assert h.series_for_meta() == {}

    def test_history_property_returns_copy(self):
        h = M.MetricsHistory()
        h.record({"entropy_loss": 1.0})
        snap = h.history
        snap["entropy_loss"].append(999.0)
        assert h.history["entropy_loss"] == [1.0], "外部改动不得污染内部序列"

    def test_numpy_scalars_are_accepted(self):
        np = pytest.importorskip("numpy")
        h = M.MetricsHistory()
        h.record({"approx_kl": np.float32(0.25), "clip_fraction": np.float64(0.5)})
        assert h.history["approx_kl"] == [pytest.approx(0.25, abs=1e-6)]
        assert h.history["clip_fraction"] == [0.5]


# =====================================================================
# 汇总
# =====================================================================

class TestSummarize:
    def test_statistics_are_correct(self):
        s = M.summarize({"value_loss": [4.0, 2.0, 6.0, 8.0]})
        d = s["value_loss"]
        assert d["n"] == 4
        assert d["first"] == 4.0 and d["last"] == 8.0
        assert d["min"] == 2.0 and d["max"] == 8.0
        assert d["mean"] == 5.0
        assert d["delta"] == 4.0

    def test_slope_of_perfect_line_is_exact(self):
        assert M.summarize({"x": [1.0, 2.0, 3.0, 4.0]})["x"]["slope"] == pytest.approx(1.0)
        assert M.summarize({"x": [4.0, 3.0, 2.0, 1.0]})["x"]["slope"] == pytest.approx(-1.0)
        assert M.summarize({"x": [5.0, 5.0, 5.0]})["x"]["slope"] == pytest.approx(0.0)

    def test_slope_none_below_two_points(self):
        assert M.summarize({"x": [3.0]})["x"]["slope"] is None
        assert M._slope([]) is None

    def test_rel_delta_and_zero_denominator(self):
        assert M.summarize({"x": [2.0, 3.0]})["x"]["rel_delta"] == pytest.approx(0.5)
        # first 近 0 时 rel_delta 退化为 None（避免除零放大成无意义的大数）
        assert M.summarize({"x": [0.0, 1.0]})["x"]["rel_delta"] is None

    def test_empty_and_all_invalid_series_are_omitted(self):
        assert M.summarize({}) == {}
        assert M.summarize({"x": []}) == {}
        assert M.summarize({"x": [float("nan"), float("inf")]}) == {}

    def test_converging_series_reports_negative_slope(self):
        """构造"收敛"序列 -> 只反映为**负斜率**, 不产出任何布尔判定。"""
        s = M.summarize({"value_loss": [10.0, 6.0, 4.0, 3.0, 2.5]})
        assert s["value_loss"]["slope"] < 0
        assert s["value_loss"]["delta"] < 0

    # ---- ★ METHOD-1 结构断言 ----
    def test_no_boolean_verdict_is_emitted(self):
        s = M.summarize({"value_loss": [10.0, 6.0, 4.0], "entropy_loss": [-0.1, -0.2, -0.3]})
        for name, d in s.items():
            for k, v in d.items():
                assert not isinstance(v, bool), f"{name}.{k} 是布尔判定 —— 违反 METHOD-1"
        assert "converged" not in json.dumps(s), "不得有 'converged' 之类判定字段"

    def test_summarize_has_no_threshold_key(self):
        s = M.summarize({"x": [1.0, 2.0]})
        assert "threshold_applied" not in s["x"]
        assert not any("threshold" in k for k in s["x"])


# =====================================================================
# 落盘用序列
# =====================================================================

class TestSeriesForMeta:
    def test_drops_empty_and_rounds(self):
        h = M.MetricsHistory()
        h.record({"entropy_loss": 1.23456789})
        ser = h.series_for_meta()
        assert ser == {"entropy_loss": [1.234568]}

    def test_respects_cap_keeping_tail(self):
        h = M.MetricsHistory()
        for i in range(10):
            h.record({"entropy_loss": float(i)})
        ser = h.series_for_meta(cap=3)
        assert ser["entropy_loss"] == [7.0, 8.0, 9.0], "截断必须保留**最近**的点"

    def test_default_cap_is_documented_constant(self):
        assert M.SERIES_CAP == 2000

    def test_series_is_json_safe(self):
        h = M.MetricsHistory()
        for v in (0.0, 1e300, -1e300, 1e-300):
            h.record({"x": v})
        _assert_json_safe(h.series_for_meta())


# =====================================================================
# summarize_run（挂 train_meta["train_metrics"] 的那个）
# =====================================================================

class TestSummarizeRun:
    def test_actual_steps_separate_from_requested(self):
        """★ `actual_timesteps` 取自 model, `requested_timesteps` 是配置值 —— 必须分开。"""
        h = M.MetricsHistory()
        h.record({"entropy_loss": -0.5})
        r = M.summarize_run(model=_FakeModel(756), history=h, duration_s=12.3456,
                            requested_timesteps=800)
        assert r["actual_timesteps"] == 756
        assert r["requested_timesteps"] == 800
        assert r["timesteps_shortfall"] == 44
        assert r["duration_s"] == pytest.approx(12.346)

    def test_no_shortfall_key_when_exact(self):
        r = M.summarize_run(model=_FakeModel(800), history=M.MetricsHistory(),
                            duration_s=1.0, requested_timesteps=800)
        assert r["timesteps_shortfall"] == 0

    def test_state_contract_keys_present(self):
        r = M.summarize_run(model=_FakeModel(10), history=M.MetricsHistory())
        assert r["schema"] == 1
        assert r["threshold_applied"] is False, \
            "用户要求本批次**不得**施加'是否收敛'阈值（METHOD-1）"
        assert "METHOD-1" in r["note"]
        assert "summary" in r and "series" in r
        assert r["duration_s"] is None, "未传时长时必须为 None, 不得伪造成 0"

    def test_model_none_does_not_raise(self):
        r = M.summarize_run(model=None, history=M.MetricsHistory())
        assert r["actual_timesteps"] is None
        assert "timesteps_shortfall" not in r

    def test_broken_model_attribute_does_not_raise(self):
        class _Boom:
            @property
            def num_timesteps(self):
                raise RuntimeError("boom")
        r = M.summarize_run(model=_Boom(), history=M.MetricsHistory())
        assert r["actual_timesteps"] is None

    def test_history_none_defaults_to_empty(self):
        r = M.summarize_run(model=_FakeModel(5), history=None)
        assert r["n_records"] == 0 and r["summary"] == {}

    def test_skipped_counts_surface_only_when_present(self):
        h = M.MetricsHistory()
        h.record({"entropy_loss": 1.0})          # 其余 9 个指标都 skip
        r = M.summarize_run(model=_FakeModel(1), history=h)
        assert r["skipped_counts"]["grad_norm"] == 1
        r2 = M.summarize_run(model=_FakeModel(1), history=M.MetricsHistory())
        assert "skipped_counts" not in r2

    def test_extra_fields_merge(self):
        r = M.summarize_run(model=_FakeModel(1), history=M.MetricsHistory(),
                            extra={"weights_source_day": "20260905"})
        assert r["weights_source_day"] == "20260905"

    def test_end_to_end_is_json_serializable(self):
        """模拟真实训练: 10 次迭代 -> train_meta 片段必须能 json.dump。"""
        h = M.MetricsHistory()
        for i in range(10):
            h.record({"entropy_loss": -0.5 - 0.01 * i,
                      "policy_loss": 0.3 / (i + 1),
                      "value_loss": 10.0 * (0.9 ** i),
                      "approx_kl": float("nan") if i == 3 else 0.01 * i,
                      "grad_norm": 1.5 + i,
                      "std": 0.5})
        r = M.summarize_run(model=_FakeModel(540), history=h, duration_s=7.7,
                            requested_timesteps=800)
        _assert_json_safe(r)
        txt = json.dumps(r, ensure_ascii=False)
        assert "NaN" not in txt and "Infinity" not in txt
        assert json.loads(txt)["summary"]["grad_norm"]["slope"] > 0
        # NaN 被跳过而不是写进去
        assert r["summary"]["approx_kl"]["n"] == 9
        assert r["skipped_counts"]["approx_kl"] == 1


# =====================================================================
# logger 取值
# =====================================================================

class TestValuesFromLogger:
    def test_maps_sb3_keys_to_canonical_names(self):
        got = M.values_from_logger({
            "train/entropy_loss": -0.7,
            "train/policy_gradient_loss": 0.02,
            "train/value_loss": 3.5,
            "train/approx_kl": 0.011,
            "train/explained_variance": 0.42,
            "time/fps": 123.0,                     # 不在映射内 -> 忽略
        })
        assert got["entropy_loss"] == -0.7
        assert got["policy_loss"] == 0.02
        assert got["value_loss"] == 3.5
        assert got["approx_kl"] == 0.011
        assert got["explained_variance"] == 0.42
        assert "fps" not in got and "time/fps" not in got

    def test_skips_nan_and_missing(self):
        got = M.values_from_logger({"train/value_loss": float("nan")})
        assert got == {}

    def test_none_input(self):
        assert M.values_from_logger(None) == {}

    def test_metric_keys_are_declared_consistently(self):
        keys = M.all_metric_keys()
        assert set(M.METRIC_LOGGER_KEYS).issubset(set(keys))
        assert set(M.EXTRA_METRIC_KEYS).issubset(set(keys))
        assert "grad_norm" in keys and "grad_norm" not in M.METRIC_LOGGER_KEYS, \
            "grad_norm 不在 logger 里, 由 train() 单独采集"


# =====================================================================
# drl_train 接线（源码级, 避免 import torch）
# =====================================================================

class TestTrainWiring:
    """断言"学习中五项"真的有采集点, 而不是只在文档里写了。"""

    @staticmethod
    def _src():
        with open(os.path.join(_SRC, "drl_train.py"), encoding="utf-8") as f:
            return f.read()

    def test_import_present(self):
        assert "import drl_metrics" in self._src()

    def test_history_attribute_initialized_in_init(self):
        src = self._src()
        i = src.index("self.metric_history = drl_metrics.MetricsHistory()")
        assert src.index("class CVaR_PPO") < i

    def test_grad_norm_captured_from_clip_grad_norm(self):
        src = self._src()
        # 注意: 必须锚定**调用点** `th.nn.utils.clip_grad_norm_(`, 不能锚 `clip_grad_norm_`
        # —— 后者会先命中解释该做法的注释。
        i = src.index("th.nn.utils.clip_grad_norm_(")
        assert "grad_norms.append" in src[i:i + 600], \
            "必须采集梯度范数（clip_grad_norm_ 返回的裁剪前总范数）"

    def test_snapshot_at_end_of_train_not_via_callback(self):
        """快照放在 train() 末尾 —— 不依赖 SB3 callback 顺序, 换版本不会静默采空。"""
        src = self._src()
        i = src.index("self.metric_history.record(_vals)")
        assert src.index("train/clip_range_vf", 0) < i

    def test_both_entry_points_emit_train_metrics(self):
        assert self._src().count('"train_metrics": drl_metrics.summarize_run(') == 2, \
            "run_drl_train 与 run_factor_value_drl 两个入口都要落盘"

    def test_duration_is_measured_around_learn(self):
        src = self._src()
        assert src.count("_t_learn0 = dt.datetime.now()") == 2
        assert src.count("duration_s=_learn_seconds") == 2

    def test_config_timesteps_not_overwritten_by_actual(self):
        """`total_timesteps` 仍是配置值; 实际步数另开字段 —— 不得混为一谈。"""
        src = self._src()
        assert '"total_timesteps": total_timesteps' in src
        assert '"actual_timesteps":' not in src, \
            "drl_train 不该自己算 actual_timesteps（由 drl_metrics 统一口径）"

    def test_collection_failure_cannot_break_training(self):
        src = self._src()
        i = src.index("self.metric_history.record(_vals)")
        seg = src[max(0, i - 700):i + 120]
        assert "except Exception" in seg, "采集必须被 try 包住, 绝不影响训练主链路"

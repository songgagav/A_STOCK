# -*- coding: utf-8 -*-
"""单元测试: weighting_scheme.py (加权方案) + factor_fusion.py (中性化/融合).

覆盖:
  - weighting_scheme: compute_weights / factor_weights / fuse_scores / load_ic_history
  - factor_fusion: neutralize_factor / weighted_fusion / compute_fusion_weights / _winsor / _is_on
"""
from __future__ import annotations

import os
import sys
import json
import math
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

# 将被测模块路径加入 sys.path
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from factor_mine import weighting_scheme as ws
from factor_fusion import (
    neutralize_factor,
    weighted_fusion,
    compute_fusion_weights,
    _winsor,
    _is_on,
    FACTOR_WEIGHTS,
)


# ===================================================================
#  weighting_scheme — compute_weights
# ===================================================================
class TestComputeWeights(unittest.TestCase):
    """compute_weights: 单因子权重计算, 三种方案 + 边缘情况."""

    def setUp(self):
        self.rng = np.random.default_rng(42)
        # 60 个交易日的稳定正 IC, 均值 ~0.05, 有一定波动
        self.positive_ic = pd.Series(
            0.05 + self.rng.normal(0, 0.03, 60), name="factor_a"
        )
        # 负均值 IC
        self.negative_ic = pd.Series(
            -0.03 + self.rng.normal(0, 0.04, 60), name="factor_b"
        )
        # 零均值 (噪声)
        self.zero_ic = pd.Series(self.rng.normal(0, 0.02, 60), name="factor_c")
        # 含 inf/nan 序列
        self.dirty_ic = self.positive_ic.copy()
        self.dirty_ic.iloc[5] = np.inf
        self.dirty_ic.iloc[10] = -np.inf
        self.dirty_ic.iloc[15] = np.nan

    # ---- 等权 ----
    def test_equal_scheme(self):
        r = ws.compute_weights(self.positive_ic, scheme="equal")
        self.assertEqual(r["scheme"], "equal")
        self.assertEqual(r["weight"], 1.0)
        self.assertIsNotNone(r["ic_mean"])
        self.assertIsNone(r["icir"])

    # ---- IC 均值 ----
    def test_ic_mean_positive(self):
        r = ws.compute_weights(self.positive_ic, scheme="ic_mean")
        self.assertEqual(r["scheme"], "ic_mean")
        self.assertGreater(r["weight"], 0.0)
        self.assertAlmostEqual(r["weight"], r["ic_mean"], places=4)

    def test_ic_mean_negative(self):
        r = ws.compute_weights(self.negative_ic, scheme="ic_mean")
        self.assertEqual(r["weight"], 0.0)

    def test_ic_mean_zero(self):
        r = ws.compute_weights(self.zero_ic, scheme="ic_mean")
        self.assertEqual(r["weight"], 0.0)

    # ---- ICIR ----
    def test_icir_positive(self):
        r = ws.compute_weights(self.positive_ic, scheme="icir")
        self.assertEqual(r["scheme"], "icir")
        self.assertGreater(r["weight"], 0.0)
        self.assertIsNotNone(r["icir"])
        self.assertGreater(r["icir"], 0.0)
        # weight 应等于 icir (未归一化时)
        self.assertAlmostEqual(r["weight"], r["icir"], places=4)

    def test_icir_negative(self):
        r = ws.compute_weights(self.negative_ic, scheme="icir")
        self.assertEqual(r["weight"], 0.0)

    def test_icir_zero(self):
        r = ws.compute_weights(self.zero_ic, scheme="icir")
        self.assertEqual(r["weight"], 0.0)

    def test_icir_min_threshold(self):
        """min_icir 截断: 低于阈值的 icir 置 0."""
        ic = pd.Series(np.full(60, 0.01))  # 小正 IC, 但 icir 大
        r = ws.compute_weights(ic, scheme="icir", min_icir=5.0)
        self.assertEqual(r["weight"], 0.0)

    # ---- 边缘情况 ----
    def test_fewer_than_10(self):
        r = ws.compute_weights(pd.Series([0.01, 0.02, 0.03]), scheme="icir")
        self.assertEqual(r["weight"], 0.0)
        self.assertEqual(r["n"], 3)

    def test_empty_series(self):
        r = ws.compute_weights(pd.Series(dtype=float), scheme="icir")
        self.assertEqual(r["weight"], 0.0)
        self.assertEqual(r["n"], 0)

    def test_window_limiting(self):
        """超过 window 个样本, 应只取最近 window 个."""
        long_ic = pd.Series(np.random.default_rng(42).normal(0.03, 0.02, 200))
        r = ws.compute_weights(long_ic, scheme="icir", window=60)
        self.assertEqual(r["n"], 60)
        self.assertGreater(r["weight"], 0.0)

    def test_inf_nan_cleaned(self):
        r = ws.compute_weights(self.dirty_ic, scheme="ic_mean")
        # dirty 序列有 3 个 inf/nan, 应当被净化
        clean_n = len(self.dirty_ic.dropna().replace([np.inf, -np.inf], np.nan).dropna())
        self.assertEqual(r["n"], clean_n)
        self.assertGreater(r["weight"], 0.0)

    def test_unknown_scheme(self):
        with self.assertRaises(ValueError):
            ws.compute_weights(self.positive_ic, scheme="unknown_scheme")

    def test_std_zero_icir(self):
        """ic_std=0 (所有值相同) 时 icir=0, weight=0."""
        flat = pd.Series(np.full(60, 0.02))
        r = ws.compute_weights(flat, scheme="icir")
        self.assertEqual(r["weight"], 0.0)
        self.assertEqual(r["icir"], 0.0)


# ===================================================================
#  weighting_scheme — factor_weights
# ===================================================================
class TestFactorWeights(unittest.TestCase):
    """factor_weights: 多因子权重向量计算."""

    def setUp(self):
        dates = pd.date_range("2026-01-01", periods=100, freq="B")
        self.ic_history = pd.DataFrame(
            {
                "hold_5": 0.04 + np.random.default_rng(42).normal(0, 0.03, 100),
                "hold_10": 0.02 + np.random.default_rng(43).normal(0, 0.04, 100),
            },
            index=dates,
        )
        self.ic_history.index.name = "date"
        self.factors = ["hold_5", "hold_10"]

    def test_equal_scheme(self):
        r = ws.factor_weights(self.factors, scheme="equal",
                              ic_history=self.ic_history)
        w = r["weights"]
        self.assertAlmostEqual(w["hold_5"]["weight"], 0.5, places=6)
        self.assertAlmostEqual(w["hold_10"]["weight"], 0.5, places=6)
        self.assertEqual(r["meta"]["scheme"], "equal")
        self.assertEqual(r["meta"]["n_factors"], 2)
        self.assertAlmostEqual(r["meta"]["total_weight"], 1.0, places=6)

    def test_ic_mean_scheme(self):
        r = ws.factor_weights(self.factors, scheme="ic_mean",
                              ic_history=self.ic_history)
        w = r["weights"]
        # 因子的 ic_mean 不同, 权重应不同
        self.assertNotAlmostEqual(w["hold_5"]["weight"], w["hold_10"]["weight"],
                                  places=3)
        # 总权重归一化
        self.assertAlmostEqual(r["meta"]["total_weight"], 1.0, places=6)

    def test_icir_scheme(self):
        r = ws.factor_weights(self.factors, scheme="icir",
                              ic_history=self.ic_history)
        w = r["weights"]
        self.assertIsNotNone(w["hold_5"]["icir"])
        self.assertIsNotNone(w["hold_10"]["icir"])
        self.assertAlmostEqual(r["meta"]["total_weight"], 1.0, places=6)

    def test_skip_normalize(self):
        r = ws.factor_weights(self.factors, scheme="equal",
                              ic_history=self.ic_history, normalize=False)
        w = r["weights"]
        self.assertEqual(w["hold_5"]["weight"], 1.0)
        self.assertEqual(w["hold_10"]["weight"], 1.0)
        self.assertAlmostEqual(r["meta"]["total_weight"], 2.0, places=6)

    def test_factor_not_in_history(self):
        r = ws.factor_weights(["nonexistent"], scheme="icir",
                              ic_history=self.ic_history)
        w = r["weights"]["nonexistent"]
        self.assertEqual(w["weight"], 1.0)
        self.assertIn("note", w)

    def test_no_ic_history(self):
        df = pd.DataFrame()
        r = ws.factor_weights(["f1", "f2"], scheme="icir", ic_history=df)
        self.assertEqual(r["meta"]["has_ic_history"], False)
        self.assertAlmostEqual(r["meta"]["total_weight"], 1.0, places=6)
        for f in ["f1", "f2"]:
            self.assertEqual(r["weights"][f]["note"], "无IC历史文件")

    def test_all_weights_zero_fallback(self):
        """所有因子权重为 0 时, 应当均等兜底."""
        idx = pd.date_range("2026-01-01", periods=60, freq="B")
        ic = pd.DataFrame({"hold_5": [0.0] * 60, "hold_5_dup": [0.0] * 60}, index=idx)
        r = ws.factor_weights(["hold_5", "hold_5_dup"], scheme="ic_mean",
                              ic_history=ic)
        for f in ["hold_5", "hold_5_dup"]:
            self.assertEqual(r["weights"][f]["weight"], 0.5)
            self.assertEqual(r["weights"][f]["note"], "均等兜底 (总权重为0)")

    def test_hold_column_selection(self):
        """使用不同的 hold 列."""
        ic = pd.DataFrame(
            {"hold_5": [0.05] * 60, "hold_20": [0.01] * 60},
            index=pd.date_range("2026-01-01", periods=60, freq="B"),
        )
        r5 = ws.factor_weights(["hold_5"], scheme="equal", ic_history=ic, hold="hold_5")
        r20 = ws.factor_weights(["hold_5"], scheme="equal", ic_history=ic, hold="hold_20")
        self.assertEqual(r5["meta"]["hold"], "hold_5")
        self.assertEqual(r20["meta"]["hold"], "hold_20")


# ===================================================================
#  weighting_scheme — fuse_scores
# ===================================================================
class TestFuseScores(unittest.TestCase):
    """fuse_scores: 多因子得分合成."""

    def setUp(self):
        self.factor_scores = {
            "factor_a": {"A": 1.0, "B": 0.5, "C": -0.5},
            "factor_b": {"A": 0.8, "B": -0.3, "C": 0.1},
        }
        self.weight_config = {
            "factor_a": {"weight": 0.6},
            "factor_b": {"weight": 0.4},
        }

    def test_basic_fusion(self):
        result = ws.fuse_scores(self.factor_scores, self.weight_config)
        self.assertIn("A", result)
        self.assertIn("B", result)
        self.assertIn("C", result)

        # 手动计算 raw score
        raw_a = 0.6 * 1.0 + 0.4 * 0.8  # = 0.92
        raw_b = 0.6 * 0.5 + 0.4 * (-0.3)  # = 0.18
        raw_c = 0.6 * (-0.5) + 0.4 * 0.1  # = -0.26
        raws = np.array([raw_a, raw_b, raw_c])
        mu, sd = raws.mean(), raws.std()
        expected_a = (raw_a - mu) / sd

        self.assertAlmostEqual(result["A"], expected_a, places=6)

        # 结果应近似 z-score 标准化
        vals = np.array(list(result.values()))
        self.assertAlmostEqual(float(vals.mean()), 0.0, places=10)
        self.assertAlmostEqual(float(vals.std()), 1.0, places=6)

    def test_zero_weight_skip(self):
        """权重为 0 的因子应跳过."""
        wc = {"factor_a": {"weight": 0.0}, "factor_b": {"weight": 1.0}}
        result = ws.fuse_scores(self.factor_scores, wc)
        # 只有 factor_b 参与
        raw_b = {s: 1.0 * self.factor_scores["factor_b"][s] for s in ["A", "B", "C"]}
        vals = np.array(list(raw_b.values()))
        mu, sd = vals.mean(), vals.std()
        expected = {s: (v - mu) / sd for s, v in raw_b.items()}
        for s in ["A", "B", "C"]:
            self.assertAlmostEqual(result[s], expected[s], places=6)

    def test_empty_factor_scores(self):
        result = ws.fuse_scores({}, self.weight_config)
        self.assertEqual(result, {})

    def test_no_common_symbols(self):
        fs = {"fa": {"X": 1.0}, "fb": {"Y": 2.0}}
        result = ws.fuse_scores(fs, {"fa": {"weight": 1.0}, "fb": {"weight": 1.0}})
        # 各只有 1 个 symbol, 合成后标准差为 0 或只有 1 个值 -> 不做 z-score
        self.assertEqual(len(result), 2)

    def test_single_symbol(self):
        """单标的时不做 z-score 标准化 (std=0, 返回原始值)."""
        fs = {"fa": {"A": 1.5}}
        result = ws.fuse_scores(fs, {"fa": {"weight": 1.0}})
        self.assertEqual(result, {"A": 1.5})


# ===================================================================
#  weighting_scheme — load_ic_history
# ===================================================================
class TestLoadIcHistory(unittest.TestCase):
    """load_ic_history: CSV 读取与列归一化."""

    def _write_csv(self, lines: list[str]) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False)
        for l in lines:
            f.write(l + "\n")
        f.close()
        return f.name

    def test_load_with_day_column(self):
        path = self._write_csv([
            "day,ic_h1,ic_h5,ic_h10",
            "2026-01-01,0.01,0.02,0.03",
            "2026-01-02,0.02,0.03,0.04",
        ])
        df = ws.load_ic_history(path)
        self.assertEqual(len(df), 2)
        self.assertIn("hold_1", df.columns)
        self.assertIn("hold_5", df.columns)
        self.assertIn("hold_10", df.columns)
        self.assertNotIn("ic_h1", df.columns)
        self.assertAlmostEqual(df.loc["2026-01-01", "hold_1"], 0.01)
        os.unlink(path)

    def test_load_with_date_column(self):
        path = self._write_csv([
            "date,ic_h5",
            "2026-01-01,0.05",
            "2026-01-02,0.06",
        ])
        df = ws.load_ic_history(path)
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.loc["2026-01-01", "hold_5"], 0.05)
        os.unlink(path)

    def test_file_not_found(self):
        df = ws.load_ic_history("/nonexistent/path.csv")
        self.assertTrue(df.empty)

    def test_empty_csv(self):
        path = self._write_csv(["day,ic_h5"])
        df = ws.load_ic_history(path)
        self.assertTrue(df.empty)
        os.unlink(path)

    def test_no_date_column(self):
        """无日期列时返回原始 df (hold_* 列不存在, 但函数返回原生 df)."""
        path = self._write_csv(["x,ic_h5", "1,0.1"])
        df = ws.load_ic_history(path)
        # 无 date 列, 函数返回原始 df; 不会有 hold_* 列
        self.assertNotIn("hold_5", df.columns)
        os.unlink(path)

    def test_no_ic_columns(self):
        """有日期列但无 ic_h* 列, 返回原始 df."""
        path = self._write_csv(["day,some_other_col", "2026-01-01,1.0"])
        df = ws.load_ic_history(path)
        # 列名不匹配 ic_h*, 不会转换, 返回原始 df
        self.assertIn("some_other_col", df.columns)
        os.unlink(path)


# ===================================================================
#  factor_fusion — neutralize_factor
# ===================================================================
class TestNeutralizeFactor(unittest.TestCase):
    """neutralize_factor: 行业/市值中性化."""

    def setUp(self):
        self.rng = np.random.default_rng(42)
        n = 200
        self.symbols = [f"{i:06d}" for i in range(n)]
        self.factor = pd.Series(
            self.rng.normal(0, 1, n), index=self.symbols, name="test_factor"
        )
        self.industry = pd.Series(
            self.rng.choice(["金融", "科技", "消费", "医药", "制造"], n),
            index=self.symbols,
        )
        self.ln_size = pd.Series(
            self.rng.uniform(20, 25, n), index=self.symbols, name="ln_size"
        )

    def test_basic_neutralization(self):
        result = neutralize_factor(self.factor, self.industry, self.ln_size)
        self.assertEqual(len(result), len(self.factor))
        # 中性化后应与行业和市值近似正交
        ind_dummies = pd.get_dummies(self.industry.loc[result.index])
        r_size = np.corrcoef(
            result.to_numpy(), self.ln_size.loc[result.index].to_numpy()
        )[0, 1]
        self.assertLess(abs(r_size), 0.3, "残差应与市值正交")

    def test_z_score_shape(self):
        result = neutralize_factor(self.factor, self.industry, self.ln_size)
        vals = result.to_numpy(dtype=float)
        self.assertAlmostEqual(float(vals.mean()), 0.0, places=10)
        self.assertAlmostEqual(float(vals.std()), 1.0, places=6)

    def test_empty_input(self):
        result = neutralize_factor(pd.Series(dtype=float))
        self.assertTrue(result.empty)

    def test_less_than_30_samples(self):
        small = pd.Series([1.0, 2.0, 3.0], index=["A", "B", "C"])
        ind = pd.Series(["金融", "科技", "消费"], index=["A", "B", "C"])
        result = neutralize_factor(small, ind)
        self.assertEqual(len(result), 3)  # 保留原始 index
        self.assertTrue(result.isna().all())  # 但全为 NaN

    def test_no_industry_no_size(self):
        """只有因子值, 行业和市值为空."""
        result = neutralize_factor(self.factor)
        # 行业默认 "unknown", ln_size 默认 0, 仍应能计算
        self.assertEqual(len(result), len(self.factor))
        # 所有值应有限
        self.assertTrue(np.all(np.isfinite(result.to_numpy())))

    def test_winsor_bounds(self):
        """极端值被 Winsorize 剪裁后再中性化."""
        factor = self.factor.copy()
        factor.iloc[:5] = 999.0  # 极端大值
        factor.iloc[-5:] = -999.0  # 极端小值
        result = neutralize_factor(factor, self.industry, self.ln_size,
                                    winsor_lo=0.01, winsor_hi=0.99)
        self.assertEqual(len(result), len(factor))
        # 结果不应包含极端值
        self.assertLess(result.abs().max(), 10)

    def test_degenerate_all_same(self):
        """所有因子值相同 -> 回归退化, 返回空 (或全 NaN)."""
        same = pd.Series(1.0, index=self.symbols[:50])
        ind = pd.Series("金融", index=self.symbols[:50])
        sz = pd.Series(22.0, index=self.symbols[:50])
        result = neutralize_factor(same, ind, sz)
        # 退化时返回值全为 NaN 或空
        self.assertTrue(result.empty or result.isna().all())

    def test_missing_values_in_factor(self):
        """因子中有 NaN 时应跳过."""
        factor = self.factor.copy()
        factor.iloc[::10] = np.nan  # 每 10 个设一个 NaN
        result = neutralize_factor(factor, self.industry, self.ln_size)
        self.assertGreater(len(result), 0)
        # NaN 对应的标的不应在结果中
        nan_idx = factor.index[factor.isna()]
        for idx in nan_idx:
            self.assertNotIn(idx, result.index)


# ===================================================================
#  factor_fusion — weighted_fusion
# ===================================================================
class TestWeightedFusion(unittest.TestCase):
    """weighted_fusion: 加权融合入口."""

    def test_with_weight_config(self):
        scores = {
            "fa": {"A": 1.0, "B": 0.0},
            "fb": {"A": 0.5, "B": -0.5},
        }
        wc = {"fa": {"weight": 0.7}, "fb": {"weight": 0.3}}
        result = weighted_fusion(scores, weight_config=wc)
        self.assertIn("A", result)
        self.assertIn("B", result)
        vals = np.array(list(result.values()))
        self.assertAlmostEqual(float(vals.mean()), 0.0, places=10)

    @patch("factor_fusion.compute_fusion_weights")
    def test_without_weight_config(self, mock_cfw):
        """weight_config 为 None 时自动计算."""
        mock_cfw.return_value = {
            "weights": {"fa": {"weight": 0.5}, "fb": {"weight": 0.5}},
            "meta": {"scheme": "equal"},
        }
        scores = {"fa": {"A": 1.0}, "fb": {"A": 2.0}}
        result = weighted_fusion(scores, weight_config=None, scheme="equal")
        self.assertIn("A", result)
        mock_cfw.assert_called_once()


# ===================================================================
#  factor_fusion — compute_fusion_weights
# ===================================================================
class TestComputeFusionWeights(unittest.TestCase):
    """compute_fusion_weights: 融合权重计算."""

    @patch("factor_mine.weighting_scheme.load_ic_history")
    @patch("factor_mine.weighting_scheme.factor_weights")
    def test_delegation(self, mock_fw, mock_load):
        mock_load.return_value = pd.DataFrame(
            {"hold_5": [0.04] * 60},
            index=pd.date_range("2026-01-01", periods=60, freq="B"),
        )
        mock_fw.return_value = {"weights": {}, "meta": {}}
        result = compute_fusion_weights(["pb_inv", "ep"], scheme="icir")
        mock_fw.assert_called_once()
        self.assertIn("weights", result)
        self.assertIn("meta", result)

    @patch("factor_mine.weighting_scheme.load_ic_history", side_effect=Exception("DB error"))
    def test_exception_fallback(self, mock_load):
        """异常时等权兜底."""
        result = compute_fusion_weights(["pb_inv", "ep"], scheme="icir")
        w = result["weights"]
        self.assertEqual(len(w), 2)
        self.assertAlmostEqual(w["pb_inv"]["weight"], 0.5, places=6)
        self.assertAlmostEqual(w["ep"]["weight"], 0.5, places=6)
        self.assertEqual(result["meta"]["scheme"], "equal_fallback")

    def test_default_factor_list(self):
        """factor_list=None 应使用 FACTOR_WEIGHTS.keys()."""
        with patch("factor_mine.weighting_scheme.factor_weights") as mock_fw:
            mock_fw.return_value = {"weights": {}, "meta": {}}
            compute_fusion_weights()
            args, _ = mock_fw.call_args
            self.assertEqual(args[0], list(FACTOR_WEIGHTS.keys()))


# ===================================================================
#  factor_fusion — _winsor
# ===================================================================
class TestWinsor(unittest.TestCase):
    """_winsor: 分位数截断."""

    def test_basic_winsor(self):
        s = pd.Series(range(100), dtype=float)
        w = _winsor(s, lo_q=0.05, hi_q=0.95)
        lo = s.quantile(0.05)
        hi = s.quantile(0.95)
        self.assertAlmostEqual(w.min(), lo, places=2)
        self.assertAlmostEqual(w.max(), hi, places=2)

    def test_all_na(self):
        s = pd.Series([np.nan, np.nan])
        w = _winsor(s)
        self.assertTrue(w.isna().all())


# ===================================================================
#  factor_fusion — _is_on
# ===================================================================
class TestIsOn(unittest.TestCase):
    """_is_on: 环境变量控制."""

    def test_default_on(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_is_on())

    def test_fusion_score_off(self):
        with patch.dict(os.environ, {"FUSION_SCORE": "0"}, clear=True):
            self.assertFalse(_is_on())

    def test_force_fml(self):
        with patch.dict(os.environ, {"FORCE_FML": "1"}, clear=True):
            self.assertFalse(_is_on())

    def test_both_set_force_fml_wins(self):
        with patch.dict(os.environ, {"FUSION_SCORE": "1", "FORCE_FML": "1"}, clear=True):
            self.assertFalse(_is_on())

    def test_fusion_score_explicit_on(self):
        with patch.dict(os.environ, {"FUSION_SCORE": "1"}, clear=True):
            self.assertTrue(_is_on())


# ===================================================================
#  factor_fusion — neutralize_factor 正交性验证
# ===================================================================
class TestNeutralizeFactorOrthogonality(unittest.TestCase):
    """验证 neutralize_factor 的残差与行业/市值正交性."""

    def test_orthogonal_to_industry(self):
        rng = np.random.default_rng(42)
        n = 500
        syms = [f"{i:06d}" for i in range(n)]
        factor = pd.Series(rng.normal(0, 1, n), index=syms)
        inds = pd.Series(rng.choice(["金融", "科技", "消费", "医药", "制造"], n), index=syms)
        ln_size = pd.Series(rng.uniform(20, 25, n), index=syms)

        result = neutralize_factor(factor, inds, ln_size)

        # 对每个行业, 残差的均值应接近 0
        for ind in inds.unique():
            mask = inds.loc[result.index] == ind
            vals = result.loc[mask].to_numpy(dtype=float)
            self.assertLess(abs(float(vals.mean())), 0.2,
                            f"行业 {ind} 残差均值应接近 0")

    def test_orthogonal_to_size(self):
        rng = np.random.default_rng(42)
        n = 500
        syms = [f"{i:06d}" for i in range(n)]
        # 故意构造与市值高度相关的因子
        ln_size = pd.Series(rng.uniform(20, 25, n), index=syms)
        factor = 2.0 * ln_size + rng.normal(0, 0.5, n)
        inds = pd.Series(rng.choice(["金融", "科技", "消费"], n), index=syms)

        result = neutralize_factor(factor, inds, ln_size)
        r_size = np.corrcoef(
            result.to_numpy(), ln_size.loc[result.index].to_numpy()
        )[0, 1]
        # 原始因子与市值的相关性接近 1, 中性化后应接近 0
        orig_r = np.corrcoef(factor.to_numpy(), ln_size.to_numpy())[0, 1]
        self.assertGreater(abs(orig_r), 0.9, "原始因子应与市值强相关")
        self.assertLess(abs(r_size), 0.3, "中性化后应与市值正交")


# ===================================================================
#  运行入口
# ===================================================================
if __name__ == "__main__":
    unittest.main(verbosity=2)
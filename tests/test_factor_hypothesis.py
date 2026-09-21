# -*- coding: utf-8 -*-
"""factor_hypothesis 的单元测试。

覆盖四类"必须锁死"的行为:
  1. **身份稳定**: 同一命题 => 同一 hypothesis_id (幂等), 不同命题 => 不同 id;
  2. **前置防线**: lint 对缺字段 / 未来函数 / 常量表达式 / 空命题的拒绝, 且 reasons
     指向正确的那个原因 (不是"反正拒了");
  3. **假设级证据**: 方向一致性 / 覆盖率 / 非退化 / 分段符号稳定性四类情形;
  4. **流水线纪律**: 没过 lint/evidence 的假设**绝不进入收益评估** (用 mock 计数器锁死)。

测试全部使用内存构造的小数组, 不依赖任何真实数据文件, 不联网, 不 import scipy。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid

import numpy as np
import pandas as pd          # 仅测试中使用 (用于验证 pandas 对象也满足数据契约)
import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_REPO = os.path.dirname(_SRC)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import factor_hypothesis as fh  # noqa: E402

# ---------------------------------------------------------------------------
# 公共夹具与构造器
# ---------------------------------------------------------------------------

THESIS = "过去20日累计涨幅高的股票, 未来5日收益更高 (动量延续)"
MECH = "资金流与信息扩散具有惯性: 上涨吸引增量资金与关注度, 形成短期正反馈。"
AVAIL = ["ret_20", "ret_5", "turnover"]


@pytest.fixture
def tmpdir_ws() -> str:
    """**工作区内**的临时目录 (刻意不用 pytest 的 tmp_path, 也不用 tempfile.mkdtemp)。

    原因: 受限执行环境里系统临时目录允许建目录却拒绝写文件; 而 tempfile.mkdtemp 会把
    目录 chmod 到 0o700, 该目录随后同样拒绝写入 —— 会让"账本能不能写"这类用例测出与
    环境有关的假结果。这里用普通 mkdir + 唯一名, 用完尽力删除。
    """
    base = os.path.join(_REPO, "data", "_tmp_factor_hypothesis_tests")
    os.makedirs(base, exist_ok=True)
    d = os.path.join(base, "case_" + uuid.uuid4().hex[:8])
    os.makedirs(d, exist_ok=False)
    yield d
    shutil.rmtree(d, ignore_errors=True)
    try:                       # 用例目录清空后顺手收掉基目录 (非空说明还有并行用例在用)
        os.rmdir(base)
    except OSError:
        pass


def mk(**kw) -> fh.Hypothesis:
    """构造一个默认"健康"的假设; 用例只覆写自己关心的字段。"""
    base = dict(thesis=THESIS, mechanism=MECH, direction=1, expression="ret_20",
                required_fields=["ret_20"], source="human")
    base.update(kw)
    return fh.Hypothesis(**base)


@pytest.fixture
def panel() -> dict:
    """(60 期 × 20 只) 合成面板: ret_20 与未来收益正相关 (rank IC ≈ 0.88)。"""
    rng = np.random.default_rng(7)
    ret20 = rng.normal(0.0, 1.0, (60, 20))
    target = 0.8 * ret20 + rng.normal(0.0, 0.4, (60, 20))
    return {
        "ret_20": ret20,
        "ret_5": rng.normal(0.0, 1.0, (60, 20)),
        "turnover": np.abs(rng.normal(3.0, 1.0, (60, 20))),
        fh.DEFAULT_TARGET_KEY: target,
    }


@pytest.fixture
def unstable_panel() -> dict:
    """全样本方向为正, 但 5 段里有 3 段反号 —— 用来测"分段稳定性"。"""
    n = 120
    rng = np.random.default_rng(3)
    fs, ys = [], []
    for k in range(5):
        base = np.linspace(0, 1, n) + rng.normal(0, 0.01, n)
        if k < 2:
            fs.append(base)
            ys.append(base)                 # 段内正相关
        else:
            fs.append(10 + base)
            ys.append(6 - base)             # 段内负相关, 但整体水平更高
    return {"f": np.concatenate(fs), fh.DEFAULT_TARGET_KEY: np.concatenate(ys)}


def unstable_hyp() -> fh.Hypothesis:
    return mk(thesis="测试命题: 该量在未来一段时间内与收益保持同向关系",
              mechanism="测试机制: 这是一段用于单元测试的合成因果链条说明文字。",
              expression="f", required_fields=["f"], source="template")


def llm_payload(items: list) -> str:
    return json.dumps({"hypotheses": items}, ensure_ascii=False)


def stub_llm(payload, capture: list | None = None):
    """返回一个 llm_call 回调; capture 非空时把 prompt 收集进去。"""
    def call(prompt: str) -> str:
        if capture is not None:
            capture.append(prompt)
        return payload if isinstance(payload, str) else llm_payload(payload)
    return call


VALID_ITEM = {"thesis": THESIS, "mechanism": MECH, "direction": "positive",
              "expression": "ret_20", "required_fields": ["ret_20"]}


# ===========================================================================
# 1. 表达式引擎 / 与 gp_mine_daily 的形态兼容
# ===========================================================================

class TestExpressionEngine:

    def test_canonical_prefix_form_is_idempotent(self):
        assert fh.to_canonical("add(ret_20, ret_5)") == "add(ret_20, ret_5)"
        assert fh.to_canonical(fh.to_canonical("add(ret_20, ret_5)")) == "add(ret_20, ret_5)"

    def test_infix_is_normalized_into_prefix(self):
        assert fh.to_canonical("ret_20 * -1") == "mul(ret_20, -1)"
        assert fh.to_canonical("(ret_20 - ret_5) / 2") == "div(sub(ret_20, ret_5), 2)"

    def test_number_formatting_is_normalized(self):
        """1 与 1.0 是同一个常量 => 规范形态一致 (身份幂等的前提)。"""
        assert fh.to_canonical("mul(ret_20, 1.0)") == fh.to_canonical("mul(ret_20, 1)")

    def test_expression_fields_order_and_dedup(self):
        e = "div(sub(ret_20, ret_5), ts_std(ret_20, 20))"
        assert fh.expression_fields(e) == ["ret_20", "ret_5"]

    @pytest.mark.parametrize("bad", [
        "add(ret_20,)",          # 缺参数
        "add(ret_20)",           # 元数不对
        "add(ret_20, ret_5, 1)",  # 元数不对
        "foo(ret_20)",           # 未知函数
        "ret_20 +",              # 悬空运算符
        "ret_20 ret_5",          # 尾部多余 token
        "ret_20 @ ret_5",        # 非法字符
        "(ret_20 + ret_5",       # 缺右括号
        "add ret_20, ret_5",     # 已知函数名没带括号
        "",                      # 空表达式
    ])
    def test_parse_failures_raise_loudly(self, bad):
        with pytest.raises(fh.ExpressionError):
            fh.parse_expression(bad)

    def test_evaluate_infix_equals_prefix(self, panel):
        a = fh.evaluate_expression("(ret_20 - ret_5) * 2", panel)
        b = fh.evaluate_expression("mul(sub(ret_20, ret_5), 2)", panel)
        assert np.allclose(a, b, equal_nan=True)

    def test_gp_daily_readable_expression_is_computable_and_causal(self):
        """gp_mine_daily 报告里的 readable 表达式可直接算, 且滚动窗口不吃未来数据。"""
        rng = np.random.default_rng(0)
        data = {"ret_20": rng.normal(size=(30, 5)), "ret_5": rng.normal(size=(30, 5))}
        v = fh.evaluate_expression("div(sub(ret_20, ret_5), ts_std(ret_20, 20))", data)
        assert v.shape == (30, 5)
        assert np.isnan(v[:19]).all(), "窗口未满的位置必须是 NaN(不许用未来数据补齐)"
        assert np.isfinite(v[19:]).all()

    def test_ts_delta_uses_only_past(self):
        x = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        v = fh.evaluate_expression("ts_delta(x, 1)", {"x": x})
        assert np.isnan(v[0])
        assert np.allclose(v[1:], [1.0, 2.0, 4.0, 8.0])

    def test_ts_window_ops_known_values(self):
        x = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        mean = fh.evaluate_expression("ts_mean(x, 3)", {"x": x})
        assert np.isnan(mean[:2]).all()
        assert np.allclose(mean[2:], [7 / 3, 14 / 3, 28 / 3])
        mx = fh.evaluate_expression("ts_max(x, 3)", {"x": x})
        assert np.allclose(mx[2:], [4.0, 8.0, 16.0])
        mn = fh.evaluate_expression("ts_min(x, 3)", {"x": x})
        assert np.allclose(mn[2:], [1.0, 2.0, 4.0])
        sd = fh.evaluate_expression("ts_std(x, 2)", {"x": x})
        assert np.isnan(sd[0]) and np.allclose(sd[1:4], [np.std([1, 2], ddof=1),
                                                         np.std([2, 4], ddof=1),
                                                         np.std([4, 8], ddof=1)])

    def test_ts_rank_is_fraction_of_window_not_above_current(self):
        desc = np.array([3.0, 2.0, 1.0])
        v = fh.evaluate_expression("ts_rank(desc, 3)", {"desc": desc})
        assert v[2] == pytest.approx(1 / 3)

    def test_ts_ops_with_insufficient_history_are_nan(self):
        x = np.array([1.0, 2.0])
        assert np.isnan(fh.evaluate_expression("ts_mean(x, 5)", {"x": x})).all()

    def test_ts_zscore_is_nan_before_window_fills(self):
        x = np.arange(10.0)
        v = fh.evaluate_expression("ts_zscore(x, 4)", {"x": x})
        assert np.isnan(v[:3]).all() and np.isfinite(v[3:]).all()

    def test_cs_rank_is_per_row_and_normalized(self):
        a = np.array([[3.0, 1.0, 2.0], [10.0, 20.0, 30.0]])
        r = fh.evaluate_expression("cs_rank(a)", {"a": a})
        assert np.allclose(r[0], [1.0, 1 / 3, 2 / 3])
        assert np.allclose(r[1], [1 / 3, 2 / 3, 1.0])

    def test_cs_demean_and_zscore_are_cross_sectional(self):
        a = np.array([[1.0, 2.0, 3.0], [5.0, 5.0, 5.0]])
        dm = fh.evaluate_expression("cs_demean(a)", {"a": a})
        assert np.allclose(dm[0], [-1.0, 0.0, 1.0])
        assert np.allclose(dm[1], [0.0, 0.0, 0.0])
        z = fh.evaluate_expression("cs_zscore(a)", {"a": a})
        assert np.allclose(z[1], [0.0, 0.0, 0.0]), "零方差的截面返回 0 (与 gp_mine_daily 同口径)"

    def test_degenerate_inputs_are_nan_not_gp_safe_constants(self):
        """刻意与 gp_mine_daily._safe_* 分歧: 退化输入返回 NaN 而不是 1.0/0.0。

        理由: evidence 阶段要用覆盖率/唯一取值数判断因子是否退化, 伪造出的常量会把
        "算不出来"掩盖成"算出来了"。
        """
        x = np.array([1.0, -1.0, 0.0, 2.0])
        assert np.isnan(fh.evaluate_expression("div(x, 0)", {"x": x})).all()
        inv = fh.evaluate_expression("inv(x)", {"x": x})
        assert np.isnan(inv[2]) and inv[0] == pytest.approx(1.0)
        log = fh.evaluate_expression("log(x)", {"x": x})
        assert np.isnan(log[2])

    def test_evaluate_accepts_hypothesis_or_expression(self, panel):
        h = mk()
        assert np.allclose(fh.evaluate_expression(h, panel),
                           fh.evaluate_expression("ret_20", panel))

    def test_missing_field_raises(self):
        with pytest.raises(fh.MissingFieldError):
            fh.evaluate_expression("ret_20", {"ret_5": np.zeros(3)})

    def test_constant_expression_evaluates_to_scalar(self):
        v = fh.evaluate_expression("1.0 + 2.0", {})
        assert np.asarray(v).ndim == 0 and float(v) == pytest.approx(3.0)

    def test_pandas_objects_satisfy_data_contract(self):
        """数据契约不依赖 pandas, 但 pandas 对象必须能直接用。"""
        s = pd.Series(np.arange(1.0, 13.0))
        assert fh.spearman_rank_ic(s, s * 2) == pytest.approx(1.0)
        df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [3.0, 2.0, 1.0]})
        assert fh.evaluate_expression("cs_zscore(a)", {"a": df}).shape == (3, 2)

    def test_function_arity_registry_covers_gp_mine_daily_operators(self):
        """算子名必须是 gp_mine_daily.GP_FUNCTIONS 的超集 (接口兼容的最低要求)。"""
        gp_ops = {"add", "sub", "mul", "div", "neg", "abs", "sqrt", "log", "inv",
                  "cs_rank", "cs_demean", "cs_zscore", "square", "cube", "sign", "clip"}
        assert gp_ops <= set(fh.FUNCTION_ARITY)
        assert set(fh.TS_FUNCTIONS) <= set(fh.FUNCTION_ARITY)


# ===========================================================================
# 2. 秩相关 (自己实现, 不依赖 scipy)
# ===========================================================================

class TestRankCorrelation:

    def test_rank_avg_1d_basic(self):
        assert np.allclose(fh._rank_avg_1d([30, 10, 20]), [3, 1, 2])

    def test_rank_avg_1d_average_ties(self):
        assert np.allclose(fh._rank_avg_1d([5, 5, 1]), [2.5, 2.5, 1.0])

    def test_rank_avg_1d_keeps_nan(self):
        r = fh._rank_avg_1d([1.0, np.nan, 3.0])
        assert np.isnan(r[1]) and r[0] == pytest.approx(1.0) and r[2] == pytest.approx(2.0)

    def test_perfect_and_reversed(self):
        assert fh.spearman_rank_ic([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
        assert fh.spearman_rank_ic([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_monotone_nonlinear_is_still_one(self):
        """秩相关只看单调性: 指数关系也应是 1.0 (这正是选它而不是 Pearson 的原因)。"""
        assert fh.spearman_rank_ic([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) == pytest.approx(1.0)

    def test_pairs_with_nan_dropped(self):
        f = [1.0, np.nan, 3.0, 4.0, 5.0]
        y = [1.0, 99.0, 3.0, 4.0, 5.0]
        assert fh.spearman_rank_ic(f, y) == pytest.approx(1.0)

    def test_undefined_cases_return_none_not_zero(self):
        assert fh.spearman_rank_ic([1, 1, 1, 1], [1, 2, 3, 4]) is None     # 因子无变异
        assert fh.spearman_rank_ic([1, 1, 1, 1], [1, 1, 1, 1]) is None
        assert fh.spearman_rank_ic([1, 2], [1, 2]) is None                 # 配对太少
        assert fh.spearman_rank_ic([1, 2, 3], [1, 2]) is None              # 长度不一致

    def test_regression_rank_mapping_is_restored_to_original_order(self):
        """回归用例: 秩必须映射回原始顺序。

        曾经漏掉 `ranks[order] = ...` 这一步, 秩退化成 1..n 的等差数列, 于是**任意**
        一对变量的秩相关都是 ±1 —— 这种 bug 会让 evidence 阶段全盘失真。
        """
        rng = np.random.default_rng(1)
        r = fh.spearman_rank_ic(rng.normal(size=200), rng.normal(size=200))
        assert r is not None and abs(r) < 0.3


# ===========================================================================
# 3. hypothesis_id 的身份稳定性
# ===========================================================================

class TestHypothesisId:

    def test_same_thesis_same_id_idempotent(self):
        assert mk().hypothesis_id == mk().hypothesis_id

    def test_different_thesis_different_id(self):
        assert mk().hypothesis_id != mk(thesis=THESIS + "(另一套说法)").hypothesis_id

    def test_same_thesis_different_expression_different_id(self):
        a = mk(expression="ret_20")
        b = mk(expression="neg(ret_20)")
        assert a.thesis == b.thesis and a.hypothesis_id != b.hypothesis_id

    def test_same_thesis_different_direction_different_id(self):
        assert mk(direction=1).hypothesis_id != mk(direction=-1).hypothesis_id

    def test_id_is_stable_under_cosmetic_whitespace_and_case(self):
        a = fh.Hypothesis(thesis="Low  VOL  anomaly", mechanism=MECH,
                          expression="ret_20", direction=1)
        b = fh.Hypothesis(thesis=" low vol anomaly ", mechanism=MECH,
                          expression="ret_20", direction=1)
        assert a.hypothesis_id == b.hypothesis_id

    def test_id_is_stable_under_syntactically_equivalent_expressions(self):
        a = mk(expression="ret_20 * -1")
        b = mk(expression="mul(ret_20, -1)")
        assert a.expression == b.expression == "mul(ret_20, -1)"
        assert a.hypothesis_id == b.hypothesis_id

    def test_id_ignores_status_evidence_source_and_field_order(self):
        a = mk(required_fields=["ret_20", "ret_5"])
        b = mk(required_fields=["ret_5", "ret_20"], source="llm")
        b.status = "verified"
        b.add_evidence("lint", {"ok": True})
        assert a.hypothesis_id == b.hypothesis_id

    def test_id_format(self):
        hid = mk().hypothesis_id
        assert hid.startswith("hp_") and len(hid) == 19
        assert re.fullmatch(r"hp_[0-9a-f]{16}", hid)

    def test_to_dict_from_dict_round_trip_keeps_id(self):
        h = mk()
        back = fh.Hypothesis.from_dict(h.to_dict())
        assert back.to_dict() == h.to_dict()
        assert back.hypothesis_id == h.hypothesis_id

    def test_defaults(self):
        h = fh.Hypothesis(thesis=THESIS, mechanism=MECH, expression="ret_20", direction=1)
        assert h.source == "template" and h.status == "proposed"
        assert h.evidence == [] and h.created_at and h.required_fields == []

    def test_unknown_source_and_status_fall_back_to_safe_defaults(self):
        h = mk(source="外星人", status="也许")
        assert h.source == "template" and h.status == "proposed"

    def test_direction_normalization(self):
        assert fh.normalize_direction("positive") == 1
        assert fh.normalize_direction("+1") == 1
        assert fh.normalize_direction(1) == 1
        assert fh.normalize_direction("negative") == -1
        assert fh.normalize_direction("-1") == -1
        assert fh.normalize_direction(-1) == -1
        assert fh.normalize_direction("maybe") == 0
        assert fh.normalize_direction(0) == 0
        assert fh.normalize_direction(2) == 0            # 只认 ±1, 不"就近猜"
        assert fh.normalize_direction(-0.5) == 0
        assert fh.normalize_direction(None) == 0
        assert fh.normalize_direction(True) == 0          # 布尔不算方向

    def test_evidence_appends_and_stamps(self):
        h = mk()
        rec = h.add_evidence("lint", {"ok": True})
        assert h.evidence and h.evidence[0]["stage"] == "lint" and rec["ok"] is True
        h.add_evidence("note", "非 Mapping 载荷")
        assert h.evidence[1]["payload"] == "非 Mapping 载荷"

    def test_summary_and_as_hypothesis_accept_dict(self):
        h = mk()
        assert h.summary()["hypothesis_id"] == h.hypothesis_id
        assert fh._as_hypothesis(h.to_dict()).hypothesis_id == h.hypothesis_id
        with pytest.raises(TypeError):
            fh._as_hypothesis(123)


# ===========================================================================
# 4. lint: 经济逻辑与可计算性前置检查
# ===========================================================================

class TestLint:

    def test_healthy_hypothesis_passes(self):
        r = fh.lint_hypothesis(mk(), AVAIL)
        assert r["ok"] is True and r["reasons"] == [] and r["warnings"] == []

    def test_accepts_dict_input(self):
        assert fh.lint_hypothesis(mk().to_dict(), AVAIL)["ok"] is True

    def test_missing_field_is_rejected_and_named(self):
        r = fh.lint_hypothesis(mk(expression="roe", required_fields=["roe"]), AVAIL)
        assert r["ok"] is False
        assert any("missing_fields" in x and "roe" in x for x in r["reasons"])

    def test_unknown_field_in_expression_is_rejected(self):
        h = mk(expression="mul(ret_20, roe)", required_fields=["ret_20"])
        r = fh.lint_hypothesis(h, AVAIL)
        assert r["ok"] is False
        assert any("unknown_field_in_expr" in x and "roe" in x for x in r["reasons"])

    def test_undeclared_but_available_field_is_only_a_warning(self):
        h = mk(expression="mul(ret_20, ret_5)", required_fields=["ret_20"])
        r = fh.lint_hypothesis(h, AVAIL)
        assert r["ok"] is True
        assert any("undeclared_field" in x and "ret_5" in x for x in r["warnings"])

    @pytest.mark.parametrize("expr", [
        "ret_20.shift(-1)",
        "neg(shift(-2))",
        "pct_change(-2)",
        "diff(-1)",
    ])
    def test_negative_shift_family_is_rejected(self, expr):
        r = fh.lint_hypothesis(mk(expression=expr, required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("future_function" in x for x in r["reasons"])

    def test_future_prefixed_field_is_rejected(self):
        h = mk(expression="future_ret_20", required_fields=["future_ret_20"])
        r = fh.lint_hypothesis(h, AVAIL + ["future_ret_20"])
        assert r["ok"] is False
        assert any("future_function" in x and "future_keyword" in x for x in r["reasons"])

    def test_next_prefixed_field_is_rejected(self):
        h = mk(expression="next_close", required_fields=["next_close"])
        r = fh.lint_hypothesis(h, AVAIL + ["next_close"])
        assert r["ok"] is False
        assert any("future_function" in x and "future_next" in x for x in r["reasons"])

    def test_t_plus_field_is_rejected(self):
        h = mk(expression="t+1_ret", required_fields=["t+1_ret"])
        r = fh.lint_hypothesis(h, AVAIL + ["t+1_ret"])
        assert r["ok"] is False
        assert any("future_function" in x and "future_t_plus" in x for x in r["reasons"])

    def test_iloc_forward_index_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="sma_5.iloc[i + 1]", required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("future_function" in x and "future_iloc" in x for x in r["reasons"])

    def test_backward_fill_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="ret_20.bfill()", required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("future_function" in x for x in r["reasons"])

    def test_negative_ts_window_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="ts_delta(ret_20, -1)"), AVAIL)
        assert r["ok"] is False
        assert any("future_function" in x for x in r["reasons"])

    def test_non_literal_ts_window_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="ts_mean(ret_20, w)"), AVAIL)
        assert r["ok"] is False
        assert any("bad_window" in x for x in r["reasons"])

    def test_centered_window_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="ts_mean(ret_20, 5, center=True)",
                                  required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("future_function" in x for x in r["reasons"])

    def test_constant_expression_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="1.0 + 2.0", required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("constant_expression" in x for x in r["reasons"])

    def test_empty_expression_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="", required_fields=[]), AVAIL)
        assert r["ok"] is False
        assert any("empty_expression" in x for x in r["reasons"])

    def test_empty_thesis_is_rejected(self):
        r = fh.lint_hypothesis(mk(thesis=""), AVAIL)
        assert r["ok"] is False
        assert any("empty_thesis" in x for x in r["reasons"])

    def test_short_thesis_is_rejected_with_threshold(self):
        r = fh.lint_hypothesis(mk(thesis="涨"), AVAIL)
        assert r["ok"] is False
        assert any("short_thesis" in x for x in r["reasons"])

    def test_long_thesis_is_only_a_warning(self):
        r = fh.lint_hypothesis(mk(thesis="啊" * 700), AVAIL)
        assert r["ok"] is True
        assert any("long_thesis" in x for x in r["warnings"])

    def test_empty_mechanism_is_rejected(self):
        r = fh.lint_hypothesis(mk(mechanism=""), AVAIL)
        assert r["ok"] is False
        assert any("empty_mechanism" in x for x in r["reasons"])

    def test_short_mechanism_is_rejected(self):
        r = fh.lint_hypothesis(mk(mechanism="因为涨"), AVAIL)
        assert r["ok"] is False
        assert any("short_mechanism" in x for x in r["reasons"])

    @pytest.mark.parametrize("bad", [0, None, "maybe", 2])
    def test_bad_direction_is_rejected(self, bad):
        r = fh.lint_hypothesis(mk(direction=bad), AVAIL)
        assert r["ok"] is False
        assert any("bad_direction" in x for x in r["reasons"])

    def test_syntax_error_is_rejected(self):
        r = fh.lint_hypothesis(mk(expression="add(ret_20, )"), AVAIL)
        assert r["ok"] is False
        assert any("syntax_error" in x for x in r["reasons"])

    def test_available_fields_none_gives_warning_not_rejection(self):
        r = fh.lint_hypothesis(mk(), None)
        assert r["ok"] is True
        assert any("available_fields_not_provided" in x for x in r["warnings"])

    def test_thresholds_are_parameterized(self):
        h = mk(thesis="短命题但不算空")
        assert fh.lint_hypothesis(h, AVAIL)["ok"] is False
        assert fh.lint_hypothesis(h, AVAIL, min_thesis_chars=3)["ok"] is True

    def test_return_contract(self):
        r = fh.lint_hypothesis(mk(), AVAIL)
        assert set(["ok", "reasons", "warnings"]) <= set(r)
        assert isinstance(r["ok"], bool) and isinstance(r["reasons"], list)
        assert r["expression_canonical"] == "ret_20"
        assert r["fields"] == ["ret_20"]

    def test_multiple_problems_are_all_reported(self):
        h = mk(thesis="", mechanism="", expression="future_x", required_fields=["roe"],
               direction=0)
        r = fh.lint_hypothesis(h, AVAIL)
        joined = " ".join(r["reasons"])
        for code in ("empty_thesis", "empty_mechanism", "missing_fields",
                     "future_function", "bad_direction"):
            assert code in joined


# ===========================================================================
# 5. evidence: 假设级证据
# ===========================================================================

class TestEvidenceCheck:

    def test_healthy_factor_passes_all_gates(self, panel):
        ev = fh.evidence_check(mk(), panel)
        assert ev["ok"] is True and ev["reasons"] == []
        assert ev["direction_ok"] is True and ev["sign_ok"] is True
        assert ev["rank_ic"] == pytest.approx(0.88, abs=0.05)
        assert ev["sign_consistency"] == pytest.approx(1.0)
        assert ev["coverage"] == pytest.approx(1.0)
        assert ev["n_obs"] == 1200

    def test_return_contract_exact_keys(self, panel):
        ev = fh.evidence_check(mk(), panel)
        for k in ("ok", "direction_ok", "rank_ic", "sign_consistency",
                  "coverage", "n_obs", "reasons"):
            assert k in ev, f"evidence_check 缺少契约字段 {k}"
        assert isinstance(ev["ok"], bool) and isinstance(ev["direction_ok"], bool)
        assert isinstance(ev["n_obs"], int) and isinstance(ev["reasons"], list)
        assert len(ev["segment_ics"]) == 5 and ev["n_segments"] == 5

    def test_direction_correct_but_single_segment_is_rejected(self, panel):
        """方向本身对, 但只有 1 段样本 —— "稳定性"无从谈起, 必须拒。"""
        ev = fh.evidence_check(mk(), panel, n_segments=1)
        assert ev["ok"] is False
        assert ev["direction_ok"] is True          # 方向确实是对的
        assert ev["sign_consistency"] == pytest.approx(1.0)
        assert any("insufficient_segments" in x for x in ev["reasons"])

    def test_single_segment_passes_when_min_segments_lowered(self, panel):
        ev = fh.evidence_check(mk(), panel, n_segments=1, min_segments=1)
        assert ev["ok"] is True

    def test_full_sample_direction_ok_but_segments_unstable(self, unstable_panel):
        """全样本方向对, 但 5 段里 3 段反号 => 拒 (只看全样本一个数就会放过去)。"""
        ev = fh.evidence_check(unstable_hyp(), unstable_panel)
        assert ev["ok"] is False
        assert ev["direction_ok"] is True and ev["rank_ic"] > 0
        assert ev["sign_consistency"] == pytest.approx(0.4)
        assert any("unstable_sign" in x for x in ev["reasons"])
        assert len(ev["reasons"]) == 1, "应当且仅当因分段不稳定被拒"

    def test_unstable_case_passes_when_threshold_lowered(self, unstable_panel):
        ev = fh.evidence_check(unstable_hyp(), unstable_panel, min_sign_consistency=0.0)
        assert ev["ok"] is True

    def test_sign_consistency_counts_only_effective_segments(self, unstable_panel):
        ev = fh.evidence_check(unstable_hyp(), unstable_panel, segment_min_obs=200)
        assert ev["n_segments_effective"] < 5
        assert any("insufficient_segments" in x for x in ev["reasons"])

    def test_low_coverage_is_rejected(self, panel):
        sparse = panel["ret_20"].copy()
        flat = sparse.reshape(-1)
        flat[: int(flat.size * 0.85)] = np.nan
        ev = fh.evidence_check(mk(expression="sparse", required_fields=["sparse"]),
                               {**panel, "sparse": sparse})
        assert ev["ok"] is False
        assert ev["coverage"] == pytest.approx(0.15, abs=0.02)
        assert any("low_coverage" in x for x in ev["reasons"])

    def test_near_constant_factor_is_rejected(self, panel):
        rng = np.random.default_rng(11)
        nearly = np.where(rng.random((60, 20)) > 0.5, 1.0, 1.0 + 1e-9)
        ev = fh.evidence_check(mk(expression="nc", required_fields=["nc"]),
                               {**panel, "nc": nearly})
        assert ev["ok"] is False
        assert ev["n_unique"] <= 10
        assert any("low_cardinality" in x for x in ev["reasons"])

    def test_zero_variance_factor_is_rejected_and_ic_undefined(self, panel):
        ev = fh.evidence_check(mk(expression="const_f", required_fields=["const_f"]),
                               {**panel, "const_f": np.ones((60, 20))})
        assert ev["ok"] is False
        assert ev["rank_ic"] is None, "无变异的因子, 秩相关无法定义 (None 而非 0.0)"
        joined = " ".join(ev["reasons"])
        assert "zero_variance" in joined and "rank_ic_undefined" in joined

    def test_direction_mismatch_is_rejected(self, panel):
        ev = fh.evidence_check(mk(expression="neg(ret_20)", direction=1), panel)
        assert ev["ok"] is False and ev["direction_ok"] is False
        assert ev["rank_ic"] < 0
        assert any("direction_mismatch" in x for x in ev["reasons"])

    def test_negative_direction_factor_with_negative_direction_passes(self, panel):
        ev = fh.evidence_check(mk(expression="neg(ret_20)", direction=-1), panel)
        assert ev["ok"] is True and ev["rank_ic"] < 0

    def test_insufficient_observations_is_rejected(self):
        tiny = {"ret_20": np.arange(20.0), fh.DEFAULT_TARGET_KEY: np.arange(20.0)}
        ev = fh.evidence_check(mk(), tiny)
        assert ev["ok"] is False
        assert any("insufficient_obs" in x for x in ev["reasons"])

    def test_target_nan_pairs_are_dropped_and_counted(self, panel):
        target = panel[fh.DEFAULT_TARGET_KEY].copy()
        target[-10:, :] = np.nan                      # 200 个缺失标签
        ev = fh.evidence_check(mk(), {**panel, fh.DEFAULT_TARGET_KEY: target})
        assert ev["n_obs"] == 1000
        assert ev["coverage"] == pytest.approx(1000 / 1200)

    def test_one_dimensional_panel_works(self):
        rng = np.random.default_rng(5)
        f = rng.normal(size=400)
        y = 0.7 * f + rng.normal(size=400) * 0.5
        ev = fh.evidence_check(mk(expression="f", required_fields=["f"]),
                               {"f": f, fh.DEFAULT_TARGET_KEY: y})
        assert ev["ok"] is True and ev["n_obs"] == 400

    def test_target_key_is_configurable(self, panel):
        data = {k: v for k, v in panel.items() if k != fh.DEFAULT_TARGET_KEY}
        data["fwd5"] = panel[fh.DEFAULT_TARGET_KEY]
        assert fh.evidence_check(mk(), data)["ok"] is False        # 默认键找不到目标
        ev = fh.evidence_check(mk(), data, target_key="fwd5")
        assert ev["ok"] is True and ev["target_key"] == "fwd5"

    def test_shape_mismatch_is_reported(self):
        ev = fh.evidence_check(mk(), {"ret_20": np.arange(10.0),
                                      fh.DEFAULT_TARGET_KEY: np.arange(20.0)})
        assert ev["ok"] is False
        assert any("shape_mismatch" in x for x in ev["reasons"])

    def test_missing_target_is_reported(self):
        ev = fh.evidence_check(mk(), {"ret_20": np.arange(20.0)})
        assert ev["ok"] is False
        assert any("missing_target" in x for x in ev["reasons"])

    def test_expression_error_is_reported_not_raised(self, panel):
        ev = fh.evidence_check(mk(expression="mul(ret_20, roe)", required_fields=["ret_20"]),
                               panel)
        assert ev["ok"] is False
        assert any("expression_error" in x for x in ev["reasons"])

    def test_min_abs_rank_ic_is_enforced_and_parameterized(self, panel):
        assert fh.evidence_check(mk(), panel)["ok"] is True
        strict = fh.evidence_check(mk(), panel, min_abs_rank_ic=0.999)
        assert strict["ok"] is False
        assert any("weak_rank_ic" in x for x in strict["reasons"])

    def test_min_coverage_threshold_is_parameterized(self, panel):
        sparse = panel["ret_20"].copy()
        flat = sparse.reshape(-1)
        flat[: int(flat.size * 0.5)] = np.nan          # 覆盖率降到 0.5
        data = {**panel, "sparse": sparse}
        h = mk(expression="sparse", required_fields=["sparse"])
        assert fh.evidence_check(h, data)["ok"] is False            # 默认 0.6 -> 拒
        assert any("low_coverage" in x for x in fh.evidence_check(h, data)["reasons"])
        assert fh.evidence_check(h, data, min_coverage=0.4)["ok"] is True   # 显式放宽 -> 过

    def test_min_n_obs_threshold_is_parameterized(self):
        n = 100
        rng = np.random.default_rng(2)
        f = rng.normal(size=n)
        data = {"f": f, fh.DEFAULT_TARGET_KEY: 0.7 * f + rng.normal(size=n) * 0.5}
        h = mk(expression="f", required_fields=["f"],
               thesis="小样本下的动量假设 (用于测试样本量门槛)")
        strict = fh.evidence_check(h, data)
        assert strict["ok"] is False
        assert any("insufficient_obs" in x for x in strict["reasons"])
        loose = fh.evidence_check(h, data, min_n_obs=50, min_segments=1)
        assert loose["ok"] is True

    def test_segment_ics_are_reported_per_segment(self, panel):
        ev = fh.evidence_check(mk(), panel)
        ics = [x for x in ev["segment_ics"] if x is not None]
        assert len(ics) == 5 and all(x > 0 for x in ics)


# ===========================================================================
# 6. 流水线纪律: 没通过 lint/evidence 的假设绝不进入收益评估
# ===========================================================================

class TestPipelineDiscipline:

    def _counter(self):
        calls: list[str] = []

        def evaluator(h, data):
            calls.append(h.hypothesis_id)
            return {"ok": True, "ic_mean": 0.03}

        return calls, evaluator

    def test_lint_rejected_never_reaches_return_evaluator(self, panel):
        calls, evaluator = self._counter()
        bad = mk(expression="future_ret_20", required_fields=["future_ret_20"],
                 thesis="用未来的收益率预测未来收益 (故意写错, 必须被 lint 拦下)")
        r = fh.validate_batch([bad], {**panel, "future_ret_20": panel["ret_20"]},
                              AVAIL + ["future_ret_20"], return_evaluator=evaluator)
        assert calls == [], "lint 未通过的假设竟然调用了收益评估回调"
        assert r["return_eval_calls"] == 0
        assert r["lint_rejected"] == 1 and r["verified"] == 0 and r["falsified"] == 0
        assert r["records"][0]["stage"] == "lint_rejected"
        assert r["records"][0]["evidence"] is None

    def test_evidence_rejected_never_reaches_return_evaluator(self, panel):
        calls, evaluator = self._counter()
        wrong = mk(expression="neg(ret_20)", direction=1)      # 方向与数据相反
        r = fh.validate_batch([wrong], panel, AVAIL, return_evaluator=evaluator)
        assert calls == []
        assert r["return_eval_calls"] == 0
        assert r["evidence_rejected"] == 1 and r["verified"] == 0
        assert r["records"][0]["stage"] == "evidence_rejected"

    def test_passing_hypothesis_is_evaluated_exactly_once(self, panel):
        calls, evaluator = self._counter()
        good = mk()
        r = fh.validate_batch([good], panel, AVAIL, return_evaluator=evaluator)
        assert calls == [good.hypothesis_id]
        assert r["verified"] == 1 and r["return_eval_calls"] == 1
        assert good.status == "verified"
        assert r["records"][0]["return_eval"]["ic_mean"] == pytest.approx(0.03)

    def test_mixed_batch_counts_and_discipline(self, panel):
        calls, evaluator = self._counter()
        lint_bad = mk(expression="future_ret_20", required_fields=["future_ret_20"],
                      thesis="引用未来字段的假设 (必须被 lint 拦下)")
        ev_bad = mk(expression="neg(ret_20)", direction=1,
                    thesis="方向标注写反的假设 (必须被 evidence 拦下)")
        good = mk()
        data = {**panel, "future_ret_20": panel["ret_20"]}
        r = fh.validate_batch([lint_bad, ev_bad, good], data,
                              AVAIL + ["future_ret_20"], return_evaluator=evaluator)
        assert len(calls) == 1 and calls == [good.hypothesis_id]
        assert r["proposed"] == 3
        assert r["lint_rejected"] == 1 and r["evidence_rejected"] == 1
        assert r["verified"] == 1 and r["falsified"] == 0
        assert r["return_eval_calls"] == r["evidence_passed"] == 1
        assert (r["proposed"] == r["lint_rejected"] + r["evidence_rejected"]
                + r["verified"] + r["falsified"])
        stages = [rec["stage"] for rec in r["records"]]
        assert stages == ["lint_rejected", "evidence_rejected", "return_evaluated"]

    def test_only_evidence_passers_are_counted_as_candidates_for_return(self, panel):
        calls, evaluator = self._counter()
        early = mk(expression="future_x", required_fields=["future_x"],
                   thesis="又一条未来函数假设 (必须被 lint 拦下)")
        late = mk(expression="neg(ret_20)", direction=1,
                  thesis="又一条方向写反的假设 (必须被 evidence 拦下)")
        ok1, ok2 = mk(), mk(expression="neg(ret_20)", direction=-1,
                            thesis="反向动量假设: 涨幅高的股票未来收益更低")
        data = {**panel, "future_x": panel["ret_20"]}
        r = fh.validate_batch([early, late, ok1, ok2], data,
                              AVAIL + ["future_x"], return_evaluator=evaluator)
        assert r["evidence_passed"] == 2 and len(calls) == 2
        assert r["lint_rejected"] == 1 and r["evidence_rejected"] == 1

    def test_falsified_when_return_evaluator_says_no(self, panel):
        calls, _ = self._counter()
        h = mk()
        r = fh.validate_batch([h], panel, AVAIL,
                              return_evaluator=lambda hyp, d: {"ok": False, "ic_mean": 0.0})
        assert r["falsified"] == 1 and r["verified"] == 0
        assert h.status == "falsified"
        assert calls == []

    def test_verdict_based_pass_is_understood(self, panel):
        r = fh.validate_batch([mk()], panel, AVAIL,
                              return_evaluator=lambda h, d: {"verdict": "ADOPT"})
        assert r["verified"] == 1
        r2 = fh.validate_batch([mk(expression="neg(ret_20)", direction=-1,
                                   thesis="反向动量: 短期涨幅高者未来收益低")],
                               panel, AVAIL,
                               return_evaluator=lambda h, d: {"verdict": "REJECT"})
        assert r2["falsified"] == 1

    def test_garbage_return_value_is_fail_closed(self, panel):
        r = fh.validate_batch([mk()], panel, AVAIL,
                              return_evaluator=lambda h, d: "看起来不错")
        assert r["falsified"] == 1 and r["verified"] == 0

    def test_raising_return_evaluator_does_not_break_the_batch(self, panel):
        def boom(h, d):
            raise RuntimeError("模拟收益评估故障")

        r = fh.validate_batch([mk()], panel, AVAIL, return_evaluator=boom)
        assert r["falsified"] == 1
        assert "RuntimeError" in r["records"][0]["return_eval"]["error"]

    def test_without_return_evaluator_verified_means_evidence_only(self, panel):
        r = fh.validate_batch([mk()], panel, AVAIL)
        assert r["return_eval_calls"] == 0
        assert r["verified"] == 1
        assert r["records"][0]["return_eval"] is None
        assert r["records"][0]["stage"] == "evidence_passed"

    def test_available_fields_derived_from_data_when_omitted(self, panel):
        r = fh.validate_batch([mk()], panel)
        assert r["lint_rejected"] == 0 and r["verified"] == 1

    def test_ledger_records_every_stage(self, panel, tmpdir_ws):
        ledger = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        lint_bad = mk(expression="future_x", required_fields=["future_x"],
                      thesis="未来字段假设 (必须被 lint 拦下)")
        good = mk()
        r = fh.validate_batch([lint_bad, good], {**panel, "future_x": panel["ret_20"]},
                              AVAIL + ["future_x"], ledger=ledger,
                              return_evaluator=lambda h, d: {"ok": True})
        st = ledger.stats()
        assert st["total"] == 2 + 2 + 1 + 1         # proposed×2 + lint×2 + evidence + return
        assert st["by_stage"]["proposed"] == 2
        assert st["by_stage"]["lint"] == 2
        assert st["by_stage"]["evidence"] == 1
        assert st["by_stage"]["return"] == 1
        assert st["unique_hypotheses"] == 2
        assert r["verified"] == 1

    def test_empty_batch(self, panel):
        r = fh.validate_batch([], panel, AVAIL)
        assert r["proposed"] == 0 and r["records"] == []
        assert r["return_eval_calls"] == 0

    def test_hypotheses_accumulate_evidence_in_place(self, panel):
        h = mk()
        fh.validate_batch([h], panel, AVAIL, return_evaluator=lambda x, d: {"ok": True})
        stages = [e["stage"] for e in h.evidence]
        assert stages == ["lint", "evidence", "return"]


# ===========================================================================
# 7. 账本
# ===========================================================================

class TestLedger:

    def test_default_path_points_at_repo_data(self):
        lg = fh.HypothesisLedger()
        assert os.path.isabs(lg.path)
        assert lg.path.replace("\\", "/").endswith("/data/factor_hypotheses.jsonl")

    def test_append_read_all_round_trip(self, tmpdir_ws):
        lg = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        h = mk()
        assert lg.append(h, "lint", {"ok": True, "reasons": []}) is True
        recs = lg.read_all()
        assert len(recs) == 1
        r = recs[0]
        assert r["hypothesis_id"] == h.hypothesis_id
        assert r["thesis"] == h.thesis and r["mechanism"] == h.mechanism
        assert r["expression"] == "ret_20" and r["direction"] == 1
        assert r["required_fields"] == ["ret_20"] and r["source"] == "human"
        assert r["stage"] == "lint" and r["payload"]["ok"] is True
        assert r["status"] == "proposed" and r["created_at"] and r["ts"]
        assert r["ledger_version"] == fh.LEDGER_VERSION

    def test_multi_stage_append_keeps_order_and_full_metadata(self, tmpdir_ws):
        lg = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        h = mk()
        for stage in ("proposed", "lint", "evidence", "return"):
            assert lg.append(h, stage, {"stage_marker": stage}) is True
        recs = lg.read_all()
        assert [r["stage"] for r in recs] == ["proposed", "lint", "evidence", "return"]
        assert all(r["hypothesis_id"] == h.hypothesis_id for r in recs)
        assert all(r["payload"]["stage_marker"] == r["stage"] for r in recs)

    def test_append_accepts_dict_and_none_payload(self, tmpdir_ws):
        lg = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        assert lg.append(mk().to_dict(), "lint", None) is True
        assert lg.read_all()[0]["payload"] is None

    def test_bad_json_lines_do_not_break_read_all(self, tmpdir_ws):
        p = os.path.join(tmpdir_ws, "h.jsonl")
        lg = fh.HypothesisLedger(path=str(p))
        assert lg.append(mk(), "lint", {"ok": True}) is True
        with open(p, "a", encoding="utf-8") as f:
            f.write("{这不是合法 JSON\n")
            f.write("[1, 2, 3]\n")            # 合法 JSON 但不是对象
            f.write("\n")                      # 空行
            f.write("又一行垃圾\n")
        assert lg.append(mk(), "evidence", {"ok": False}) is True
        recs = lg.read_all()
        assert len(recs) == 2, "坏行必须被跳过, 好行必须读出来"
        assert lg.bad_lines == 3

    def test_write_failure_returns_false_and_does_not_raise(self, tmpdir_ws):
        target_dir = os.path.join(tmpdir_ws, "a_directory")
        os.makedirs(target_dir, exist_ok=True)
        lg = fh.HypothesisLedger(path=str(target_dir))       # 打开目录必然失败
        assert lg.append(mk(), "lint", {}) is False
        assert lg.write_failures == 1 and lg.last_error

    def test_write_failure_when_parent_is_a_file(self, tmpdir_ws):
        blocker = os.path.join(tmpdir_ws, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("我是文件, 不是目录")
        lg = fh.HypothesisLedger(path=os.path.join(blocker, "sub", "h.jsonl"))
        assert lg.append(mk(), "lint", {}) is False
        assert lg.last_error

    def test_unserializable_payload_still_lands(self, tmpdir_ws):
        """payload 里混进不可序列化对象时用 default=str 兜底, 不能因为留痕失败丢记录。"""
        lg = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        assert lg.append(mk(), "evidence", {"obj": object()}) is True
        assert "object object" in lg.read_all()[0]["payload"]["obj"]

    def test_read_all_missing_file_returns_empty(self, tmpdir_ws):
        lg = fh.HypothesisLedger(path=os.path.join(tmpdir_ws, "nope.jsonl"))
        assert lg.read_all() == [] and lg.bad_lines == 0

    def test_stats(self, tmpdir_ws):
        lg = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        a, b = mk(), mk(expression="neg(ret_20)", direction=-1,
                        thesis="反向动量: 短期涨幅高者未来收益更低", source="llm")
        lg.append(a, "lint", {"ok": True})
        lg.append(a, "evidence", {"ok": True})
        lg.append(b, "lint", {"ok": False})
        st = lg.stats()
        assert st["total"] == 3 and st["unique_hypotheses"] == 2
        assert st["by_stage"] == {"lint": 2, "evidence": 1}
        assert st["by_source"] == {"human": 2, "llm": 1}
        assert st["exists"] is True and st["first_ts"] and st["last_ts"]
        assert st["path"].endswith("h.jsonl")

    def test_stats_on_empty_ledger(self, tmpdir_ws):
        st = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl"))).stats()
        assert st["total"] == 0 and st["exists"] is False
        assert st["first_ts"] is None and st["by_stage"] == {}


# ===========================================================================
# 8. LLM 提案 (注入式 llm_call, 不联网)
# ===========================================================================

class TestProposeFromLLM:

    def test_prompt_requires_the_five_fields_and_n(self):
        p = fh.render_prompt({"available_fields": AVAIL}, 4)
        for key in ("thesis", "mechanism", "direction", "expression", "required_fields"):
            assert key in p
        assert "4" in p
        assert "ret_20" in p, "context 必须真的进了 prompt"
        assert "JSON" in p
        assert "未来函数" in p, "prompt 里必须写明严禁未来函数"

    def test_render_prompt_accepts_str_context_and_custom_templates(self):
        p = fh.render_prompt("原始上下文文本", 2)
        assert "原始上下文文本" in p
        p2 = fh.render_prompt("ctx", 3, template=lambda c, n: f"N={n};C={c}")
        assert p2 == "N=3;C=ctx"
        p3 = fh.render_prompt("ctx", 3, template="只有 {n} 个占位")   # 无 {context}
        assert "只有 3 个占位" in p3 and "ctx" in p3

    def test_end_to_end_from_stub_llm(self):
        captured: list[str] = []
        hyps = fh.propose_from_llm(context={"available_fields": AVAIL},
                                   llm_call=stub_llm([VALID_ITEM], captured), n=1)
        assert len(hyps) == 1
        h = hyps[0]
        assert isinstance(h, fh.Hypothesis) and h.source == "llm"
        assert h.thesis == THESIS and h.expression == "ret_20"
        assert h.direction == 1                       # "positive" -> +1
        assert h.required_fields == ["ret_20"]
        assert h.status == "proposed"
        assert captured and isinstance(captured[0], str) and len(captured[0]) > 50

    def test_direction_variants_from_llm(self):
        items = [dict(VALID_ITEM, thesis=THESIS + " A", direction="-1"),
                 dict(VALID_ITEM, thesis=THESIS + " B", direction=-1),
                 dict(VALID_ITEM, thesis=THESIS + " C", direction="negative")]
        hyps = fh.propose_from_llm(context={}, llm_call=stub_llm(items), n=3)
        assert [h.direction for h in hyps] == [-1, -1, -1]

    def test_fenced_markdown_json_is_parsed(self):
        raw = "```json\n" + llm_payload([VALID_ITEM]) + "\n```"
        hyps = fh.propose_from_llm(context={}, llm_call=stub_llm(raw), n=1)
        assert len(hyps) == 1

    def test_json_wrapped_in_prose_is_parsed(self):
        raw = "好的, 这是我的建议:\n" + llm_payload([VALID_ITEM]) + "\n希望有帮助。"
        assert len(fh.propose_from_llm(context={}, llm_call=stub_llm(raw), n=1)) == 1

    def test_trailing_comma_json_is_repaired(self):
        raw = '{"hypotheses": [{"thesis": "%s", "mechanism": "%s", "direction": 1, ' \
              '"expression": "ret_20", "required_fields": ["ret_20"],},],}' % (THESIS, MECH)
        assert len(fh.propose_from_llm(context={}, llm_call=stub_llm(raw), n=1)) == 1

    def test_bare_list_and_single_object_shapes(self):
        assert len(fh.propose_from_llm(context={}, llm_call=stub_llm([VALID_ITEM]), n=1)) == 1
        single = fh.propose_from_llm(context={}, llm_call=stub_llm(json.dumps(
            VALID_ITEM, ensure_ascii=False)), n=1)
        assert len(single) == 1

    def test_malformed_json_raises_loudly(self):
        with pytest.raises(fh.HypothesisParseError) as e:
            fh.propose_from_llm(context={}, llm_call=stub_llm("我觉得反转因子不错"), n=3)
        assert e.value.raw
        assert e.value.errors and e.value.errors[0]["kind"] == "invalid_json"

    def test_truncated_json_raises_loudly(self):
        with pytest.raises(fh.HypothesisParseError):
            fh.propose_from_llm(context={}, llm_call=stub_llm('{"hypotheses": [{"thesis": "x"'),
                               n=1)

    def test_empty_hypotheses_list_raises_not_silent_empty(self):
        """**从不静默返回 []**: 空列表会让上游以为"模型今天没想法"。"""
        with pytest.raises(fh.HypothesisParseError):
            fh.propose_from_llm(context={}, llm_call=stub_llm({"hypotheses": []}), n=3)

    def test_all_items_invalid_raises(self):
        bad = [{"mechanism": MECH, "expression": "ret_20"},          # 缺 thesis
               {"thesis": THESIS, "direction": 1}]                   # 缺 expression
        with pytest.raises(fh.HypothesisParseError) as e:
            fh.propose_from_llm(context={}, llm_call=stub_llm(bad), n=2)
        kinds = [x["kind"] for x in e.value.errors]
        assert "missing_thesis" in kinds and "missing_expression" in kinds

    def test_non_string_llm_return_raises(self):
        with pytest.raises(fh.HypothesisParseError):
            fh.propose_from_llm(context={}, llm_call=lambda p: None)
        with pytest.raises(fh.HypothesisParseError):
            fh.propose_from_llm(context={}, llm_call=lambda p: "   ")

    def test_non_callable_llm_call_raises_type_error(self):
        with pytest.raises(TypeError):
            fh.propose_from_llm(context={}, llm_call="不是回调")

    def test_partial_invalid_items_are_kept_and_reported(self):
        raw = llm_payload([VALID_ITEM,
                           {"mechanism": MECH, "expression": "ret_20"},     # 缺 thesis
                           "我是字符串不是对象",
                           dict(VALID_ITEM, thesis=THESIS + " B")])
        parsed = fh.parse_llm_output(raw)
        assert len(parsed["hypotheses"]) == 2
        assert parsed["found"] == 4
        kinds = [x["kind"] for x in parsed["errors"]]
        assert "missing_thesis" in kinds and "not_object" in kinds
        hyps = fh.propose_from_llm(context={}, llm_call=stub_llm(raw), n=4)
        assert len(hyps) == 2, "有合法条目时不应整体抛错"

    def test_field_aliases_and_inferred_required_fields(self):
        item = {"hypothesis": THESIS, "rationale": MECH, "expected_ic_sign": "negative",
                "expr": "neg(ret_20)"}
        parsed = fh.parse_llm_output(llm_payload([item]))
        h = parsed["hypotheses"][0]
        assert h.thesis == THESIS and h.direction == -1
        assert h.required_fields == ["ret_20"], "缺 required_fields 时由表达式推导"
        assert parsed["warnings"][0]["kind"] == "inferred_required_fields"

    def test_parse_llm_output_reports_raw_length(self):
        parsed = fh.parse_llm_output(llm_payload([VALID_ITEM]))
        assert parsed["raw_len"] > 0 and parsed["errors"] == []

    def test_no_network_or_sdk_imports(self):
        """硬约束: 不直连 SDK / 不联网 / 不 import pandas/scipy。"""
        src = open(os.path.join(_SRC, "factor_hypothesis.py"), encoding="utf-8").read()
        forbidden = r"^\s*(?:import|from)\s+(scipy|pandas|requests|openai|httpx|aiohttp|urllib|socket)\b"
        assert re.search(forbidden, src, re.M) is None


# ===========================================================================
# 9. 端到端 + CLI
# ===========================================================================

class TestEndToEnd:

    def test_stub_llm_through_full_pipeline(self, panel, tmpdir_ws):
        items = [
            VALID_ITEM,
            dict(VALID_ITEM, thesis=THESIS + " (反向)", direction=-1,
                 expression="neg(ret_20)"),
            {"thesis": "未来函数样例: 用下一期收益率预测未来收益",
             "mechanism": MECH, "direction": 1, "expression": "ts_delta(ret_20, -1)",
             "required_fields": ["ret_20"]},
        ]
        hyps = fh.propose_from_llm(context={"available_fields": AVAIL},
                                   llm_call=stub_llm(items), n=3)
        assert len(hyps) == 3
        ledger = fh.HypothesisLedger(path=str(os.path.join(tmpdir_ws, "h.jsonl")))
        calls: list[str] = []

        def evaluator(h, data):
            calls.append(h.hypothesis_id)
            return {"ok": True, "ic_mean": 0.05, "verdict": "ADOPT"}

        r = fh.validate_batch(hyps, panel, AVAIL, ledger=ledger,
                              return_evaluator=evaluator,
                              min_n_obs=200, min_coverage=0.6, min_sign_consistency=0.6)
        assert r["proposed"] == 3
        assert r["lint_rejected"] == 1                  # 未来函数
        assert r["verified"] == 2 and r["falsified"] == 0
        assert len(calls) == r["evidence_passed"] == 2
        assert [h.status for h in hyps] == ["verified", "verified", "rejected"]
        st = ledger.stats()
        assert st["unique_hypotheses"] == 3 and st["total"] == 10
        # 账本里的状态必须是"该阶段结论落定后"的状态, 否则事后无法回答"哪一步拒的"
        assert st["by_status"]["verified"] == 2      # 两条走到收益阶段并 verified
        assert st["by_status"]["rejected"] == 1      # 一条在 lint 阶段就被拒
        assert st["by_stage"] == {"proposed": 3, "lint": 3, "evidence": 2, "return": 2}

    def test_cli_selftest_runs_green(self, capsys):
        rc = fh._main(["--selftest"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "自检结论: 通过" in out
        assert "流水线纪律" in out
        assert "lint_rejected=1" in out and "evidence_rejected=1" in out

    def test_cli_list_prints_stats(self, capsys, tmpdir_ws):
        p = os.path.join(tmpdir_ws, "h.jsonl")
        lg = fh.HypothesisLedger(path=str(p))
        lg.append(mk(), "lint", {"ok": True})
        rc = fh._main(["--list", "--path", str(p)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "总记录 1 条" in out and "lint" in out

    def test_cli_list_on_empty_ledger(self, capsys, tmpdir_ws):
        rc = fh._main(["--list", "--path", os.path.join(tmpdir_ws, "none.jsonl")])
        out = capsys.readouterr().out
        assert rc == 0 and "账本为空" in out

    def test_cli_without_command_prints_help(self, capsys):
        assert fh._main([]) == 1
        assert "usage" in capsys.readouterr().out.lower()

    def test_selftest_does_not_touch_repo_ledger(self, monkeypatch, tmpdir_ws, capsys):
        """自检不得污染 data/factor_hypotheses.jsonl (默认写仓库内的一次性临时目录)。"""
        guarded = os.path.join(tmpdir_ws, "should_not_be_used.jsonl")
        monkeypatch.setattr(fh, "DEFAULT_LEDGER_PATH", guarded)
        assert fh._main(["--selftest"]) == 0
        capsys.readouterr()
        assert not os.path.exists(guarded)

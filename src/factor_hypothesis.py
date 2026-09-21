# -*- coding: utf-8 -*-
# ============================================================
# factor_hypothesis.py -- 假设级证据驱动的因子挖掘 (FaVOR 思路)
#
# 为什么要有这个模块
# ------------------
# 本仓已有的 GP 因子挖掘链路 (factor_mine/gp_mine_daily.py) 是**收益导向**的:
# 以 RankIC 为适应度, 让遗传算法在"能算出来的表达式"空间里搜索, 最后把 fitness
# 最高的表达式交给人看。这条路的天花板不是算力, 而是**多重比较**: 搜索空间越大,
# 挑出来的高 IC 越可能只是噪声 (本仓 src/pbo_cscv.py / src/overfitting_test.py
# 已经在用 PBO/CSCV 与置换检验对抗它, 但那是**事后**的补救)。
#
# 本模块把顺序倒过来 —— 参考 FaVOR 一类"假设优先"的做法:
#
#   1. 先有**命题** (thesis): 一句话说清"为什么这个量能预测收益";
#   2. 再有**机制** (mechanism): 经济/行为金融上的因果链条, 而不是"它和收益相关";
#   3. 然后过 **lint**  : 经济逻辑与可计算性的前置检查 (语法/字段/未来函数/常量);
#   4. 再验 **evidence**: 在**碰收益之前**, 先看数据是否支持这个逻辑本身 ——
#      方向是否与预期一致、因子是否非退化、**分段符号是否稳定**;
#   5. 最后才轮到 **收益评估** (return evaluator), 而且收益**只用于排序**, 不用于
#      "发现": 没过 lint/evidence 的假设**绝不允许**进入收益评估阶段。
#
# 收益导向挖掘与本模块的差别, 一句话: 前者问"哪个表达式 IC 高", 后者问
# "哪个**可被证伪的经济命题**既有数据支持、又恰好有收益"。
#
# 与既有链路的关系 (同构, 不改动它们)
# ----------------------------------
# · 表达式语法与 factor_mine/gp_mine_daily.py **同形**: 采用 gplearn 打印出的
#   函数式前缀表达式 (即 gp_mine_daily._to_readable() 的输出形态), 例如
#   `div(sub(ret_20, ret_5), ts_std(ret_20, 20))`。gp_mine_daily 报告里的
#   `readable` 字段可以直接喂给本模块的 parse_expression()。
#   算子名是 gp_mine_daily.GP_FUNCTIONS 的超集 (多出 ts_* 滚动算子, 见下)。
# · 数值语义与 gp_mine_daily 的 `_safe_*` **有一处刻意分歧**: gp 的 div/log/inv
#   在退化输入上返回 1.0/0.0 (gplearn 的 closure 要求"永不产生 NaN"), 而本模块
#   返回 NaN。理由: 本模块的 evidence 阶段要用**覆盖率/唯一取值数**去判断因子
#   是否退化, 伪造出来的 0.0/1.0 会把"其实算不出来"掩盖成"算出来了"。
# · 评估可复用 factor_mine/evaluator.py 与 ai_factor_lab.py: 通过 validate_batch
#   的 `return_evaluator` 回调注入 (本模块不 import 它们, 以免把 duckdb/scipy/
#   pandas 这些重依赖拖进假设阶段)。
#
# 数据契约 (故意不依赖 pandas)
# ---------------------------
# `data` 是 `Mapping[str, np.ndarray]`:
#   · 每个字段 -> 一维 (T,) 或二维 (T, S) 的数组, T=时间(索引递增=时间递增),
#     S=截面。时序算子沿 axis=0, 截面算子沿 axis=1 (一维输入视为"单一截面");
#   · 目标变量 (未来收益) 放在 `data['__target__']` (可用 target_key 改名);
#   · **样本必须按 (时间, 截面) 顺序存放**: evidence 的分段稳定性是按数组顺序
#     切 K 段的, 顺序即时间顺序, 否则"分段"就没有子样本的含义。
#   pandas 的 Series/DataFrame 满足上述契约 (np.asarray 即可), 但本模块不 import
#   pandas —— 一个只做"假设体检"的模块不该依赖重型数据栈。
#
# ---------- 占位默认值 (重要) ----------
# 本模块所有阈值都是**占位默认值 (placeholder)**, 不是从本仓数据上标定出来的:
#   MIN_THESIS_CHARS=12 / MIN_MECHANISM_CHARS=20 / MAX_*_CHARS,
#   evidence_check 的 min_n_obs / min_coverage / min_unique / min_std /
#   min_abs_rank_ic / min_sign_consistency / min_segments / segment_min_obs。
# 生产接线处 (例如 scripts/ 里的日更任务)**必须显式传入**这些参数, 并把口径写进
# 配置与文档 —— 否则阈值就变成了"没人知道为什么是 0.6"的隐性自由度, 而隐性自由度
# 正是本模块想要消灭的东西。默认值只保证"能跑通、能自检、能在单测里被断言"。
# ============================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

__all__ = [
    # 异常
    "HypothesisError", "ExpressionError", "MissingFieldError", "HypothesisParseError",
    # 常量
    "DEFAULT_LEDGER_PATH", "DEFAULT_TARGET_KEY", "LEDGER_VERSION", "STATUSES", "SOURCES",
    # 表达式引擎
    "FUNCTION_ARITY", "TS_FUNCTIONS", "parse_expression", "to_canonical",
    "expression_fields", "evaluate_expression",
    # 秩相关工具
    "spearman_rank_ic",
    # 假设与检查
    "Hypothesis", "normalize_direction", "lint_hypothesis", "evidence_check",
    "validate_batch", "render_prompt", "parse_llm_output", "propose_from_llm",
    "HypothesisLedger", "DEFAULT_PROMPT_TEMPLATE",
]

# ===================================================================
# 0. 常量
# ===================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)

#: 假设账本默认落盘位置 (append-only JSONL)。构造函数接受 path= 以便测试隔离。
DEFAULT_LEDGER_PATH = os.path.join(_REPO, "data", "factor_hypotheses.jsonl")

#: 目标变量在 data 中的默认键名 (未来收益标签)。
DEFAULT_TARGET_KEY = "__target__"

#: 账本记录格式版本 (便于未来迁移时区分行格式)。
LEDGER_VERSION = 1

#: thesis / mechanism 的长度下限 (占位默认值, 生产接线处应显式传入)。
#: 为什么是"下限"而不是"越长越好": 长度下限只用来挡住"test"/"因子"/"待补充"这类
#: 占位符式空话 —— 一句话说不清机制的假设, 后面每一步都在为一句空话花算力。
#: 反过来, 长度**上限**只是 warning 而非拒绝: 上千字的 thesis 通常是把好几个机制
#: 揉在一起, 不可证伪, 但"冗长"本身不是逻辑错误, 交给下游人工判断更合适。
MIN_THESIS_CHARS = 12
MIN_MECHANISM_CHARS = 20
MAX_THESIS_CHARS = 600
MAX_MECHANISM_CHARS = 1200

#: 合法状态集合 (Hypothesis.status)。
STATUSES = ("proposed", "rejected", "verified", "falsified")

#: 合法来源集合 (Hypothesis.source)。
SOURCES = ("llm", "human", "template")

#: 退化输入阈值, 与 gp_mine_daily._safe_div / _safe_log 的 0.001 对齐。
DIV_EPS = 1e-3
LOG_EPS = 1e-3

#: clip 算子的截断边界 (与 gp_mine_daily.GP_FUNCTIONS 的 clip 一致)。
CLIP_BOUND = 3.0


def _now_str() -> str:
    """统一时间戳格式 (与本仓 factor_mine/ledger.py 一致)。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ===================================================================
# 1. 异常
# ===================================================================

class HypothesisError(Exception):
    """本模块所有异常的基类。"""


class ExpressionError(HypothesisError, ValueError):
    """因子表达式无法解析 / 无法求值。"""


class MissingFieldError(ExpressionError):
    """表达式引用了 data 中不存在的字段。"""


class HypothesisParseError(HypothesisError, ValueError):
    """LLM 输出无法解析为任何 Hypothesis。

    属性:
        raw:    原始输出前 500 字符 (便于留痕/复现, 不整段保存以免账本膨胀)
        errors: 逐条失败原因 (列表)
    """

    def __init__(self, message: str, raw: str = "", errors: Optional[list] = None):
        super().__init__(message)
        self.raw = str(raw)[:500]
        self.errors = list(errors or [])


# ===================================================================
# 2. 表达式引擎 (gplearn 兼容的函数式前缀表达式 + infix 兼容层)
# ===================================================================
#
# 语法:
#   前缀 (规范形态, 与 gp_mine_daily 打印一致):
#       add(ret_5, ret_20)          div(ret_20, ts_std(ret_20, 20))
#       neg(rsi_14)                 cs_rank(sub(ret_20, ret_5))
#   infix (ai_factor_lab 风格, 只为"人写的/LLM 写的"输入做兼容):
#       (ret_20 - ret_5) / ts_std(ret_20, 20)      ret_20 * -1
#   两者可混用; 一切输入都会被规范化成前缀形态 (to_canonical), 以便
#   ① hypothesis_id 对语法等价的写法保持幂等; ② 落账本后可直接回灌 GP 链路。

_TOKEN_RE = re.compile(r"\d+\.\d*|\.\d+|\d+|[A-Za-z_][A-Za-z0-9_]*|[-+*/(),]")

_INFIX_PREC = {"+": 1, "-": 1, "*": 2, "/": 2}
_INFIX_FN = {"+": "add", "-": "sub", "*": "mul", "/": "div"}

#: 一元算子 -> 元数 1。
_UNARY_FUNCTIONS = {
    "neg", "abs", "sqrt", "log", "inv",
    "cs_rank", "cs_demean", "cs_zscore",
    "square", "cube", "sign", "clip",
}
#: 二元算子 -> 元数 2。
_BINARY_FUNCTIONS = {"add", "sub", "mul", "div"}

#: 时序算子 (第二个参数必须是正整数窗口字面量)。
TS_FUNCTIONS = ("ts_mean", "ts_std", "ts_rank", "ts_delta", "ts_zscore", "ts_max", "ts_min")

#: 函数名 -> 元数 (公开, 供 lint/下游工具做静态检查)。
FUNCTION_ARITY: dict[str, int] = {}
FUNCTION_ARITY.update({k: 1 for k in _UNARY_FUNCTIONS})
FUNCTION_ARITY.update({k: 2 for k in _BINARY_FUNCTIONS})
FUNCTION_ARITY.update({k: 2 for k in TS_FUNCTIONS})


def _tokenize(expression: str) -> list[tuple[str, str, int]]:
    """把表达式切成 (kind, text, pos) 三元组; 非法字符当场报错。

    kind ∈ {'num', 'name', 'op'}。刻意不做"宽容处理"(例如把 `t+1` 当字段名) ——
    看不懂的输入必须吵, 不能猜。
    """
    s = str(expression)
    toks: list[tuple[str, str, int]] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        m = _TOKEN_RE.match(s, i)
        if not m:
            raise ExpressionError(f"表达式含非法字符 {ch!r} (位置 {i}): {s!r}")
        text = m.group(0)
        if text[0].isdigit() or text[0] == ".":
            toks.append(("num", text, i))
        elif text[0].isalpha() or text[0] == "_":
            toks.append(("name", text, i))
        else:
            toks.append(("op", text, i))
        i = m.end()
    if not toks:
        raise ExpressionError("表达式为空")
    return toks


class _Parser:
    """Pratt 解析器: 同时吃前缀函数式与 infix, 输出 ('field'|'const'|'call') 元组树。"""

    def __init__(self, tokens: list[tuple[str, str, int]], src: str):
        self.toks = tokens
        self.src = src
        self.pos = 0

    # -- 工具 ------------------------------------------------------
    def _peek(self) -> Optional[tuple[str, str, int]]:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def _next(self) -> Optional[tuple[str, str, int]]:
        t = self._peek()
        if t is not None:
            self.pos += 1
        return t

    # -- 入口 ------------------------------------------------------
    def parse(self):
        node = self._expr(0)
        rest = self._peek()
        if rest is not None:
            raise ExpressionError(
                f"表达式尾部有多余内容 {rest[1]!r} (位置 {rest[2]}): {self.src!r}")
        return node

    # -- 文法 ------------------------------------------------------
    def _expr(self, min_prec: int):
        left = self._unary()
        while True:
            t = self._peek()
            if t is None or t[0] != "op" or t[1] not in _INFIX_PREC:
                break
            prec = _INFIX_PREC[t[1]]
            if prec < min_prec:
                break
            self.pos += 1
            right = self._expr(prec + 1)
            left = ("call", _INFIX_FN[t[1]], [left, right])
        return left

    def _unary(self):
        t = self._peek()
        if t is not None and t[0] == "op" and t[1] in ("-", "+"):
            self.pos += 1
            operand = self._unary()
            if t[1] == "+":
                return operand
            if operand[0] == "const":          # 常量折叠: -1 直接是常量 -1
                return ("const", -float(operand[1]))
            return ("call", "neg", [operand])
        return self._primary()

    def _primary(self):
        t = self._next()
        if t is None:
            raise ExpressionError(f"表达式意外结束: {self.src!r}")
        kind, text, pos = t
        if kind == "num":
            return ("const", float(text))
        if kind == "name":
            nxt = self._peek()
            if nxt is not None and nxt[0] == "op" and nxt[1] == "(":
                return self._call(text, pos)
            if text in FUNCTION_ARITY:
                raise ExpressionError(f"函数 {text} 必须带括号调用: {self.src!r}")
            return ("field", text)
        if text == "(":
            node = self._expr(0)
            close = self._next()
            if close is None or close[1] != ")":
                raise ExpressionError(f"缺少右括号: {self.src!r}")
            return node
        raise ExpressionError(f"意外的 token {text!r} (位置 {pos}): {self.src!r}")

    def _call(self, name: str, pos: int):
        if name not in FUNCTION_ARITY:
            raise ExpressionError(f"未知函数 {name!r} (位置 {pos}): {self.src!r}")
        self.pos += 1                                     # 吃掉 '('
        args: list = []
        nxt = self._peek()
        if nxt is not None and nxt[0] == "op" and nxt[1] == ")":
            self.pos += 1
        else:
            while True:
                args.append(self._expr(0))
                sep = self._next()
                if sep is None:
                    raise ExpressionError(f"{name}(...) 缺少右括号: {self.src!r}")
                if sep[1] == ",":
                    continue
                if sep[1] == ")":
                    break
                raise ExpressionError(f"{name}(...) 参数分隔符非法 {sep[1]!r}: {self.src!r}")
        want = FUNCTION_ARITY[name]
        if len(args) != want:
            raise ExpressionError(
                f"{name} 需要 {want} 个参数, 实际 {len(args)} 个: {self.src!r}")
        return ("call", name, args)


def parse_expression(expression: str):
    """解析因子表达式, 返回语法树 (元组树)。

    抛出 ExpressionError (不返回 None): 解析失败必须吵 —— lint 依赖它给出
    `syntax_error`, 静默返回 None 会让"写错的表达式"伪装成"没引用字段"。
    """
    if not isinstance(expression, str):
        raise ExpressionError(f"表达式必须是字符串, 得到 {type(expression).__name__}")
    return _Parser(_tokenize(expression), str(expression)).parse()


def _fmt_num(v: float) -> str:
    """规范数字文本: 1.0 -> '1', -1.0 -> '-1', 0.5 -> '0.5'。

    目的是让 `1` 与 `1.0` 这类写法产生同一个 hypothesis_id (语法等价 => 同一个假设)。
    """
    f = float(v)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _canonical(node) -> str:
    """语法树 -> 规范前缀表达式 (gplearn/prog.__str__ 风格: fn(a, b))。"""
    kind = node[0]
    if kind == "field":
        return node[1]
    if kind == "const":
        return _fmt_num(node[1])
    return f"{node[1]}(" + ", ".join(_canonical(a) for a in node[2]) + ")"


def to_canonical(expression) -> str:
    """把任意合法写法规范化成前缀表达式 (可直接写回 GP 链路/账本)。

    `to_canonical("ret_20 * -1") == to_canonical("mul(ret_20, -1)") == "mul(ret_20, -1)"`。
    幂等: 对规范形态再次调用返回同一字符串。
    """
    node = parse_expression(expression) if isinstance(expression, str) else expression
    return _canonical(node)


def _walk_fields(node, out: list) -> None:
    kind = node[0]
    if kind == "field":
        if node[1] not in out:
            out.append(node[1])
    elif kind == "call":
        for a in node[2]:
            _walk_fields(a, out)


def expression_fields(expression) -> list[str]:
    """列出表达式引用的字段名 (按首次出现顺序去重)。"""
    node = parse_expression(expression) if isinstance(expression, str) else expression
    out: list[str] = []
    _walk_fields(node, out)
    return out


# -------------------------------------------------------------------
# 2.1 算子实现 (numpy)
# -------------------------------------------------------------------
# 退化输入一律返回 NaN, 而不是 gp_mine_daily 的 0.0/1.0 —— 见模块 docstring 的说明。

def _red_mean(view):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(view, axis=-1)


def _red_std(view):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanstd(view, axis=-1, ddof=1)


def _red_max(view):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmax(view, axis=-1)


def _red_min(view):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmin(view, axis=-1)


def _red_ts_rank(view):
    """窗口内"当前值"的分位 (<=, 归一化到 (0,1]), 与 gp_mine_daily 的 ts_rank 口径一致。"""
    valid = np.isfinite(view)
    cnt = np.sum(valid, axis=-1)
    last = view[..., -1:]
    le = np.sum(valid & (view <= last), axis=-1)
    rank = np.where(cnt > 0, le / np.maximum(cnt, 1), np.nan)
    return np.where(np.isfinite(view[..., -1]), rank, np.nan)


def _min_periods(w: int) -> int:
    """滚动窗口的最小有效观测数 (与 gp_mine_daily 的 min_periods=w//2+1 对齐)。"""
    return max(1, w // 2 + 1)


def _ts_apply(x: np.ndarray, w: int, reducer) -> np.ndarray:
    """沿 axis=0 做滚动聚合; 窗口不足的位置填 NaN (绝不用未来数据补齐)。"""
    a = np.asarray(x, dtype=float)
    if a.ndim == 0:
        raise ExpressionError("时序算子需要至少一维数组 (T,) 或 (T, S)")
    if w < 1:
        raise ExpressionError(f"时序算子窗口必须为正整数, 得到 {w}")
    t = a.shape[0]
    if t < w:
        return np.full(a.shape, np.nan, dtype=float)
    view = np.lib.stride_tricks.sliding_window_view(a, w, axis=0)
    cnt = np.sum(np.isfinite(view), axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.asarray(reducer(view), dtype=float)
    vals = np.where(cnt >= _min_periods(w), vals, np.nan)
    pad = np.full((w - 1,) + vals.shape[1:], np.nan, dtype=float)
    return np.concatenate([pad, vals], axis=0)


def _op_ts_delta(x: np.ndarray, w: int) -> np.ndarray:
    """x[t] - x[t-w] (因果: 只用过去)。"""
    a = np.asarray(x, dtype=float)
    if a.ndim == 0:
        raise ExpressionError("时序算子需要至少一维数组 (T,) 或 (T, S)")
    out = np.full(a.shape, np.nan, dtype=float)
    if w < 1:
        raise ExpressionError(f"时序算子窗口必须为正整数, 得到 {w}")
    if a.shape[0] > w:
        out[w:] = a[w:] - a[:-w]
    return out


def _op_ts_zscore(x: np.ndarray, w: int) -> np.ndarray:
    a = np.asarray(x, dtype=float)
    mu = _ts_apply(a, w, _red_mean)
    sd = _ts_apply(a, w, _red_std)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (a - mu) / sd
    return np.where(sd > 1e-12, z, np.nan)


def _op_div(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(a, b)
    return np.where(np.abs(b) > DIV_EPS, out, np.nan)


def _op_sqrt(a):
    return np.sqrt(np.abs(np.asarray(a, dtype=float)))


def _op_log(a):
    a = np.asarray(a, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(np.abs(a))
    return np.where(np.abs(a) > LOG_EPS, out, np.nan)


def _op_inv(a):
    a = np.asarray(a, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.divide(1.0, a)
    return np.where(np.abs(a) > DIV_EPS, out, np.nan)


def _op_clip(a):
    return np.clip(np.asarray(a, dtype=float), -CLIP_BOUND, CLIP_BOUND)


def _cs_axis(a: np.ndarray) -> int:
    """截面方向: 二维 (T,S) 沿 axis=1; 一维视为"单一截面"沿 axis=0。"""
    if a.ndim == 0:
        raise ExpressionError("截面算子需要至少一维数组 (T, S) 或 (S,)")
    return 1 if a.ndim >= 2 else 0


def _nanmean_axis(a: np.ndarray, axis: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(a, axis=axis, keepdims=True)


def _nanstd_axis(a: np.ndarray, axis: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanstd(a, axis=axis, keepdims=True, ddof=1)


def _op_cs_rank(a):
    """截面排序 (归一化到 (0,1]), 除以**该截面有效观测数**而不是元素总数。"""
    a = np.asarray(a, dtype=float)
    ax = _cs_axis(a)
    ranks = np.apply_along_axis(_rank_avg_1d, ax, a)
    cnt = np.sum(np.isfinite(a), axis=ax, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return ranks / np.where(cnt > 0, cnt, np.nan)


def _op_cs_demean(a):
    a = np.asarray(a, dtype=float)
    ax = _cs_axis(a)
    return a - _nanmean_axis(a, ax)


def _op_cs_zscore(a):
    a = np.asarray(a, dtype=float)
    ax = _cs_axis(a)
    mu = _nanmean_axis(a, ax)
    sd = _nanstd_axis(a, ax)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (a - mu) / sd
    return np.where(sd > 1e-12, z, 0.0)      # 与 gp_mine_daily._cs_zscore_1d 同口径


_UNARY_IMPL: dict[str, Callable] = {
    "neg": lambda a: -np.asarray(a, dtype=float),
    "abs": lambda a: np.abs(np.asarray(a, dtype=float)),
    "sqrt": _op_sqrt,
    "log": _op_log,
    "inv": _op_inv,
    "square": lambda a: np.asarray(a, dtype=float) ** 2,
    "cube": lambda a: np.asarray(a, dtype=float) ** 3,
    "sign": lambda a: np.sign(np.asarray(a, dtype=float)),
    "clip": _op_clip,
    "cs_rank": _op_cs_rank,
    "cs_demean": _op_cs_demean,
    "cs_zscore": _op_cs_zscore,
}

_BINARY_IMPL: dict[str, Callable] = {
    "add": lambda a, b: np.asarray(a, dtype=float) + np.asarray(b, dtype=float),
    "sub": lambda a, b: np.asarray(a, dtype=float) - np.asarray(b, dtype=float),
    "mul": lambda a, b: np.asarray(a, dtype=float) * np.asarray(b, dtype=float),
    "div": _op_div,
}

_TS_IMPL: dict[str, Callable] = {
    "ts_mean": lambda x, w: _ts_apply(x, w, _red_mean),
    "ts_std": lambda x, w: _ts_apply(x, w, _red_std),
    "ts_rank": lambda x, w: _ts_apply(x, w, _red_ts_rank),
    "ts_max": lambda x, w: _ts_apply(x, w, _red_max),
    "ts_min": lambda x, w: _ts_apply(x, w, _red_min),
    "ts_delta": _op_ts_delta,
    "ts_zscore": _op_ts_zscore,
}


def _window_arg(node, fname: str) -> int:
    """取 ts_*(x, w) 的窗口参数; 必须是正整数字面量 (否则吵)。"""
    if node[0] != "const":
        raise ExpressionError(f"{fname} 的窗口参数必须是整数字面量, 不能用表达式/字段")
    w = float(node[1])
    if not np.isfinite(w) or w <= 0 or float(int(w)) != w:
        raise ExpressionError(f"{fname} 的窗口参数必须是正整数, 得到 {_fmt_num(w)}")
    return int(w)


def _eval_node(node, data: Mapping[str, Any]) -> np.ndarray:
    kind = node[0]
    if kind == "field":
        name = node[1]
        if name not in data:
            raise MissingFieldError(f"data 中缺少字段 {name!r} (表达式依赖它)")
        return np.asarray(data[name], dtype=float)
    if kind == "const":
        return np.asarray(float(node[1]), dtype=float)
    name, args = node[1], node[2]
    if name in _TS_IMPL:
        x = _eval_node(args[0], data)
        w = _window_arg(args[1], name)
        return _TS_IMPL[name](x, w)
    vals = [_eval_node(a, data) for a in args]
    impl = _UNARY_IMPL.get(name) or _BINARY_IMPL.get(name)
    if impl is None:                                    # 理论上不可达 (解析器已挡)
        raise ExpressionError(f"未实现的算子: {name}")
    return impl(*vals)


def evaluate_expression(expression, data: Mapping[str, Any]) -> np.ndarray:
    """按 data 计算因子值。

    Args:
        expression: 表达式字符串, Hypothesis (取其 .expression), 或 parse_expression()
            的返回值。
        data: {字段名: 一维/二维 np.ndarray}; 见模块 docstring 的数据契约。

    Returns:
        np.ndarray, 形状与字段一致 (常量表达式返回 0 维数组)。

    Raises:
        ExpressionError / MissingFieldError: 解析失败或字段缺失 —— 不返回全 NaN,
            因为"算不出来"和"算出来全是缺失"必须在 evidence 阶段被区分开。
    """
    if isinstance(expression, Hypothesis):
        expression = expression.expression
    node = parse_expression(expression) if isinstance(expression, str) else expression
    return _eval_node(node, data)


# ===================================================================
# 3. 秩相关 (自己实现, 不引入 scipy 硬依赖)
# ===================================================================

def _rank_avg_1d(x) -> np.ndarray:
    """平均秩 (并列取均值, 1-based), NaN 保持 NaN。等价 scipy.stats.rankdata 的默认行为。"""
    a = np.asarray(x, dtype=float).ravel()
    out = np.full(a.shape, np.nan, dtype=float)
    mask = np.isfinite(a)
    v = a[mask]
    if v.size == 0:
        return out
    order = np.argsort(v, kind="mergesort")     # v 中第 i 小的元素在原始数组的下标 order[i]
    sv = v[order]
    uniq, inv = np.unique(sv, return_inverse=True)   # inv: 排序后第 j 个位置属于第几个不同取值
    cnt = np.bincount(inv, minlength=uniq.size)
    csum = np.cumsum(cnt)
    start = csum - cnt
    avg_rank = (start + csum - 1) / 2.0 + 1.0        # 每个不同取值的平均秩 (1-based)
    ranks_sorted = avg_rank[inv]                     # 排序后顺序上的秩
    ranks = np.empty_like(ranks_sorted)
    ranks[order] = ranks_sorted                      # 还原回原始顺序 (漏了这步 => 秩退化成 1..n)
    out[mask] = ranks
    return out


def spearman_rank_ic(factor_values, target_values,
                     min_pairs: int = 3) -> Optional[float]:
    """秩相关 (Spearman), 自行实现 —— 本模块不 import scipy。

    只成对剔除 (NaN/inf 同删), 并列取平均秩。样本不足或任一侧无变异时返回 None
    (**不返回 0.0**: 0 表示"无相关", None 表示"无法判断", 两者在 evidence 阶段
    的处置完全不同)。

    Args:
        min_pairs: 最少有效配对数。默认 3 只是"相关系数在数学上还能定义"的下限;
            evidence_check 会用自己的门槛 (min_pairs 参数, 占位默认值 10) 再收一道。
    """
    f = np.asarray(factor_values, dtype=float).ravel()
    y = np.asarray(target_values, dtype=float).ravel()
    if f.shape != y.shape:
        return None
    mask = np.isfinite(f) & np.isfinite(y)
    if int(mask.sum()) < int(min_pairs):
        return None
    rf = _rank_avg_1d(f[mask])
    ry = _rank_avg_1d(y[mask])
    rf = rf - rf.mean()
    ry = ry - ry.mean()
    denom = float(np.sqrt(np.sum(rf * rf) * np.sum(ry * ry)))
    if denom <= 0.0:
        return None
    return float(np.clip(float(np.sum(rf * ry)) / denom, -1.0, 1.0))


# ===================================================================
# 4. Hypothesis
# ===================================================================

_DIR_POS = {"+1", "1", "1.0", "positive", "pos", "+", "up", "long", "正", "正相关", "正向"}
_DIR_NEG = {"-1", "-1.0", "negative", "neg", "-", "down", "short", "负", "负相关", "反向"}


def normalize_direction(value) -> int:
    """把各种写法归一成 +1 / -1; **无法识别 (含 0/2/1.5/bool) 一律返回 0**, 由 lint 拒绝。

    只接受"就是 +1 / -1"的写法 (数值、数字字符串、positive/negative 等词)。返回 0 而不是
    猜一个方向: 方向是假设的核心断言, 猜一个等于伪造断言 —— 而伪造的断言后面每一步都会
    被当真。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = float(value)
        if not np.isfinite(v):
            return 0
        if v == 1.0:
            return 1
        if v == -1.0:
            return -1
        return 0
    s = str(value).strip().lower()
    if s in _DIR_POS:
        return 1
    if s in _DIR_NEG:
        return -1
    return 0


def _norm_text(s: Any) -> str:
    """文本归一 (折叠空白 + 小写), 让同一命题的不同排版得到同一个 id。"""
    return " ".join(str(s or "").split()).lower()


@dataclass
class Hypothesis:
    """一个**可证伪**的因子假设 (本模块的核心数据对象)。

    字段:
        hypothesis_id:   稳定哈希 (见 _compute_id), 内容寻址: 同一命题 => 同一 id。
        thesis:          自然语言命题 (一句话: 为什么这个量能预测收益)。
        mechanism:       经济机制 / 行为金融解释 (因果链条, 不是"它和收益相关")。
        direction:       +1 / -1, 预期因子值与未来收益的秩相关符号; 0 = 未识别(无效)。
        expression:      可计算的因子表达式 (规范化为 gp_mine_daily 同形的前缀形态)。
        required_fields: 依赖的数据字段名列表。
        source:          'llm' / 'human' / 'template'。
        status:          'proposed' / 'rejected' / 'verified' / 'falsified'。
        evidence:        list[dict], 每条形如 {'stage':..., 'ts':..., ...指标}。
        created_at:      创建时刻。

    身份 (id) 的口径: 只哈希**内容字段** thesis + mechanism + direction + 规范表达式;
    source/status/evidence/created_at 是元数据与状态, 刻意不参与哈希 —— 否则"同一个
    假设被重新评估一次"就会变成两个假设, 账本再也对不上账。required_fields 同样不参与
    (它可由表达式推导, 顺序/写法噪声不该改变身份)。
    """

    hypothesis_id: str = ""
    thesis: str = ""
    mechanism: str = ""
    direction: int = 1
    expression: str = ""
    required_fields: list[str] = field(default_factory=list)
    source: str = "template"
    status: str = "proposed"
    evidence: list[dict] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.thesis = str(self.thesis or "").strip()
        self.mechanism = str(self.mechanism or "").strip()
        self.direction = normalize_direction(self.direction)
        self.expression = str(self.expression or "").strip()
        # required_fields: 去重但保留书写顺序 (顺序不该影响 id, 见 _compute_id)
        seen: list[str] = []
        for f in (self.required_fields or []):
            fs = str(f).strip()
            if fs and fs not in seen:
                seen.append(fs)
        self.required_fields = seen
        self.source = str(self.source or "template").strip().lower()
        if self.source not in SOURCES:
            self.source = "template"
        self.status = str(self.status or "proposed").strip().lower()
        if self.status not in STATUSES:
            self.status = "proposed"
        self.evidence = list(self.evidence or [])
        if self.expression:
            try:      # 能解析就规范化; 解析不了保留原文, 由 lint 报 syntax_error
                self.expression = to_canonical(self.expression)
            except ExpressionError:
                pass
        if not self.created_at:
            self.created_at = _now_str()
        self.hypothesis_id = self._compute_id()

    # -- 身份 ------------------------------------------------------
    def _compute_id(self) -> str:
        """内容寻址 id: hp_ + sha256(内容) 前 16 位十六进制。

        稳定性来自两点: ① 文本归一 (折叠空白/大小写); ② 表达式在 __post_init__ 里
        已被规范化成前缀形态 —— 所以 `ret_20 * -1` 与 `mul(ret_20, -1)` 是同一个假设。
        """
        key = "|".join([
            _norm_text(self.thesis),
            _norm_text(self.mechanism),
            str(int(self.direction)),
            self.expression,
        ])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return "hp_" + digest[:16]

    # -- 序列化 ----------------------------------------------------
    def to_dict(self) -> dict:
        """转 dict (账本/JSON 用)。键名与本模块公开接口一致。"""
        return {
            "hypothesis_id": self.hypothesis_id,
            "thesis": self.thesis,
            "mechanism": self.mechanism,
            "direction": int(self.direction),
            "expression": self.expression,
            "required_fields": list(self.required_fields),
            "source": self.source,
            "status": self.status,
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Hypothesis":
        """从 dict 还原 (未知键忽略, 缺失键取默认)。id 会被重算 (内容寻址)。"""
        if not isinstance(d, Mapping):
            raise TypeError(f"from_dict 期望 Mapping, 得到 {type(d).__name__}")
        return cls(
            thesis=d.get("thesis", ""),
            mechanism=d.get("mechanism", ""),
            direction=d.get("direction", 1),
            expression=d.get("expression", ""),
            required_fields=list(d.get("required_fields") or []),
            source=d.get("source", "template"),
            status=d.get("status", "proposed"),
            evidence=list(d.get("evidence") or []),
            created_at=d.get("created_at", "") or "",
        )

    # -- 证据 ------------------------------------------------------
    def add_evidence(self, stage: str, payload: Any) -> dict:
        """追加一条证据 (就地修改, 返回该条记录)。stage ∈ {'lint','evidence','return',...}。"""
        rec: dict[str, Any] = {"stage": str(stage), "ts": _now_str()}
        if isinstance(payload, Mapping):
            rec.update(dict(payload))
        else:
            rec["payload"] = payload
        self.evidence.append(rec)
        return rec

    def summary(self) -> dict:
        """单行摘要 (CLI / 日志用)。"""
        return {
            "hypothesis_id": self.hypothesis_id,
            "thesis": self.thesis[:60],
            "direction": int(self.direction),
            "expression": self.expression,
            "status": self.status,
            "source": self.source,
            "n_evidence": len(self.evidence),
        }


def _as_hypothesis(h) -> Hypothesis:
    """容错入口: 允许把账本里的 dict 直接喂给检查函数。"""
    if isinstance(h, Hypothesis):
        return h
    if isinstance(h, Mapping):
        return Hypothesis.from_dict(h)
    raise TypeError(f"期望 Hypothesis 或 dict, 得到 {type(h).__name__}")


# ===================================================================
# 5. lint: 经济逻辑与可计算性的前置检查 (碰收益之前)
# ===================================================================
#
# 未来函数的静态防线。命中即拒绝 —— 这一条不能"警告了事": 前视偏差不会抛异常、
# 不会算错数, 只会让后续每一步结论都虚高, 且事后极难发现。宁可错杀 (例如字段名
# 里含 'future' 但确实不是未来数据), 也不放过 —— 错杀的代价是重写一个假设,
# 放过的代价是一整条链路失真。

_FUTURE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("future_shift", re.compile(r"\bshift\s*\(\s*-", re.I)),
    ("future_diff", re.compile(r"\b(?:diff|pct_change)\s*\(\s*-", re.I)),
    ("future_keyword", re.compile(r"\bfuture", re.I)),
    ("future_next", re.compile(r"\bnext_", re.I)),
    ("future_t_plus", re.compile(r"(?<![A-Za-z0-9_])t\+\s*\d", re.I)),
    ("future_iloc", re.compile(r"\.iloc\s*\[[^\]]*\+\s*\d", re.I)),
    ("future_bfill", re.compile(r"(bfill\s*\(|backfill\s*\(|fillna\s*\([^)]*bfill)", re.I)),
    ("future_negative_window", re.compile(r"\bts_\w+\s*\([^()]*,\s*-", re.I)),
    ("centered_window", re.compile(r"center\s*=\s*true", re.I)),
)


def _check_window_nodes(node, reasons: list[str]) -> None:
    """语法树级检查: ts_*(x, w) 的窗口必须是正整数字面量。

    正则只能挡字符串写法, 这一层挡的是"解析后结构上就是未来数据"的情况
    (例如 ts_delta(ret_20, -1)) —— 两层叠加, 因为静态防线的价值在于覆盖不同写法。
    """
    if node[0] != "call":
        return
    name, args = node[1], node[2]
    if name in _TS_IMPL:
        wnode = args[1]
        if wnode[0] == "const":
            w = float(wnode[1])
            if w <= 0:
                reasons.append(
                    f"future_function: {name} 的窗口为 {_fmt_num(w)} —— 零/负窗口取的是未来数据")
            elif float(int(w)) != w:
                reasons.append(f"bad_window: {name} 的窗口必须是正整数, 得到 {_fmt_num(w)}")
        else:
            reasons.append(f"bad_window: {name} 的窗口必须是整数字面量, 无法静态确认是因果窗口")
    for a in args:
        _check_window_nodes(a, reasons)


def lint_hypothesis(h, available_fields: Optional[Iterable[str]] = None, *,
                    min_thesis_chars: int = MIN_THESIS_CHARS,
                    min_mechanism_chars: int = MIN_MECHANISM_CHARS,
                    max_thesis_chars: int = MAX_THESIS_CHARS,
                    max_mechanism_chars: int = MAX_MECHANISM_CHARS) -> dict:
    """假设的**前置体检**: 经济逻辑是否说得清 + 表达式是否可算且没用未来数据。

    Args:
        h: Hypothesis 或等价的 dict。
        available_fields: 当前可用的数据字段名集合 (None = 跳过字段可用性检查并给 warning)。
        min_thesis_chars / min_mechanism_chars: 长度下限。**占位默认值** (12 / 20 字符):
            生产接线处应显式传入 —— 下限应随"命题模板"的实际写法标定, 而不是拍一个数。
            为什么不是越长越好: 长度只能挡住占位符式空话; 真正的质量靠 mechanism 是否
            给出**因果链条**、以及后续 evidence 阶段能否被数据检验, 靠字数堆不出来。
        max_thesis_chars / max_mechanism_chars: 长度上限, 只出 warning 不拒绝 (冗长≠错误)。

    Returns:
        {'ok': bool, 'reasons': [...], 'warnings': [...], 'fields': [...],
         'expression_canonical': str}

        reasons 里的稳定前缀 (下游按前缀分流, 不要解析中文描述):
            empty_thesis / short_thesis / long_thesis(warning)
            empty_mechanism / short_mechanism / long_mechanism(warning)
            bad_direction            方向必须是 +1/-1
            missing_fields           required_fields 里有当前拿不到的字段
            unknown_field_in_expr    表达式引用了拿不到的字段
            undeclared_field(warning) 表达式用了可用但未在 required_fields 声明的字段
            future_function          命中未来函数 (shift(-n)/future/next_/t+N/iloc[i+1]/
                                     负窗口 ts_*/bfill 等)
            bad_window               ts_* 窗口不是可静态确认的正整数
            syntax_error             表达式无法解析
            constant_expression      表达式未引用任何字段 (常量, 不构成因子)
            empty_expression         表达式为空
            available_fields_not_provided(warning)
    """
    hyp = _as_hypothesis(h)
    reasons: list[str] = []
    warnings_: list[str] = []

    # --- 1) 命题与机制 (经济逻辑能不能说清楚) ---
    thesis, mech = hyp.thesis, hyp.mechanism
    if not thesis:
        reasons.append("empty_thesis: thesis 为空 —— 没有命题就没有可证伪的假设")
    elif len(thesis) < int(min_thesis_chars):
        reasons.append(
            f"short_thesis: thesis 仅 {len(thesis)} 字符 < 下限 {int(min_thesis_chars)}")
    elif len(thesis) > int(max_thesis_chars):
        warnings_.append(
            f"long_thesis: thesis {len(thesis)} 字符 > {int(max_thesis_chars)}, "
            f"过长往往是把多个机制揉在一起, 不可证伪")
    if not mech:
        reasons.append("empty_mechanism: mechanism 为空 —— 必须给出经济/行为金融机制")
    elif len(mech) < int(min_mechanism_chars):
        reasons.append(
            f"short_mechanism: mechanism 仅 {len(mech)} 字符 < 下限 {int(min_mechanism_chars)}")
    elif len(mech) > int(max_mechanism_chars):
        warnings_.append(f"long_mechanism: mechanism {len(mech)} 字符 > {int(max_mechanism_chars)}")

    # --- 2) 方向 ---
    if hyp.direction not in (1, -1):
        reasons.append(
            f"bad_direction: direction={hyp.direction!r} 必须是 +1 或 -1 (方向是核心断言, 不许缺省)")

    # --- 3) 字段可用性 (声明侧) ---
    avail: Optional[set] = None
    if available_fields is None:
        warnings_.append("available_fields_not_provided: 未提供可用字段集合, 跳过字段可用性检查")
    else:
        avail = {str(x) for x in available_fields}
        missing = [f for f in hyp.required_fields if f not in avail]
        if missing:
            reasons.append(f"missing_fields: required_fields 中缺少可用字段 {missing}")

    # --- 4) 未来函数静态扫描 (在解析之前, 先挡"根本不该出现"的写法) ---
    raw_expr = hyp.expression
    if not raw_expr:
        reasons.append("empty_expression: expression 为空")
    else:
        for code, pat in _FUTURE_PATTERNS:
            m = pat.search(raw_expr)
            if m:
                reasons.append(
                    f"future_function: 命中未来函数模式 {code} -> {m.group(0)!r} "
                    f"(前视偏差的静态防线, 命中即拒)")

    # --- 5) 语法 + 结构 ---
    node = None
    canonical = raw_expr
    if raw_expr:
        try:
            node = parse_expression(raw_expr)
            canonical = _canonical(node)
        except ExpressionError as e:
            reasons.append(f"syntax_error: 表达式无法解析: {e}")

    fields: list[str] = []
    if node is not None:
        _walk_fields(node, fields)
        if not fields:
            reasons.append("constant_expression: 表达式未引用任何字段(常量), 不构成因子")
        if avail is not None:
            unknown = [f for f in fields if f not in avail]
            if unknown:
                reasons.append(f"unknown_field_in_expr: 表达式引用不可用字段 {unknown}")
            undeclared = [f for f in fields if f not in hyp.required_fields and f in avail]
            if undeclared:
                warnings_.append(
                    f"undeclared_field: 表达式使用了未在 required_fields 声明的字段 {undeclared}")
        _check_window_nodes(node, reasons)

    # 去重但保序 (同一个原因可能被正则层与语法树层各命中一次)
    reasons = list(dict.fromkeys(reasons))
    warnings_ = list(dict.fromkeys(warnings_))
    return {
        "ok": not reasons,
        "reasons": reasons,
        "warnings": warnings_,
        "fields": fields,
        "expression_canonical": canonical,
    }


# ===================================================================
# 6. evidence: 假设级证据 (在算收益之前)
# ===================================================================

def evidence_check(h, data: Mapping[str, Any], *,
                   target_key: str = DEFAULT_TARGET_KEY,
                   n_segments: int = 5,
                   min_n_obs: int = 200,
                   min_coverage: float = 0.6,
                   min_unique: int = 10,
                   min_std: float = 1e-12,
                   min_abs_rank_ic: float = 0.01,
                   min_sign_consistency: float = 0.6,
                   min_segments: int = 3,
                   segment_min_obs: int = 20,
                   min_pairs: int = 10) -> dict:
    """**假设级证据体检**: 先问"这个因子的逻辑被数据支持吗", 再谈收益。

    检查项:
      1. 方向: 因子与目标的全样本 Spearman 秩相关, 符号是否与 h.direction 一致,
         且绝对值达到 min_abs_rank_ic (符号对但强度约等于 0 的"方向正确"没有意义);
      2. 覆盖率与非退化性: 非空比例、唯一取值数、标准差 (近似常量/零方差直接拒);
      3. 子样本稳定性: 把样本按顺序切 K 段, 各段秩相关的**同向比例** ——
         全样本一个数会被少数极端段掩盖, 分段符号一致率才看得出"是不是一直有效"。

    Args:
        h: Hypothesis 或等价 dict。
        data: 见模块 docstring 的数据契约; 顺序必须是 (时间, 截面) 顺序。
        target_key: 目标变量键名 (占位默认值 '__target__')。
        n_segments: 分段数 (占位默认值 5)。
        min_n_obs: 最少有效配对数 (占位默认值 200)。
        min_coverage: 因子最低覆盖率 = 有效因子值 / 因子数组元素总数 (占位默认值 0.6)。
        min_unique: 唯一取值数下限, <= 该值视为"近似常量" (占位默认值 10)。
        min_std: 标准差下限 (占位默认值 1e-12, 真正的零方差)。
        min_abs_rank_ic: |全样本秩相关| 下限 (占位默认值 0.01)。
        min_sign_consistency: 分段同向比例下限 (占位默认值 0.6, 即 5 段里至少 3 段同向)。
        min_segments: 有效分段数下限 (占位默认值 3) —— **只有 1 段的"稳定性"不是稳定性**。
        segment_min_obs: 单段最少有效配对数 (占位默认值 20), 不够的段不计入分母。
        min_pairs: 秩相关最少配对数 (占位默认值 10)。

    **以上默认值全是占位值**: 它们只保证本模块能跑通、能在单测里被断言; 生产接线处
    (日更任务/研究脚本) 必须**显式传入**, 并把口径写进配置 —— 阈值是本模块唯一的
    "自由度", 隐式自由度会让"通过证据体检"变成一句无法审计的话。

    Returns:
        {'ok', 'direction_ok', 'rank_ic', 'sign_consistency', 'coverage',
         'n_obs', 'reasons', ...}
        另含诊断字段: sign_ok / n_unique / factor_std / n_segments /
        n_segments_effective / segment_ics / target_key / expression。
        rank_ic 可能为 None (无法定义, 例如因子无变异): 与 0.0 含义不同, 别混用。
    """
    hyp = _as_hypothesis(h)
    reasons: list[str] = []
    result: dict[str, Any] = {
        "ok": False,
        "direction_ok": False,
        "sign_ok": False,
        "rank_ic": None,
        "sign_consistency": None,
        "coverage": 0.0,
        "n_obs": 0,
        "n_unique": 0,
        "factor_std": None,
        "n_segments": int(n_segments),
        "n_segments_effective": 0,
        "segment_ics": [],
        "target_key": target_key,
        "expression": hyp.expression,
        "reasons": reasons,
    }

    # --- 0) 算因子值 ---
    try:
        factor = np.asarray(evaluate_expression(hyp.expression, data), dtype=float)
    except ExpressionError as e:
        reasons.append(f"expression_error: 无法计算因子值: {e}")
        return result
    if target_key not in data:
        reasons.append(f"missing_target: data 中缺少目标变量 {target_key!r}")
        return result
    target = np.asarray(data[target_key], dtype=float)
    if factor.shape != target.shape:
        reasons.append(
            f"shape_mismatch: 因子形状 {factor.shape} 与目标 {target.shape} 不一致")
        return result

    total = int(factor.size)
    f_flat = factor.ravel()
    y_flat = target.ravel()
    pair_mask = np.isfinite(f_flat) & np.isfinite(y_flat)
    n_obs = int(pair_mask.sum())
    result["n_obs"] = n_obs
    result["coverage"] = float(n_obs / total) if total else 0.0

    # --- 1) 样本量 / 覆盖率 / 非退化性 ---
    if n_obs == 0:
        reasons.append("no_observation: 没有任何成对有效观测")
        return result
    valid_vals = f_flat[pair_mask]
    n_unique = int(np.unique(np.round(valid_vals, 12)).size)
    std = float(np.std(valid_vals))
    result["n_unique"] = n_unique
    result["factor_std"] = std

    if n_obs < int(min_n_obs):
        reasons.append(f"insufficient_obs: 有效样本 {n_obs} < {int(min_n_obs)}")
    if result["coverage"] < float(min_coverage):
        reasons.append(
            f"low_coverage: 覆盖率 {result['coverage']:.3f} < {float(min_coverage):.3f} "
            f"(有效 {n_obs} / 总 {total})")
    if n_unique <= int(min_unique):
        reasons.append(f"low_cardinality: 唯一取值 {n_unique} <= {int(min_unique)} (近似常量)")
    if std <= float(min_std):
        reasons.append(f"zero_variance: 标准差 {std:.3e} <= {float(min_std):.3e}")

    # --- 2) 全样本方向 ---
    ic = spearman_rank_ic(f_flat[pair_mask], y_flat[pair_mask], min_pairs=min_pairs)
    result["rank_ic"] = ic
    if ic is None:
        reasons.append("rank_ic_undefined: 秩相关无法定义 (因子或目标无变异/配对太少)")
    else:
        sign_ok = (ic > 0 and hyp.direction > 0) or (ic < 0 and hyp.direction < 0)
        result["sign_ok"] = bool(sign_ok)
        if not sign_ok:
            reasons.append(
                f"direction_mismatch: rank_ic={ic:+.3f} 与 direction={hyp.direction:+d} 符号相反")
        elif abs(ic) < float(min_abs_rank_ic):
            reasons.append(
                f"weak_rank_ic: |rank_ic|={abs(ic):.4f} < {float(min_abs_rank_ic):.4f}")
        else:
            result["direction_ok"] = True

    # --- 3) 子样本稳定性 (K 段同向比例) ---
    n_seg = max(1, int(n_segments))
    idx = np.flatnonzero(pair_mask)
    seg_ics: list[Optional[float]] = []
    for chunk in np.array_split(idx, n_seg):
        if chunk.size < int(segment_min_obs):
            seg_ics.append(None)
            continue
        seg_ics.append(spearman_rank_ic(f_flat[chunk], y_flat[chunk], min_pairs=min_pairs))
    defined = [s for s in seg_ics if s is not None]
    result["segment_ics"] = seg_ics
    result["n_segments_effective"] = len(defined)
    if defined:
        agree = sum(1 for s in defined
                    if (s > 0 and hyp.direction > 0) or (s < 0 and hyp.direction < 0))
        result["sign_consistency"] = float(agree / len(defined))

    if len(defined) < int(min_segments):
        reasons.append(
            f"insufficient_segments: 有效分段 {len(defined)} < {int(min_segments)} "
            f"(分段稳定性不可判 —— 全样本一个数不能当作稳健性)")
    elif result["sign_consistency"] < float(min_sign_consistency):
        reasons.append(
            f"unstable_sign: 分段同向比例 {result['sign_consistency']:.2f} "
            f"< {float(min_sign_consistency):.2f} (各段符号见 segment_ics)")

    reasons[:] = list(dict.fromkeys(reasons))
    result["ok"] = not reasons
    return result


# ===================================================================
# 7. validate_batch: lint -> evidence -> (仅通过者) 收益评估
# ===================================================================

_PASS_VERDICTS = {"ADOPT", "PASS", "PASSED", "VERIFIED", "OK", "ACCEPT"}


def _interpret_return_eval(res: Any) -> tuple[Optional[bool], str]:
    """把收益评估回调的返回值判成 通过/不通过。

    判定顺序 (显式, 不用 truthiness 猜):
        1. bool                        -> 直接用
        2. dict 里有 'passed' (bool)   -> 用它
        3. dict 里有 'ok' (bool)       -> 用它
        4. dict 里 'verdict' 是字符串  -> 在 _PASS_VERDICTS 里则通过, 否则不通过
        5. 其它                        -> (None, 原因) => 按**不通过**处置 (fail-closed)
    """
    if isinstance(res, bool):
        return res, "bool"
    if isinstance(res, Mapping):
        if isinstance(res.get("passed"), bool):
            return bool(res["passed"]), "passed"
        if isinstance(res.get("ok"), bool):
            return bool(res["ok"]), "ok"
        verdict = res.get("verdict")
        if isinstance(verdict, str):
            return verdict.strip().upper() in _PASS_VERDICTS, "verdict"
    return None, "无法判定 (回调返回值既非 bool 也无 passed/ok/verdict)"


def validate_batch(hypotheses: Iterable, data: Mapping[str, Any],
                   available_fields: Optional[Iterable[str]] = None,
                   **evidence_kwargs) -> dict:
    """把一批假设推过流水线: lint -> evidence -> (仅通过者) 收益评估。

    **核心纪律 (本模块存在的理由)**: 没通过 lint 或 evidence 的假设, 绝不允许进入
    收益评估阶段。收益是最后的排序依据, 不是发现因子的驱动力 —— 一旦让"收益高"的
    假设绕过后置检查, 就等于把收益率重新变成了挖掘目标, 多重比较问题原样回来。
    实现上靠控制流保证 (每阶段失败即 continue), 并由返回值里的 `return_eval_calls`
    计数器让它**可被测试锁死**; 单测里用一个 mock 计数器断言"从未被调用"。

    Args:
        hypotheses: Iterable[Hypothesis | dict]。
        data: 见模块 docstring 的数据契约。
        available_fields: 可用字段集合; None = 由 data 的键推导 (去掉 target)。
        **evidence_kwargs:
            return_evaluator: 可选回调 `(h, data) -> dict|bool`, 只在 evidence 通过后调用。
                生产接线处在这里接 factor_mine/evaluator.evaluate 或
                ai_factor_lab.evaluate_hypothesis —— 但**必须在回调内部显式传入自己的
                门槛参数**, 不要依赖本模块或它们的占位默认值。
            ledger: 可选 HypothesisLedger; 提供则逐阶段落账 (proposed/lint/evidence/return)。
            target_key: 透传给 evidence_check。
            其余键全部透传给 evidence_check。

    Returns:
        {'proposed', 'lint_rejected', 'evidence_rejected', 'verified', 'falsified',
         'records': [...], 'return_eval_calls', 'evidence_passed',
         'ledger_write_failures', 'settings'}
        说明: 没有提供 return_evaluator 时, 'verified' 表示"**仅**通过假设级证据,
        收益未评估" —— 记录里 return_eval 为 None 以示区分, 不要把两者混为一谈。
    """
    return_evaluator = evidence_kwargs.pop("return_evaluator", None)
    ledger = evidence_kwargs.pop("ledger", None)
    target_key = evidence_kwargs.get("target_key", DEFAULT_TARGET_KEY)
    if available_fields is None:
        available_fields = [k for k in data.keys() if k != target_key]
    avail = {str(x) for x in available_fields}

    hyps = [_as_hypothesis(x) for x in hypotheses]
    records: list[dict] = []
    counters = {"proposed": len(hyps), "lint_rejected": 0, "evidence_rejected": 0,
                "verified": 0, "falsified": 0}
    return_eval_calls = 0
    evidence_passed = 0

    def _log(h: Hypothesis, stage: str, payload: Any) -> None:
        if ledger is not None:
            ledger.append(h, stage, payload)

    for h in hyps:
        _log(h, "proposed", {"thesis": h.thesis, "direction": h.direction})
        rec: dict[str, Any] = {
            "hypothesis_id": h.hypothesis_id,
            "thesis": h.thesis,
            "expression": h.expression,
            "direction": h.direction,
            "source": h.source,
            "lint": None,
            "evidence": None,
            "return_eval": None,
            "stage": "",
            "status": "",
        }

        # --- 阶段 1: lint ---
        lint = lint_hypothesis(h, avail)
        rec["lint"] = lint
        if not lint["ok"]:
            h.status = "rejected"      # 先定状态再落账: 账本必须能回答"是哪一步拒的"
        h.add_evidence("lint", lint)
        _log(h, "lint", lint)
        if not lint["ok"]:
            counters["lint_rejected"] += 1
            rec["stage"], rec["status"] = "lint_rejected", "rejected"
            records.append(rec)
            continue                                  # 纪律: 绝不进入收益评估

        # --- 阶段 2: evidence (假设级, 碰收益之前) ---
        ev = evidence_check(h, data, **evidence_kwargs)
        rec["evidence"] = ev
        if not ev["ok"]:
            h.status = "rejected"
        h.add_evidence("evidence", ev)
        _log(h, "evidence", ev)
        if not ev["ok"]:
            counters["evidence_rejected"] += 1
            rec["stage"], rec["status"] = "evidence_rejected", "rejected"
            records.append(rec)
            continue                                  # 纪律: 绝不进入收益评估

        evidence_passed += 1

        # --- 阶段 3: 收益评估 (可选, 只对通过者) ---
        if return_evaluator is None:
            h.status = "verified"
            counters["verified"] += 1
            rec["stage"], rec["status"] = "evidence_passed", "verified"
            records.append(rec)
            continue

        return_eval_calls += 1
        try:
            res = return_evaluator(h, data)
        except Exception as e:                        # 回调自己炸了 => 该假设判否, 不拖垮整批
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        passed, basis = _interpret_return_eval(res)
        rec["return_eval"] = res if isinstance(res, Mapping) else {"value": res}
        rec["return_eval_basis"] = basis
        if passed:
            h.status = "verified"
            counters["verified"] += 1
            rec["stage"], rec["status"] = "return_evaluated", "verified"
        else:
            h.status = "falsified"
            counters["falsified"] += 1
            rec["stage"], rec["status"] = "return_evaluated", "falsified"
        h.add_evidence("return", rec["return_eval"])
        _log(h, "return", rec["return_eval"])
        records.append(rec)

    # 账本写入失败要能被调用方看见 (留痕失败不抛, 但绝不静默)
    out = dict(counters)
    out["records"] = records
    out["return_eval_calls"] = return_eval_calls
    out["evidence_passed"] = evidence_passed
    out["ledger_write_failures"] = int(getattr(ledger, "write_failures", 0)) if ledger else 0
    out["settings"] = {k: v for k, v in evidence_kwargs.items() if not callable(v)}
    return out


# ===================================================================
# 8. HypothesisLedger: append-only 落盘 (JSONL)
# ===================================================================

class HypothesisLedger:
    """假设账本: append-only JSONL, 每条保存**逻辑 + 指标 + 来源元数据**。

    形态参考本仓 factor_mine/ledger.py (register/append_report) 与
    src/kill_switch.py 的审计账本: 只追加、不重写、不删行 —— 事后能回答
    "这个因子当初凭什么被放进来、又是被哪一步拒掉的"。

    纪律 (本仓既有约定): **写入失败绝不抛异常** —— 留痕不得拖垮主链路。但也不能
    静默: append() 返回 False, 并把原因写进 self.last_error / self.write_failures,
    调用方据此决定是否告警。
    """

    def __init__(self, path: Optional[str] = None):
        """
        Args:
            path: 账本文件路径; None = DEFAULT_LEDGER_PATH
                  (<repo>/data/factor_hypotheses.jsonl)。测试请传 tmp 路径。
        """
        self.path = str(path) if path else DEFAULT_LEDGER_PATH
        self.last_error = ""
        self.write_failures = 0
        self.bad_lines = 0

    # -- 写 --------------------------------------------------------
    def _record(self, h, stage: str, payload: Any) -> dict:
        hyp = _as_hypothesis(h)
        if isinstance(payload, Mapping):
            payload_obj: Any = dict(payload)
        else:
            payload_obj = payload
        return {
            "ts": _now_str(),
            "ledger_version": LEDGER_VERSION,
            "stage": str(stage),
            "hypothesis_id": hyp.hypothesis_id,
            "status": hyp.status,
            "thesis": hyp.thesis,
            "mechanism": hyp.mechanism,
            "direction": int(hyp.direction),
            "expression": hyp.expression,
            "required_fields": list(hyp.required_fields),
            "source": hyp.source,
            "created_at": hyp.created_at,
            "payload": payload_obj,
        }

    def append(self, h, stage: str, payload: Any = None) -> bool:
        """追加一条记录。

        Returns:
            True = 已落盘; False = 写入失败 (**不抛异常**), 原因见 self.last_error。
        """
        try:
            rec = self._record(h, stage, payload)
            line = json.dumps(rec, ensure_ascii=False, default=str)
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except Exception as e:      # noqa: BLE001 —— 留痕失败不得拖垮主链路 (本仓纪律)
            self.last_error = f"{type(e).__name__}: {e}"
            self.write_failures += 1
            return False

    # -- 读 --------------------------------------------------------
    def read_all(self) -> list[dict]:
        """读回全部记录。**坏行只计数不抛**: 一行坏 JSON 不该让整本账读不出来。"""
        out: list[dict] = []
        self.bad_lines = 0
        if not os.path.exists(self.path):
            return out
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:   # noqa: BLE001
                        self.bad_lines += 1
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
                    else:
                        self.bad_lines += 1
        except OSError as e:
            self.last_error = f"{type(e).__name__}: {e}"
        return out

    def stats(self) -> dict:
        """账本统计 (CLI --list 用)。"""
        recs = self.read_all()
        by_stage: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_source: dict[str, int] = {}
        ids: set = set()
        ts_list: list[str] = []
        for r in recs:
            by_stage[r.get("stage", "?")] = by_stage.get(r.get("stage", "?"), 0) + 1
            by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
            by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
            if r.get("hypothesis_id"):
                ids.add(r["hypothesis_id"])
            if r.get("ts"):
                ts_list.append(str(r["ts"]))
        return {
            "path": self.path,
            "exists": os.path.exists(self.path),
            "total": len(recs),
            "bad_lines": self.bad_lines,
            "by_stage": by_stage,
            "by_status": by_status,
            "by_source": by_source,
            "unique_hypotheses": len(ids),
            "first_ts": min(ts_list) if ts_list else None,
            "last_ts": max(ts_list) if ts_list else None,
        }


# ===================================================================
# 9. LLM 提案 (注入式 llm_call, 不直连任何 SDK / 不联网)
# ===================================================================

#: 默认 prompt 模板。占位符用**字面替换** ({context} / {n}) 而不是 str.format ——
#: 模板里必然出现 JSON 示例的花括号, str.format 会当场炸。
DEFAULT_PROMPT_TEMPLATE = """你是 A 股量化研究员。请提出 {n} 个**可证伪**的因子假设。

要求 (逐条都是硬要求):
1. 每个假设必须先有经济逻辑, 再谈数据: thesis 一句话说清"为什么这个量能预测未来收益";
2. mechanism 给出**因果链条** (资金流/信息扩散/行为偏差/制度约束/会计传导...),
   不接受"它与收益相关"这类同义反复;
3. direction: 预期因子值与未来收益的秩相关符号, 只能是 +1 或 -1;
4. expression: 可计算的因子表达式。语法为函数式前缀表达式, 例:
   div(sub(ret_20, ret_5), ts_std(ret_20, 20))   cs_rank(neg(ret_20))
   可用算子:
     add/sub/mul/div, neg/abs/sqrt/log/inv/square/cube/sign/clip,
     cs_rank/cs_demean/cs_zscore (截面),
     ts_mean/ts_std/ts_rank/ts_delta/ts_zscore/ts_max/ts_min (滚动, 第二参为正整数窗口)
   字段只能取自下方 context 里列出的可用字段。
   **严禁未来函数**: 不允许 shift(-n)、未来窗口、future_*/next_* 字段、t+N 时点。
5. required_fields: 表达式用到的字段名列表。

只输出 JSON, 不要任何 Markdown 围栏、不要额外解释:
{"hypotheses": [{"thesis": "...", "mechanism": "...", "direction": 1,
  "expression": "neg(ret_20)", "required_fields": ["ret_20"]}]}

[context]
{context}
"""


def render_prompt(context: Any, n: int = 5, template: Optional[Any] = None) -> str:
    """把 context 与数量渲染成给模型的 prompt。

    prompt 中**显式要求**模型给出 thesis/mechanism/direction/expression/
    required_fields 五个字段的 JSON (见 DEFAULT_PROMPT_TEMPLATE)。

    Args:
        context: 字符串, 或可 JSON 序列化的对象 (可用字段、数据概况、已有因子表现…)。
        n: 请求的候选数量。
        template: None = 内置模板; str = 自定义模板 (可含 {context}/{n} 占位符,
            用字面替换, 因此模板里的 JSON 花括号是安全的; 若模板不含 {context},
            会把 context 追加到末尾, 以免调用方以为上下文已经传进去了);
            或 callable(context, n) -> str。
    """
    if template is None:
        tpl: Any = DEFAULT_PROMPT_TEMPLATE
    else:
        tpl = template
    if isinstance(context, str):
        ctx_text = context
    else:
        ctx_text = json.dumps(context, ensure_ascii=False, indent=2, default=str)
    if callable(tpl) and not isinstance(tpl, str):
        return str(tpl(context, n))
    text = str(tpl)
    if "{n}" in text:
        text = text.replace("{n}", str(int(n)))
    if "{context}" in text:
        text = text.replace("{context}", ctx_text)
    else:
        text = text + "\n\n[context]\n" + ctx_text
    return text


def _balanced_slice(text: str, start: int) -> Optional[str]:
    """从 start 处的 { 或 [ 起, 取到配平的闭合处 (字符串内的括号不计)。"""
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
            if not stack:
                return text[start:i + 1]
    return None


def _extract_json(raw: str) -> Any:
    """从模型输出里抠出第一个可解析的 JSON (容忍 ``` 围栏与前后废话)。失败返回 None。"""
    text = raw.strip()
    text = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    candidates = [text]
    for m in re.finditer(r"[\[{]", text):
        sub = _balanced_slice(text, m.start())
        if sub and sub not in candidates:
            candidates.append(sub)
    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):   # 第二次: 去掉尾随逗号
            try:
                return json.loads(attempt)
            except Exception:      # noqa: BLE001 —— 逐个候选试, 全失败才返回 None
                continue
    return None


def parse_llm_output(raw: str, source: str = "llm") -> dict:
    """把模型原始输出解析成 Hypothesis 列表。

    `propose_from_llm` 的底层函数, 额外暴露**逐条**失败原因, 便于留痕与测试。

    Returns:
        {'hypotheses': [Hypothesis,...], 'errors': [{'index','kind','message'},...],
         'warnings': [...], 'found': int, 'raw_len': int}

        单条非法 (缺 thesis/expression、非 dict) => 记入 errors 并跳过, 不静默丢弃记录;
        整个输出无法解析 => hypotheses 为空 (由 propose_from_llm 负责吵)。
    """
    errors: list[dict] = []
    warnings_: list[dict] = []
    obj = _extract_json(str(raw))
    if obj is None:
        errors.append({"index": -1, "kind": "invalid_json",
                       "message": "输出中找不到可解析的 JSON"})
        return {"hypotheses": [], "errors": errors, "warnings": warnings_,
                "found": 0, "raw_len": len(str(raw))}

    if isinstance(obj, Mapping):
        items = obj.get("hypotheses") or obj.get("candidates") or obj.get("factors")
        if items is None:
            items = [obj]
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    if not isinstance(items, list):
        errors.append({"index": -1, "kind": "bad_shape",
                       "message": "'hypotheses' 字段不是列表"})
        items = []

    hyps: list[Hypothesis] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            errors.append({"index": i, "kind": "not_object",
                           "message": f"第 {i} 条不是 JSON 对象: {type(item).__name__}"})
            continue
        thesis = item.get("thesis") or item.get("hypothesis") or ""
        expression = item.get("expression") or item.get("expr") or item.get("formula") or ""
        if not str(thesis).strip():
            errors.append({"index": i, "kind": "missing_thesis",
                           "message": f"第 {i} 条缺少 thesis"})
            continue
        if not str(expression).strip():
            errors.append({"index": i, "kind": "missing_expression",
                           "message": f"第 {i} 条缺少 expression"})
            continue
        rf = item.get("required_fields")
        if not rf:
            try:
                rf = expression_fields(str(expression))
            except ExpressionError:
                rf = []
            warnings_.append({"index": i, "kind": "inferred_required_fields",
                              "message": f"第 {i} 条未给 required_fields, 由表达式推导 {rf}"})
        elif isinstance(rf, str):
            rf = [s.strip() for s in re.split(r"[,;，、]", rf) if s.strip()]
        hyp = Hypothesis(
            thesis=str(thesis),
            mechanism=str(item.get("mechanism") or item.get("rationale") or ""),
            direction=item.get("direction", item.get("expected_ic_sign", 1)),
            expression=str(expression),
            required_fields=list(rf),
            source=source,
        )
        hyps.append(hyp)
    return {"hypotheses": hyps, "errors": errors, "warnings": warnings_,
            "found": len(items), "raw_len": len(str(raw))}


def propose_from_llm(prompt_template: Optional[Any] = None,
                     context: Any = None,
                     llm_call: Optional[Callable[[str], str]] = None,
                     n: int = 5) -> list[Hypothesis]:
    """让模型提候选假设, 并解析成 Hypothesis 列表。

    LLM 调用是**注入式**的: 本模块不 import 任何 SDK、不联网, 只调用
    `llm_call(prompt) -> str`。这样单测可以用 stub, 生产可以把 ai_factor_lab /
    llm_commentary 里已有的调用封装 (含重试/成本控制/脱敏) 直接塞进来。

    Args:
        prompt_template: None = 内置模板; str = 自定义模板 (含 {context}/{n} 占位符,
            字面替换); callable(context, n) -> str。
        context: 传给模型的市场/字段上下文 (见 render_prompt)。
        llm_call: 必须可调用; 非可调用 => TypeError。
        n: 请求的候选数量。模型返回多于/少于 n 都按实际条数处理 (不截断, 不补空)。

    Returns:
        list[Hypothesis] (source='llm'), **至少一条**。

    Raises:
        TypeError: llm_call 不可调用。
        HypothesisParseError: **响亮失败** —— llm_call 返回非字符串/空串/模型没给任何
            可用假设 (含输出不是 JSON、JSON 结构不对、每条都缺 thesis 或 expression)。
            **本函数从不静默返回 `[]`**: 空列表会让上游以为"模型今天没想法", 而真相
            往往是 prompt 崩了或模型跑偏了 —— 这两种情况的处置完全不同。
            异常的 .raw (原始输出前 500 字) 与 .errors (逐条原因) 供留痕。
            单条非法但另有合法条目时**不抛**, 非法条目记在 parse_llm_output 的 errors 里。
    """
    if not callable(llm_call):
        raise TypeError(f"llm_call 必须是可调用对象, 得到 {type(llm_call).__name__}")
    prompt = render_prompt(context, n, template=prompt_template)
    raw = llm_call(prompt)
    if not isinstance(raw, str) or not raw.strip():
        raise HypothesisParseError(
            f"llm_call 未返回非空字符串 (得到 {type(raw).__name__})",
            raw="" if raw is None else str(raw))
    parsed = parse_llm_output(raw, source="llm")
    if not parsed["hypotheses"]:
        raise HypothesisParseError(
            f"无法从模型输出解析出任何假设 (found={parsed['found']}, "
            f"errors={len(parsed['errors'])} 条)",
            raw=raw, errors=parsed["errors"])
    return parsed["hypotheses"]


# ===================================================================
# 10. selftest / CLI
# ===================================================================

def _selftest_data(seed: int = 20260101, t: int = 60, s: int = 20) -> dict:
    """自检用的合成面板 (内存构造, 不读任何真实数据文件)。"""
    rng = np.random.default_rng(seed)
    ret20 = rng.normal(0.0, 1.0, (t, s))
    target = 0.8 * ret20 + rng.normal(0.0, 0.4, (t, s))    # ret_20 与未来收益正相关
    return {
        "ret_20": ret20,
        "ret_5": rng.normal(0.0, 1.0, (t, s)),
        "turnover": np.abs(rng.normal(3.0, 1.0, (t, s))),
        DEFAULT_TARGET_KEY: target,
    }


_SELFTEST_THESIS = {
    "momentum": "过去20日累计涨幅高的股票, 未来5日收益更高 (动量延续)",
    "reversal": "过去20日累计涨幅高的股票, 未来5日收益更低 (短期反转)",
}
_SELFTEST_MECH = {
    "momentum": ("资金流与信息扩散具有惯性: 上涨吸引增量资金与关注度, 在主题行情里形成"
                 "短期正反馈, 使强势股在未来数日继续跑赢。"),
    "reversal": ("短期涨幅过大后, 获利盘了结叠加流动性冲击使价格回落; 行为金融的过度反应"
                 "假说认为散户追高造成短期超买, 随后均值回复。"),
}


def _selftest_hypotheses_payload() -> dict:
    """自检里 stub llm_call 返回的"模型输出" (故意混入 1 条未来函数 + 1 条方向写反)。"""
    return {"hypotheses": [
        {"thesis": _SELFTEST_THESIS["momentum"],
         "mechanism": _SELFTEST_MECH["momentum"],
         "direction": "positive", "expression": "ret_20", "required_fields": ["ret_20"]},
        {"thesis": _SELFTEST_THESIS["reversal"],
         "mechanism": _SELFTEST_MECH["reversal"],
         "direction": -1, "expression": "neg(ret_20)", "required_fields": ["ret_20"]},
        # 方向与数据相反: 应被 evidence 阶段拒掉 (绝不进收益评估)
        {"thesis": _SELFTEST_THESIS["reversal"] + " (方向标注为负, 但表达式未取负)",
         "mechanism": _SELFTEST_MECH["reversal"],
         "direction": -1, "expression": "ret_20", "required_fields": ["ret_20"]},
        # 未来函数: 应被 lint 阶段拒掉
        {"thesis": "用未来一期的收益率变化预测未来收益 (前视偏差样例)",
         "mechanism": "这条假设是故意写错的样例: 它引用了未来数据, 必须被静态防线拦下。",
         "direction": 1, "expression": "ts_delta(ret_20, -1)", "required_fields": ["ret_20"]},
    ]}


def _selftest(ledger_path: Optional[str] = None, seed: int = 20260101) -> int:
    """用内置 stub llm_call 跑一遍完整流水线并打印 (不联网、不读真实数据)。

    ledger_path=None => 写**仓库内**的临时目录 (自检不该污染 data/factor_hypotheses.jsonl;
    也不写系统临时目录 —— 某些受限环境允许在 %TEMP% 建目录却拒绝写文件, 那会让自检
    的账本步骤假失败)。目录用完尽力删除, 删不掉也不影响自检结论。
    """
    print("=" * 68)
    print("factor_hypothesis 自检 (--selftest): stub llm_call + 合成面板")
    print("=" * 68)
    data = _selftest_data(seed=seed)
    avail = sorted(k for k in data if k != DEFAULT_TARGET_KEY)
    print(f"合成面板: {np.asarray(data['ret_20']).shape[0]} 期 × "
          f"{np.asarray(data['ret_20']).shape[1]} 只; 可用字段 {avail}")

    prompts: list[str] = []

    def stub_llm_call(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(_selftest_hypotheses_payload(), ensure_ascii=False)

    hyps = propose_from_llm(context={"available_fields": avail,
                                     "target": "未来5日收益 (fwd5)"},
                            llm_call=stub_llm_call, n=4)
    print(f"\n[1] render_prompt/propose_from_llm: prompt {len(prompts[0])} 字符, "
          f"解析出 {len(hyps)} 条假设")
    for h in hyps:
        print(f"    - {h.hypothesis_id}  dir={h.direction:+d}  {h.expression}")

    print("\n[2] 分段阈值显式传入 (生产接线处就该这么做): "
          "min_sign_consistency=0.6, min_n_obs=200, min_coverage=0.6")

    tmp_dir = None
    if ledger_path is None:
        tmp_dir = _make_selftest_tmpdir()
        ledger_path = os.path.join(tmp_dir, "factor_hypotheses.jsonl")
    ledger = HypothesisLedger(path=ledger_path)
    calls: list[str] = []

    def stub_return_evaluator(h, d):
        """占位收益回调: 恒通过, 只用来演示"仅通过 evidence 者才被调用"。

        生产接线处应换成 factor_mine.evaluator.evaluate / ai_factor_lab.evaluate_hypothesis,
        **并在那里显式传入自己的 IC/ICIR/单调性门槛**。
        """
        calls.append(h.hypothesis_id)
        return {"ok": True, "verdict": "STUB", "note": "selftest 占位回调 (未做真实收益评估)"}

    r = validate_batch(hyps, data, avail, ledger=ledger,
                       return_evaluator=stub_return_evaluator,
                       n_segments=5, min_n_obs=200, min_coverage=0.6,
                       min_sign_consistency=0.6)
    print(f"\n[3] validate_batch: proposed={r['proposed']} "
          f"lint_rejected={r['lint_rejected']} evidence_rejected={r['evidence_rejected']} "
          f"verified={r['verified']} falsified={r['falsified']}")
    for rec in r["records"]:
        why = ""
        if rec["lint"] and not rec["lint"]["ok"]:
            why = rec["lint"]["reasons"][0]
        elif rec["evidence"] and not rec["evidence"]["ok"]:
            why = rec["evidence"]["reasons"][0]
        ev = rec["evidence"] or {}
        print(f"    {rec['hypothesis_id']}  {rec['status']:<10} {rec['stage']:<17} "
              f"rank_ic={ev.get('rank_ic')} sign_consistency={ev.get('sign_consistency')} "
              f"{why}")
    print(f"\n[4] 流水线纪律: 收益评估回调被调用 {r['return_eval_calls']} 次, "
          f"evidence 通过 {r['evidence_passed']} 条 "
          f"-> {'一致 ✓' if r['return_eval_calls'] == r['evidence_passed'] else '不一致 ✗'}")

    st = ledger.stats()
    print(f"\n[5] 账本 {st['path']}")
    print(f"    总记录 {st['total']} 条, 假设 {st['unique_hypotheses']} 个, "
          f"坏行 {st['bad_lines']}, 按阶段 {st['by_stage']}")
    if tmp_dir is not None:
        # 尽力清理; 删不掉也不能把自检结论带崩 (Windows 上偶发占用)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:      # noqa: BLE001
            pass
    ok = (r["return_eval_calls"] == r["evidence_passed"] == len(calls)
          and r["lint_rejected"] >= 1 and r["evidence_rejected"] >= 1
          and r["verified"] >= 1 and st["total"] >= r["proposed"] * 2
          and r["ledger_write_failures"] == 0)
    print("\n自检结论: " + ("通过 ✓ (lint/evidence 各拦下至少 1 条; 收益回调只对通过者调用)"
                          if ok else "失败 ✗ (流水线纪律未满足)"))
    return 0 if ok else 1


def _make_selftest_tmpdir() -> str:
    """给自检准备一个**可写**的一次性目录 (优先仓库内, 系统临时目录兜底)。

    刻意不用 tempfile.mkdtemp: 它会把目录 chmod 到 0o700, 而在某些受限执行环境里,
    被 chmod 过的目录随后会拒绝一切写入 —— 自检的账本步骤就会"假失败"。所以这里用
    普通 mkdir + 唯一目录名。
    """
    name = "_tmp_factor_hyp_selftest_" + uuid.uuid4().hex[:8]
    try:
        base = os.path.join(_REPO, "data")
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, name)
        os.makedirs(path, exist_ok=False)
        return path
    except OSError:
        path = os.path.join(tempfile.gettempdir(), name)
        os.makedirs(path, exist_ok=False)
        return path


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口。

    用法:
        python src/factor_hypothesis.py --list                # 打印账本统计
        python src/factor_hypothesis.py --selftest            # stub llm_call 跑通全流水线
        python src/factor_hypothesis.py --list --path <p>     # 指定账本路径
    """
    ap = argparse.ArgumentParser(
        prog="factor_hypothesis",
        description="假设级证据驱动的因子挖掘 (lint -> evidence -> 收益评估)")
    ap.add_argument("--list", action="store_true", help="打印账本统计")
    ap.add_argument("--selftest", action="store_true",
                    help="用内置 stub llm_call + 合成面板跑一遍完整流水线并打印")
    ap.add_argument("--path", default=None, help="账本路径 (默认 data/factor_hypotheses.jsonl)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        return _selftest(ledger_path=args.path)

    if args.list:
        ledger = HypothesisLedger(path=args.path)
        st = ledger.stats()
        print(f"假设账本: {st['path']} (存在={st['exists']})")
        print(f"  总记录 {st['total']} 条; 坏行 {st['bad_lines']} 行; "
              f"不同假设 {st['unique_hypotheses']} 个")
        print(f"  按阶段: {st['by_stage']}")
        print(f"  按状态: {st['by_status']}")
        print(f"  按来源: {st['by_source']}")
        print(f"  时间范围: {st['first_ts']} ~ {st['last_ts']}")
        if st["total"] == 0:
            print("  (账本为空: 还没有假设被登记 —— 先跑 --selftest 看流水线, 或接入 propose_from_llm)")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

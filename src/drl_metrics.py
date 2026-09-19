# -*- coding: utf-8 -*-
"""DRL 训练过程指标采集与汇总（DRL-2）—— 轻量模块, **不依赖 torch / gymnasium / SB3**.

为什么需要它
    DRL-2 的现状是「学习中断指标**结构性不可验证**」: 实测 `train_meta.json`（16 份样本）
    共 23 个键, 其中**没有** entropy / policy_loss / value_loss / grad_norm /
    实际训练步数 / 训练时长。设计要求的「学习中」五项检查（步数 / 损失收敛 / 熵值 /
    梯度范数 / 时长）因此**无从验证** —— 不是"检查没通过", 而是"根本没有数据可查"。

本模块做两件事
    ① **采集**: `MetricsHistory` 逐次迭代累积指标序列（`CustomPPO.train()` 末尾快照）。
    ② **汇总**: `summarize()` 把序列压成 `{n, first, last, min, max, mean, delta, slope}`
       —— 把"收敛性"变成**可查证的数值**, 而不是一个布尔断言。

关于「是否收敛」——**本模块刻意不判定**（METHOD-1）
    判定"损失是否已收敛"必然要一个阈值（如 `|slope| < 1e-4` 或 `rel_delta < 5%`）。
    按项目已固化的原则（见 `docs/drl-learning-verification.md` §📌）:
      「边界/阈值的设定必须基于**该值发生时的下游表现**证据, 而非**该值的分布范围**」
    故本模块**只输出数值**, 并显式标注 `threshold_applied: False`;
    `converged` 之类的布尔量**一律不产出**。待积累足够多天的
    (指标序列 → 次日/次周下游表现) 配对数据后, 再回头定阈值。

与 DRL-4 的关系
    DRL-4 降级链的触发条件全是**结构性**的（训练失败 / 文件不可用 / 无有效版本）,
    刻意不含统计阈值。本模块补上的是"**结构性可验证性**": 让"学习中"从
    "无数据"变成"有数据", 从而为将来的阈值标定（以及 DRL-3 的学习后检查）提供证据基础。
"""
from __future__ import annotations

import math

#: 规范化指标名 -> SB3 logger 名（`train()` 内 `logger.record` 用的键）。
#: `grad_norm` 不在 logger 里: 它由 `CustomPPO.train()` 在 `clip_grad_norm_` 处单独采集。
METRIC_LOGGER_KEYS = {
    "entropy_loss": "train/entropy_loss",
    "policy_loss": "train/policy_gradient_loss",
    "value_loss": "train/value_loss",
    "approx_kl": "train/approx_kl",
    "clip_fraction": "train/clip_fraction",
    "explained_variance": "train/explained_variance",
    "std": "train/std",
    "ent_coef": "train/adapted_ent_coef",
    "n_updates": "train/n_updates",
}
#: 由 `train()` 自行采集（不在 logger 中）的指标名。
EXTRA_METRIC_KEYS = ("grad_norm",)
#: `train_meta["train_metrics"]` 中保留的完整序列点数上限（防参数调大后 train_meta 膨胀）。
SERIES_CAP = 2000


def all_metric_keys() -> "list[str]":
    return list(METRIC_LOGGER_KEYS) + list(EXTRA_METRIC_KEYS)


class MetricsHistory:
    """逐次迭代累积指标序列。`record()` 对缺失/非有限值**跳过**（不写 NaN/None 占位）。

    刻意不做任何判定、不设阈值 —— 它只负责"把数留下来"。
    """

    def __init__(self) -> None:
        self._h: "dict[str, list[float]]" = {k: [] for k in all_metric_keys()}
        self.skipped: "dict[str, int]" = {}
        self.n_records = 0

    def record(self, values: dict) -> None:
        """把一次迭代的 `{指标名: 数值}` 追加进序列。缺的键记一次 skip。"""
        for k in self._h:
            v = (values or {}).get(k)
            fv = _finite(v)
            if fv is None:
                self.skipped[k] = self.skipped.get(k, 0) + 1
                continue
            self._h[k].append(fv)
        self.n_records += 1

    @property
    def history(self) -> "dict[str, list[float]]":
        return {k: list(v) for k, v in self._h.items()}

    def series_for_meta(self, cap: int = SERIES_CAP) -> dict:
        """落盘用序列: 只保留**非空**指标的最近 `cap` 个点。"""
        out = {}
        for k, v in self._h.items():
            if v:
                out[k] = [round(float(x), 6) for x in v[-cap:]]
        return out

    def summary(self) -> dict:
        return summarize(self._h)


def _finite(v):
    """把任意值收敛成 float, 非有限(NaN/Inf)/非数值/布尔 -> None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _slope(xs: "list[float]") -> "float | None":
    """最小二乘斜率（相对迭代序号）。点数 < 2 时为 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx = (n - 1) / 2.0
    my = sum(xs) / n
    num = sum((i - mx) * (x - my) for i, x in enumerate(xs))
    den = sum((i - mx) ** 2 for i in range(n))
    return (num / den) if den else None


def summarize(history: dict) -> dict:
    """逐指标汇总成**只含数值**的统计量。**不产出任何布尔判定**（见模块 docstring）。"""
    out: "dict[str, dict]" = {}
    for k, series in (history or {}).items():
        xs = [f for f in (_finite(v) for v in (series or [])) if f is not None]
        if not xs:
            continue
        n = len(xs)
        first, last = xs[0], xs[-1]
        mean = sum(xs) / n
        delta = last - first
        out[k] = {
            "n": n,
            "first": round(first, 6),
            "last": round(last, 6),
            "min": round(min(xs), 6),
            "max": round(max(xs), 6),
            "mean": round(mean, 6),
            "delta": round(delta, 6),
            # 相对变化: 分母用 |first|, 近 0 时退化为 None（避免除零放大成无意义的大数）
            "rel_delta": (round(delta / abs(first), 6) if abs(first) > 1e-12 else None),
            "slope": (None if (s := _slope(xs)) is None else round(s, 9)),
        }
    return out


def summarize_run(model=None, history: MetricsHistory | None = None,
                  duration_s: "float | None" = None,
                  requested_timesteps: "int | None" = None,
                  extra: "dict | None" = None) -> dict:
    """汇总一次训练运行 —— 直接挂到 `train_meta["train_metrics"]`。

    关键点: `actual_timesteps` 取自 `model.num_timesteps`（**实际执行步数**）,
    与 `requested_timesteps`（配置值）分开记录 —— 原先 `train_meta.total_timesteps`
    是配置值, 二者混淆正是 DRL-2 里"步数无从验证"的根源之一。
    """
    hist = history if history is not None else MetricsHistory()
    actual = None
    try:
        if model is not None:
            actual = int(getattr(model, "num_timesteps", 0) or 0)
    except Exception:  # noqa: BLE001
        actual = None

    rec = {
        "schema": 1,
        "threshold_applied": False,   # 明确: 本批次不施加任何"是否收敛"的阈值
        "note": ("只记录数值, 不判定收敛/不设阈值"
                 "(METHOD-1: 阈值须基于下游表现而非分布范围)"),
        "n_records": int(getattr(hist, "n_records", 0)),
        "requested_timesteps": (int(requested_timesteps)
                                if requested_timesteps is not None else None),
        "actual_timesteps": actual,
        "duration_s": (round(float(duration_s), 3) if duration_s is not None else None),
        "summary": hist.summary(),
        "series": hist.series_for_meta(),
    }
    if actual is not None and requested_timesteps:
        rec["timesteps_shortfall"] = int(requested_timesteps) - actual
    if getattr(hist, "skipped", None):
        rec["skipped_counts"] = dict(hist.skipped)
    if extra:
        rec.update(extra)
    return rec


def values_from_logger(name_to_value: dict) -> dict:
    """从 SB3 `logger.name_to_value` 取本轮指标值（缺的键不出现）。"""
    src = name_to_value or {}
    out = {}
    for norm, lk in METRIC_LOGGER_KEYS.items():
        f = _finite(src.get(lk))
        if f is not None:
            out[norm] = f
    return out


__all__ = ["METRIC_LOGGER_KEYS", "EXTRA_METRIC_KEYS", "SERIES_CAP",
           "all_metric_keys", "MetricsHistory", "summarize", "summarize_run",
           "values_from_logger"]

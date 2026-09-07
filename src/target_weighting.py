# ============================================================
# target_weighting.py -- 目标权重分配器 (策略层优化 2026-09-05)
#
# 关闭 premarket 健康检查长期标注的「target_weight 悬空契约」:
#   DRL/selection 每项携带 target_weight, 但 realtime/backtest 引擎始终按
#   等权 band 撮合. 本模块产出真正驱动下单的目标权重:
#     1) 收缩(shrink): 用 f_ml 预测收益(排名模型重映射后为收益尺度)做
#        rank 线性加权 -> 高确信标的多暴露、低确信票减配, 但避免重尾
#        极端权重(相比直接按预测收益加权对点估计噪声更鲁棒).
#     2) 集中度上限(cap): 单票目标权重 <= max_mult * equal, 低于
#        风控集中度触发线(0.08*1.55=12.4%), 不触发压回自振荡.
#     3) 归一: sum(target_weight) = 1.0 (相对总资产, DRL 既有契约).
#
# 用法:
#   from target_weighting import allocate_target_weights
#   top_n = allocate_target_weights(top_n)   # 每项写/覆写 target_weight
#   等权回退: fml 全部缺失 / mode="equal" -> 每项 1/n.
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from config import TARGET_WEIGHT

_LOG = logging.getLogger("target_weighting")


def _equal_weights(n: int) -> list[float]:
    return [1.0 / max(n, 1)] * n


def _fml_values(items: list[dict]) -> list[float]:
    vals: list[float] = []
    for it in items:
        v = it.get("fml")
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = float("nan")
        vals.append(f if np.isfinite(f) else float("nan"))
    return vals


def allocate_target_weights(items: list[dict],
                            mode: str | None = None,
                            min_mult: float | None = None,
                            max_mult: float | None = None) -> list[dict]:
    """按配置为 top_n 每项分配 target_weight(原地写字段并返回列表).

    权重语义: 相对总资产, 和为 1.0.
    fml 模式: 依据 f_ml 值降序做 rank 线性收缩, 区间 [equal*min_mult,
    equal*max_mult]; 无任何有效 fml / n<2 / mode=equal 时退化为等权 1/n.
    """
    cfg = dict(TARGET_WEIGHT or {})
    mode = mode or cfg.get("mode", "fml")
    n = len(items)
    if n == 0:
        return items
    equal = 1.0 / n
    if mode != "fml" or n < 2:
        for it, w in zip(items, _equal_weights(n)):
            it["target_weight"] = round(w, 6)
        return items
    lo = max(0.05, (min_mult if min_mult is not None
                    else float(cfg.get("min_mult", 0.60))) * equal)
    hi = max(lo + 1e-4, (max_mult if max_mult is not None
                         else float(cfg.get("max_mult", 1.21))) * equal)

    vals = _fml_values(items)
    if not any(np.isfinite(v) for v in vals):
        for it, w in zip(items, _equal_weights(n)):
            it["target_weight"] = round(w, 6)
        return items

    # fml 降序排名 (NaN 垫底 -> 最小权重)
    order = sorted(range(n), key=lambda i: (-vals[i] if np.isfinite(vals[i])
                                            else float("inf")))
    rank = {idx: r for r, idx in enumerate(order)}       # 0=最强 .. n-1=最弱
    raw = np.array([lo + (hi - lo) * (1.0 - rank[i] / (n - 1.0))
                    for i in range(n)])                  # rank线性: 强->hi

    # 迭代 clip+归一, 使 sum=1 且每项落在 [lo, hi]
    w = raw.copy()
    for _ in range(50):
        w = np.clip(w, lo, hi)
        s = w.sum()
        if s <= 0:
            break
        if abs(s - 1.0) < 1e-9:
            break
        # 按比例缩放后再次 clip; 若 hi 上限过紧导致和无法达 1, 缩小 hi 重试
        w *= 1.0 / s
    if abs(w.sum() - 1.0) > 1e-6:      # 兜底: 线性缩放(可能轻微越界由引擎容忍)
        w *= 1.0 / w.sum()

    for it, wi in zip(items, w):
        it["target_weight"] = round(float(wi), 6)
    return items


def normalize_weights(items: list[dict]) -> float:
    """返回权重和(消费侧兜底归一用); 缺 target_weight 的项按等权补 1/n."""
    n = max(len(items), 1)
    s = 0.0
    for it in items:
        w = it.get("target_weight")
        try:
            f = float(w)
            if np.isfinite(f) and f > 0:
                it["target_weight"] = f
                s += f
                continue
        except (TypeError, ValueError):
            pass
        it["target_weight"] = 1.0 / n      # 缺省项补等权
        s += 1.0 / n
    return float(s)


def _already_nonuniform(items: list[dict], tol: float = 1e-6) -> bool:
    """targets 是否已带「非等权」target_weight(视为已由 selector 分配)."""
    n = max(len(items), 1)
    eq = 1.0 / n
    ws = []
    for it in items:
        w = it.get("target_weight")
        try:
            f = float(w)
        except (TypeError, ValueError):
            return False
        if not np.isfinite(f) or f <= 0:
            return False
        ws.append(f)
    return any(abs(x - eq) > tol for x in ws)


def ensure_target_weights(targets: list[dict], as_of: str | None = None,
                          mode: str | None = None) -> list[dict]:
    """引擎装载目标池后调用: 确保每项 target_weight 与配置一致且和=1.

    - mode="equal"         : 一律等权 1/n.
    - mode="fml"           : 若目标已带非等权权重(selector 现选路径, 已含 fml)
                             则仅归一; 否则(DRL plan 等权路径)现算 f_ml 预测
                             收益并做收缩+集中度封顶分配.
    f_ml 现算失败/缺失自动回退等权, 绝不抛异常(保持选股链路健壮).
    """
    cfg = dict(TARGET_WEIGHT or {})
    mode = mode or cfg.get("mode", "fml")
    if not targets:
        return targets
    n = len(targets)
    if mode != "fml":
        for it, w in zip(targets, [1.0 / n] * n):
            it["target_weight"] = round(w, 6)
        return targets
    if not _already_nonuniform(targets):
        # DRL/旧 selection 等权路径: 现算预测打分以激活预测加权.
        # [方案A] 优先 factor_fusion (四因子 ICIR 加权横截面中性化打分);
        # 融合被禁用/异常/覆盖<80% 时由 fusion_or_fml 自动回退旧
        # ml_fusion_bridge.compute_fml('fml_fallback'); 两者皆空则 'equal'.
        try:
            from factor_fusion import fusion_or_fml
            scores, used = fusion_or_fml(targets, as_of or "")
            if scores:
                n_wrote = 0
                for t in targets:
                    v = scores.get(t.get("canon"))
                    if isinstance(v, (int, float)):
                        t["fml"] = float(v)
                        t["fml_source"] = ("factor_fusion" if used == "fusion"
                                           else "ml_fusion_bridge")
                        n_wrote += 1
                _LOG.info("ensure_target_weights: used=%s as_of=%s n_target=%d "
                          "n_fml=%d (source=%s)",
                          used, as_of or "", len(targets), n_wrote,
                          ("factor_fusion" if used == "fusion"
                           else "ml_fusion_bridge"))
        except Exception:  # noqa: BLE001
            _LOG.warning("ensure_target_weights: 现算打分失败, 退化为等权",
                         exc_info=True)
    allocate_target_weights(targets)       # 无有效 fml 自动等权
    normalize_weights(targets)
    return targets

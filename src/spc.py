# ============================================================
# spc.py -- SPC (Statistical Process Control) + CC (Control Chart)
#
# 目标: 把策略收益、Sharpe、IC、最大回撤等关键指标纳入 Shewhart 控制图 +
#       Western Electric Rules + CUSUM, 自动检测漂移/异常, 输出分级告警.
#
# 分级:
#   P0 红色  紧急: 关键控制图 3σ 越界 / CUSUM 触发 / Sharpe 跌破 LSL
#   P1 黄色  警告: 8 点同侧 / 4 点同 ±1σ  / Sharpe 跌出 -1σ
#   P2 蓝色  关注: 趋势类 (漂移 >1σ but 未越界) / IC 均值低于阈值
#   P3 灰色  信息: trade count 异常 / 其它
#
# 输入: pd.Series (按时间排序的指标)
# 输出: {
#   "indicator": str,
#   "level": "P0"|"P1"|"P2"|"P3"|"OK",
#   "mean": float,
#   "sigma": float,
#   "ucl": /lcl: float,
#   "n": int,
#   "last": float,
#   "violations": [...],   # 触发的规则列表
#   "message": str,
# }
# ============================================================

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


# 告警级别常量
P0 = "P0"  # 红色 紧急
P1 = "P1"  # 黄色 警告
P2 = "P2"  # 蓝色 关注
P3 = "P3"  # 灰色 信息
OK = "OK"


@dataclass
class IndicatorCfg:
    """单个指标的控制图配置."""
    name: str
    unit: str = ""
    # 中心线 / 上下控制限计算窗口 (默认 rolling)
    window: int = 20
    # 是否单边 (如最大回撤只有下界)
    one_sided: str = "two"  # "two" / "lower" / "upper"
    # 手工指定 sigma 倍数 (默认 3σ -> 99.73%)
    sigma: float = 3.0
    # 自定义 LSL/USL (绝对阈值, 例如最大回撤 < -10%)
    lsl: float | None = None
    usl: float | None = None
    # 触发级别 (默认 P0), 若只是软指标可降到 P1
    trigger_level: str = P0
    # 触顶检测: 当指标长时间贴在 LSL/USL 上 (>ceil_window 天), 说明阈值可能触顶,
    # 此时即使 LSL 没有被"突破", 也应作为独立告警维度 (避免"卡死在 -10%"导致均值偏移误报)
    ceil_window: int = 5           # 触顶检测窗口 (默认 5 个点)
    ceil_min_hits: int = 3          # 触顶触发的最小命中点数 (>= ceil_min_hits 即告警)
    ceil_tolerance: float = 1e-6    # 触顶容差 (与 LSL/USL 距离 <= 此值即视为贴在阈值)
    ceil_trigger_level: str = P1    # 触顶告警等级 (默认 P1, 视场景可降为 P2)


# ============================================================
# 控制限计算 (Shewhart)
# ============================================================
def control_limits(series: pd.Series, window: int = 20, sigma: float = 3.0) -> dict:
    """rolling mean ± sigma * rolling std (忽略 NaN)."""
    s = pd.Series(series).dropna()
    if len(s) < 3:
        return {"mean": float, "ucl": float, "lcl": float, "std": float}
    if window:
        roll = s.rolling(window=window, min_periods=max(3, window // 2))
        mean = float(roll.mean().iloc[-1]) if not roll.mean().empty else float(s.mean())
        std = float(roll.std().iloc[-1]) if not roll.std().empty else float(s.std())
    else:
        mean = float(s.mean())
        std = float(s.std())
    return {
        "mean": mean,
        "std": std,
        "ucl": mean + sigma * std,
        "lcl": mean - sigma * std,
    }


# ============================================================
# Western Electric Rules (Nelson简化版)
# ============================================================
def we_rules(series: pd.Series, sigma: float = 1.0) -> list[str]:
    """经典 Nelson rules:
    1. 1 点 > 3σ   (Zone A) -> 报告为 caller 处理
    2. 连续 9 点 同侧
    3. 连续 6 点 递增/递减
    4. 连续 14 点 交替
    5. 2/3 点 > 2σ 同侧
    6. 4/5 点 > 1σ 同侧
    7. 连续 15 点 < 1σ (中心 1σ 内)
    8. 连续 8 点 > 1σ 同侧
    """
    s = pd.Series(series).dropna().reset_index(drop=True)
    if len(s) < 9:
        return []
    mean = s.mean()
    std = s.std()
    if std == 0 or math.isnan(std):
        return []

    hits = []

    # rule 2: 连续 9 点 同侧
    for i in range(9, len(s) + 1):
        window = s.iloc[i - 9:i]
        if (window > mean).all() or (window < mean).all():
            hits.append(f"WE-2: 连续9点{'上' if window.iloc[-1] > mean else '下'}侧偏离均值")
            break

    # rule 3: 连续 6 点 递增/递减
    for i in range(6, len(s) + 1):
        win = s.iloc[i - 6:i].values
        diffs = np.diff(win)
        if (diffs > 0).all() or (diffs < 0).all():
            hits.append(f"WE-3: 连续6点{'升' if diffs[-1] > 0 else '降'}势")
            break

    # rule 6: 连续 4/5 点 偏离 > 1σ 同侧
    threshold = mean + sigma * std
    for i in range(5, len(s) + 1):
        win = s.iloc[i - 5:i]
        # 至少 4 点偏离同侧
        if ((win > threshold).sum() >= 4) or ((win < mean - sigma * std).sum() >= 4):
            hits.append(f"WE-6: 连续5点中至少4点偏离>1σ {'上' if win.iloc[-1] > threshold else '下'}侧")
            break

    # rule 8: 连续 8 点 在 ±1σ 同侧
    upper1 = mean + sigma * std
    lower1 = mean - sigma * std
    for i in range(8, len(s) + 1):
        win = s.iloc[i - 8:i]
        if (win > upper1).all():
            hits.append(f"WE-8: 连续8点高于>+1σ (上漂)")
            break
        if (win < lower1).all():
            hits.append(f"WE-8: 连续8点低于<-1σ (下漂)")
            break

    return hits


# ============================================================
# CUSUM (Cumulative Sum) -- 均值漂移快速检测
# ============================================================
def cusum(series: pd.Series, k: float = 0.5, h: float = 4.0) -> dict:
    """One-sided upper CUSUM. k=0.5σ 警戒线, h=4σ 决策限.
    返回 {'positive': [...], 'negative': [...], 'triggered': str|None}.
    """
    s = pd.Series(series).dropna().reset_index(drop=True)
    if len(s) < 5:
        return {"positive": [], "negative": [], "triggered": None}
    mu0 = float(s.mean())
    sigma = float(s.std())
    if sigma == 0:
        return {"positive": [], "negative": [], "triggered": None}

    k_abs = k * sigma
    h_abs = h * sigma
    s_pos = []
    s_neg = []
    c_plus = 0.0
    c_minus = 0.0
    triggered = None
    for v in s.values:
        c_plus = max(0.0, c_plus + (v - mu0) - k_abs)
        c_minus = max(0.0, c_minus + (mu0 - v) - k_abs)
        s_pos.append(c_plus)
        s_neg.append(c_minus)
        if triggered is None:
            if c_plus > h_abs:
                triggered = f"CUSUM+ 上漂突破: {c_plus:.2f} > {h_abs:.2f} (k={k}, h={h})"
            elif c_minus > h_abs:
                triggered = f"CUSUM- 下漂突破: {c_minus:.2f} > {h_abs:.2f} (k={k}, h={h})"
    return {
        "positive": s_pos,
        "negative": s_neg,
        "triggered": triggered,
        "k": k, "h": h,
        "mu0": mu0, "sigma": sigma,
    }


# ============================================================
# 触顶检测 (Threshold Pinning / Ceiling Detection)
# ============================================================
def lsl_pinning(series: pd.Series, lsl: float | None,
                usl: float | None, window: int = 5,
                min_hits: int = 3, tol: float = 1e-6) -> dict:
    """独立告警维度: 指标长期贴在 LSL/USL 上.

    场景: max_drawdown 卡死在 -10% (LSL), 看起来像"维持稳定", 实际是阈值触顶,
    真实回撤可能更深, 但被 LSL 截断, 导致均值偏移误判.

    Returns:
        {"lsl_pinned": bool, "lsl_hits": int, "usl_pinned": bool,
         "usl_hits": int, "window": int, "min_hits": int,
         "lsl": float|None, "usl": float|None,
         "first_pinned_idx": int|None,
         "last_pinned_value": float|None,
         "message": str}
    """
    s = pd.Series(series).dropna().reset_index(drop=True)
    n = len(s)
    out = {
        "lsl_pinned": False, "lsl_hits": 0, "usl_pinned": False, "usl_hits": 0,
        "window": min(window, n), "min_hits": min_hits, "lsl": lsl, "usl": usl,
        "first_pinned_idx": None, "last_pinned_value": None,
        "message": "",
    }
    if n == 0 or (lsl is None and usl is None):
        return out
    w = min(window, n)
    recent = s.tail(w)
    if lsl is not None:
        # "贴在 LSL" = 距离 LSL 在 tol 以内 (含等号); 仅取下侧
        lsl_hits_mask = (recent - lsl).abs() <= tol
        # 同时确保是 lower pinned (值 <= lsl + tol), 不要把 upper 端点误算进来
        lsl_lower_mask = recent <= lsl + tol
        n_lsl = int((lsl_hits_mask & lsl_lower_mask).sum())
        out["lsl_hits"] = n_lsl
        if n_lsl >= min_hits:
            out["lsl_pinned"] = True
            # 找首次贴点
            pinned_idx = recent.index[(lsl_hits_mask & lsl_lower_mask)].tolist()
            if pinned_idx:
                out["first_pinned_idx"] = int(pinned_idx[0])
                out["last_pinned_value"] = float(recent.iloc[-1])
    if usl is not None:
        usl_hits_mask = (recent - usl).abs() <= tol
        usl_upper_mask = recent >= usl - tol
        n_usl = int((usl_hits_mask & usl_upper_mask).sum())
        out["usl_hits"] = n_usl
        if n_usl >= min_hits:
            out["usl_pinned"] = True
            pinned_idx = recent.index[(usl_hits_mask & usl_upper_mask)].tolist()
            if pinned_idx:
                if out["first_pinned_idx"] is None:
                    out["first_pinned_idx"] = int(pinned_idx[0])
                out["last_pinned_value"] = float(recent.iloc[-1])
    msgs = []
    if out["lsl_pinned"]:
        msgs.append(f"LSL 触顶: 近 {w} 点中 {out['lsl_hits']} 点贴在 LSL={lsl}")
    if out["usl_pinned"]:
        msgs.append(f"USL 触顶: 近 {w} 点中 {out['usl_hits']} 点贴在 USL={usl}")
    out["message"] = "; ".join(msgs)
    return out


# ============================================================
# 主函数: 单指标 SPC 检查
# ============================================================
def spc_check(series: pd.Series, cfg: IndicatorCfg) -> dict:
    """对单个时间序列做 SPC 检查, 返回分级告警 dict."""
    s = pd.Series(series).dropna()
    n = len(s)
    out = {
        "indicator": cfg.name,
        "unit": cfg.unit,
        "level": OK,
        "mean": None,
        "sigma": None,
        "ucl": None,
        "lcl": None,
        "n": n,
        "last": float(s.iloc[-1]) if n else None,
        "violations": [],
        "message": "",
    }
    if n < 3:
        out["message"] = "样本不足, 无法判别"
        return out

    cl = control_limits(s, window=cfg.window, sigma=cfg.sigma)
    out.update({"mean": cl["mean"], "sigma": cl["std"],
                "ucl": cl["ucl"], "lcl": cl["lcl"]})
    last = float(s.iloc[-1])

    # 1) Shewhart 越界 (3σ)
    shewhart_hit = False
    if cfg.one_sided in ("two", "upper") and last > cl["ucl"]:
        out["violations"].append(f"Shewhart 越界: last={last:.4f} > UCL={cl['ucl']:.4f}")
        shewhart_hit = True
    if cfg.one_sided in ("two", "lower") and last < cl["lcl"]:
        out["violations"].append(f"Shewhart 越界: last={last:.4f} < LCL={cl['lcl']:.4f}")
        shewhart_hit = True

    # 2) LSL/USL 绝对阈值 (最近 N 点任一越界, 或 last 越界, 都算告警)
    lsl_hit = False
    if cfg.lsl is not None:
        # last 越界
        if last < cfg.lsl:
            out["violations"].append(f"绝对阈值越界: last={last:.4f} < LSL={cfg.lsl}")
            lsl_hit = True
        else:
            # 历史越界 (滚动窗内任一点)
            recent = s.tail(min(cfg.window, len(s)))
            breached = recent[recent < cfg.lsl]
            if not breached.empty:
                out["violations"].append(
                    f"窗口内 {len(breached)} 点跌破 LSL={cfg.lsl} "
                    f"(min={float(recent.min()):.4f})"
                )
                lsl_hit = True
    if cfg.usl is not None and last > cfg.usl:
        out["violations"].append(f"绝对阈值越界: last={last:.4f} > USL={cfg.usl}")
        lsl_hit = True

    # 3) CUSUM 漂移
    cu = cusum(s, k=0.5, h=4.0)
    if cu["triggered"]:
        out["violations"].append(cu["triggered"])

    # 4) Western Electric 软规则
    we = we_rules(s)
    out["violations"].extend(we)

    # 5) LSL/USL 触顶检测 (独立告警维度) -- 当指标长期贴在阈值上时,
    #    即使没突破, 也可能意味着阈值被截断或指标真实分布已偏移
    pin = lsl_pinning(s, cfg.lsl, cfg.usl,
                      window=cfg.ceil_window, min_hits=cfg.ceil_min_hits,
                      tol=cfg.ceil_tolerance)
    out["pinning"] = pin
    if pin["lsl_pinned"] or pin["usl_pinned"]:
        out["violations"].append(pin["message"])

    # 分级
    pinning_hit = pin["lsl_pinned"] or pin["usl_pinned"]
    if shewhart_hit or lsl_hit or cu["triggered"]:
        out["level"] = cfg.trigger_level if cfg.trigger_level in (P0, P1) else P0
        out["message"] = "; ".join(out["violations"][:3])
    elif pinning_hit:
        # 触顶是独立维度: 即使没有 Shewhart/LSL/CUSUM 触发, 也应作为告警
        ceil_lv = cfg.ceil_trigger_level if cfg.ceil_trigger_level in (P0, P1, P2, P3) else P1
        # 但若已有 we 命中, 触顶告警不可压低 we 的级别
        if we:
            out["level"] = min(ceil_lv, P1 if any("连续" in v for v in we) else P2,
                               key=lambda x: {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "OK": 4}[x])
        else:
            out["level"] = ceil_lv
        out["message"] = "; ".join(out["violations"][:3])
    elif we:
        out["level"] = P1 if any("连续" in v for v in we) else P2
        out["message"] = "; ".join(out["violations"][:3])
    elif abs(last - cl["mean"]) > cfg.sigma * cl["std"]:  # 落在 2-3σ 之间
        out["level"] = P2
        out["message"] = f"近 1 点偏离 ±2σ 范围 ({last:.4f}, mean={cl['mean']:.4f})"
    else:
        out["message"] = f"OK, last={last:.4f} 在 ±{cfg.sigma}σ 内"
    return out


# ============================================================
# 批量: 对一组指标跑 SPC
# ============================================================
def spc_batch(series_map: dict[str, pd.Series], cfgs: dict[str, IndicatorCfg]) -> list[dict]:
    """series_map: {indicator_name: pd.Series}; cfgs: {indicator_name: cfg}"""
    out = []
    for name, cfg in cfgs.items():
        s = series_map.get(name)
        if s is None:
            out.append({"indicator": name, "level": "MISSING", "message": "无数据"})
            continue
        out.append(spc_check(s, cfg))
    return out


# ============================================================
# 预定义指标配置 (用于 run_daily 调度)
# ============================================================
DEFAULT_CFGS = {
    "daily_return": IndicatorCfg(
        name="daily_return", unit="%", window=20, sigma=3.0, trigger_level=P0
    ),
    "sharpe_rolling": IndicatorCfg(
        name="sharpe_rolling", unit="", window=30, sigma=2.0, trigger_level=P0,
    ),
    "max_drawdown": IndicatorCfg(
        name="max_drawdown", unit="%", window=30, sigma=2.5,
        one_sided="lower", lsl=-10.0, trigger_level=P0,
        # 触顶检测: max_drawdown 卡在 -10% (LSL) 经常出现, 5 点里只要 3 点贴在阈值即告警
        ceil_window=5, ceil_min_hits=3, ceil_tolerance=1e-6,
        ceil_trigger_level=P1,
    ),
    "ic_hold_5": IndicatorCfg(
        name="ic_hold_5", unit="", window=20, sigma=2.5,
        one_sided="lower", lsl=-0.05, trigger_level=P1,
    ),
}


if __name__ == "__main__":
    # 冒烟测试: 构造一条含漂移 + 越界的序列
    import json as _json
    np.random.seed(0)
    base = np.random.normal(0.001, 0.01, 30).cumsum()
    base[20:] += 0.02  # 第 21 点开始漂移
    s = pd.Series(base)

    print("daily_return SPC:", _json.dumps(spc_check(s, DEFAULT_CFGS["daily_return"]),
                                            ensure_ascii=False, indent=2, default=str))
    print("ic_hold_5 SPC:", _json.dumps(spc_check(s, DEFAULT_CFGS["ic_hold_5"]),
                                         ensure_ascii=False, indent=2, default=str))
    # 触顶测试: 构造一条尾部 5 点卡死在 LSL=-10 的序列
    pinned = pd.Series([-12.0, -11.0, -10.5, -10.0, -10.0, -10.0, -10.0, -10.0, -10.0])
    print("max_drawdown (pinned) SPC:", _json.dumps(
        spc_check(pinned, DEFAULT_CFGS["max_drawdown"]),
        ensure_ascii=False, indent=2, default=str))
    # 纯正常序列 (应 OK)
    normal = pd.Series([-2.0, -3.0, -1.5, -2.5, -3.5, -2.8, -3.2, -2.7, -3.3, -2.9])
    print("max_drawdown (normal) SPC:", _json.dumps(
        spc_check(normal, DEFAULT_CFGS["max_drawdown"]),
        ensure_ascii=False, indent=2, default=str))
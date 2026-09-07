# -*- coding: utf-8 -*-
"""IC 门控 + 单日亏损防御 + 自适应调仓节奏 (factor_gate).

基于 121 日归因结论与 B 路径重放发现, 落地(2026-09-07 修订):

1. IC 门控 (融合因子失效检测), 采用联合判据 + 3 日滞后:
   - 进入 risk 需同时满足:
       rolling_ic_mean < FG_IC_RISK_MEAN_J (默认 -0.005)
       且 neg_share > FG_IC_NEG_SHARE_RISK (默认 0.55)
       且 (ic_ir 缺失时忽略, 否则 ic_ir < FG_IC_RISK_IR, 默认 -0.5)
     → 避免单个弱条件把正常期误判为 risk (B 路径重放: 旧判据下 risk 占
       53/122 天, 平均暴露仅 0.71, 削掉过多上行).
   - risk 退出滞后: 需连续 FG_HYST_EXIT (默认 3) 日不满足 risk 才退出;
     进入同样需连续 FG_HYST_ENTER (默认 3) 日满足.
   - normal/caution 按天即时 (caution 仅在非 risk 且 mean < FG_IC_NORMAL_MEAN).

2. 暴露下限提高: risk 状态 exposure = FG_EXP_RISK (默认 0.8, 原 0.4),
   caution = FG_EXP_CAUTION (默认 0.9), 保留更多上行参与.

3. 单日亏损防御 (不变量): daily_loss <= FG_LOSS_HEAVY(-2%) 冻结新买;
   <= FG_LOSS_WARN(-1%) 压缩暴露. 回撤熔断仍由 PaperBook 兜底.

4. 个体因子 IC 漂移监控 (2026-09-07 新增):
   - 对各因子独立监控短期 IC 与长期均值的偏离
   - 漂移 > FG_FACTOR_IC_DRIFT (默认 0.15) 时标记为 unstable
   - 不稳定因子数 > 阈值时, 增强门控严度

5. Sharpe 变点检测 (2026-09-07 新增):
   - 滚动窗口检测 Sharpe 跳变幅度
   - 跳变 > FG_SHARPE_CHANGE_THRESHOLD 时, 自动进入 caution 档位

输出 plan dict 由 realtime_engine._compute_gate 消费; 仅控制策略调仓.
# 环境变量覆盖:
#   FG_IC_WINDOW / FG_IC_NORMAL_MEAN / FG_IC_RISK_MEAN_J / FG_IC_NEG_SHARE_RISK /
#   FG_IC_RISK_IR / FG_LOSS_WARN / FG_LOSS_HEAVY / FG_EXP_CAUTION / FG_EXP_RISK /
#   FG_HYST_ENTER / FG_HYST_EXIT / FG_INTERVAL_CAUTION_STEP / FG_INTERVAL_RISK_STEP /
#   FG_FACTOR_IC_DRIFT / FG_UNSTABLE_FACTOR_LIMIT / FG_SHARPE_CHANGE_THRESHOLD
"""

from __future__ import annotations

import json
import os

import numpy as np

_BASE = os.path.dirname(os.path.abspath(__file__))
IC_CACHE = os.path.join(_BASE, "data", "factor_mine", "fusion_ic_121d.json")


# ------------------------------------------------------------------
# 默认阈值: 取值优先级 = 环境变量 > 配置文件(FACTOR_GATE_CONFIG 或
# data/factor_gate_config.json) > 代码默认值
# ------------------------------------------------------------------
_CFG_FILE = os.path.join(_BASE, "data", "factor_gate_config.json")


def _load_file_cfg() -> dict:
    cands = []
    envp = os.environ.get("FACTOR_GATE_CONFIG")
    if envp:
        cands.append(envp)
    cands.append(_CFG_FILE)
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, (int, float))}, p
        except Exception:
            continue
    return {}, None


_FILE_CFG, _FILE_CFG_PATH = _load_file_cfg()


def _num(name: str, env: str, default: float) -> float:
    v = os.environ.get(env)
    if v is not None and str(v).strip() != "":
        try:
            return float(v)
        except ValueError:
            pass
    if name in _FILE_CFG:
        try:
            return float(_FILE_CFG[name])
        except (TypeError, ValueError):
            pass
    return float(default)


def _inum(name: str, env: str, default: int) -> int:
    v = os.environ.get(env)
    if v is not None and str(v).strip() != "":
        try:
            return int(float(v))
        except ValueError:
            pass
    if name in _FILE_CFG:
        try:
            return int(float(_FILE_CFG[name]))
        except (TypeError, ValueError):
            pass
    return int(default)


class _Cfg:
    # ---- 窗口与判据 ----
    ic_window = _inum("ic_window", "FG_IC_WINDOW", 20)
    ic_normal_mean = _num("ic_normal_mean", "FG_IC_NORMAL_MEAN", 0.005)   # >= 正常
    # 联合 risk 判据
    ic_risk_mean = _num("ic_risk_mean", "FG_IC_RISK_MEAN_J", -0.005)      # mean 须低于该值
    ic_neg_share_risk = _num("ic_neg_share_risk", "FG_IC_NEG_SHARE_RISK", 0.55)
    ic_risk_ir = _num("ic_risk_ir", "FG_IC_RISK_IR", -0.5)                # 可选 ICIR 下限
    # 滞后 (进入/退出 risk 需连续 N 日)
    hyst_enter = _inum("hyst_enter", "FG_HYST_ENTER", 3)
    hyst_exit = _inum("hyst_exit", "FG_HYST_EXIT", 3)
    # 单日亏损防御
    daily_loss_warn = _num("daily_loss_warn", "FG_LOSS_WARN", -0.01)
    daily_loss_heavy = _num("daily_loss_heavy", "FG_LOSS_HEAVY", -0.02)
    # 暴露档位: normal 恒 1.0; risk/caution 只小幅降, 保留上行参与
    exp_caution = _num("exp_caution", "FG_EXP_CAUTION", 0.9)
    exp_risk = _num("exp_risk", "FG_EXP_RISK", 0.8)
    int_caution_step = _inum("int_caution_step", "FG_INTERVAL_CAUTION_STEP", 2)
    int_risk_step = _inum("int_risk_step", "FG_INTERVAL_RISK_STEP", 3)
    # ---- 个体因子 IC 漂移监控 (2026-09-07 新增) ----
    factor_ic_drift = _num("factor_ic_drift", "FG_FACTOR_IC_DRIFT", 0.15)
    unstable_factor_limit = _inum("unstable_factor_limit", "FG_UNSTABLE_FACTOR_LIMIT", 2)
    # ---- Sharpe 变点检测 (2026-09-07 新增) ----
    sharpe_change_threshold = _num("sharpe_change_threshold", "FG_SHARPE_CHANGE_THRESHOLD", 0.3)
    sharpe_change_window = _inum("sharpe_change_window", "FG_SHARPE_CHANGE_WINDOW", 5)
    # 严重跳变阈值(≥ 该值且检测到变点 → 直接跳级 risk)与变点后保持天数
    sharpe_severe = _num("sharpe_severe", "FG_SHARPE_SEVERE", 0.6)
    sharpe_change_hold = _inum("sharpe_change_hold", "FG_SHARPE_CHANGE_HOLD", 3)
    # IC 漂移"长期基准"窗口(交易日, 与 gate 融合 IC 缓存 121 日口径一致)
    ic_drift_long_window = _inum("ic_drift_long_window", "FG_IC_DRIFT_LONG_WINDOW", 121)
    enabled = os.environ.get("FACTOR_GATE_ENABLED", "1") != "0"
    cfg_file_loaded = bool(_FILE_CFG)


# ------------------------------------------------------------------
# 个体因子 IC 漂移监控 (2026-09-07 新增; 2026-09-07 修订为方向有效性语义)
# ------------------------------------------------------------------
# 反转义 alpha 因子: 策略中以"低波 / 近跌给高分"使用 (f_vol = 低波高分,
# f_mom_rev = -mom_20), 因此对这些因子 **深度负 IC = 更有效**.
# 与 premarket_healthcheck._ic_recent_vs_history(reversal=True) 的口径一致:
# 真正的失效是 短期 IC 转正(变成反向信号) 或 强度收敛归零, 而非"负 IC 加深".
_REVERSAL_ALPHA_FACTORS = {"vol", "mom_20", "mom_rev"}


def check_factor_ic_drift(
    factor_ics: dict[str, dict],
    cfg: _Cfg | None = None,
) -> dict:
    """检查各因子 IC 的稳定性 (方向有效性语义).

    对反转义因子 (见 _REVERSAL_ALPHA_FACTORS): 不稳定 = 短期 IC 转正
    (flipped) 或 未翻转但强度收敛到 < max(70% × 长期强度, 0.02) (weakened);
    负 IC 加深 (更有效) 不判不稳.
    对常规方向因子 (含长期强度本就接近 0 的): 沿用 |短期 - 长期| 漂移 > 阈值判不稳.

    Args:
        factor_ics: {factor_name: {"long_mean": float, "short_mean": float}}.

    Returns:
        {"unstable_factors": [因子名], "n_unstable": int, "drift_detail": {
           name: {long_mean, short_mean, drift, mode: 'reversal'|'level',
                  flipped, weakened, reason}}}.
    """
    c = cfg or _Cfg()
    unstable = []
    detail = {}
    for name, ic in factor_ics.items():
        long_m = ic.get("long_mean")
        short_m = ic.get("short_mean")
        if long_m is None or short_m is None:
            continue
        drift = abs(short_m - long_m)
        is_rev = name in _REVERSAL_ALPHA_FACTORS and abs(long_m) >= 0.02
        if is_rev:
            # 反转义 alpha: 有效方向是负 IC.
            flipped = bool(short_m > 0 and abs(short_m) >= 0.01)
            weakened = bool(not flipped and abs(long_m) >= 0.02
                            and abs(short_m) < max(abs(long_m) * 0.7, 0.02))
            unstable_flag = flipped or weakened
            d = {"long_mean": long_m, "short_mean": short_m,
                 "drift": drift, "mode": "reversal",
                 "flipped": flipped, "weakened": weakened}
            if flipped:
                d["reason"] = "短期IC转正 → 反转义失效(信号变反向)"
                unstable.append(name)
            elif weakened:
                d["reason"] = "反转义强度收敛到 <70%长期(或 <0.02) → 信号失灵"
                unstable.append(name)
            else:
                d["reason"] = ("反转义方向保持(负IC加深或持平=更有效/有效)"
                               if short_m < 0 else "反转义方向保持")
            detail[name] = d
        else:
            # 常规方向因子 (或长期方向不明确): 原始漂移判据
            unstable_flag = drift > c.factor_ic_drift
            detail[name] = {
                "long_mean": long_m, "short_mean": short_m, "drift": drift,
                "mode": "level",
                "reason": (f"漂移 {drift:.4f} > {c.factor_ic_drift:.2f} → 不稳定"
                           if unstable_flag else "漂移在阈值内"),
            }
            if unstable_flag:
                unstable.append(name)
    return {
        "unstable_factors": unstable,
        "n_unstable": len(unstable),
        "drift_detail": detail,
    }


# ------------------------------------------------------------------
# 因子健康 -> 打分权重处置 (2026-09-07): 失效因子隔离
# 监控曲线名(vol/mom_20) -> selector 打分键(vol/mom_rev).
# ------------------------------------------------------------------
_DRIFT_CURVE_TO_SCORE_KEY = {"vol": "vol", "mom_20": "mom_rev"}


def load_factor_ic_from_curves(cfg: _Cfg | None = None) -> dict:
    """从 data/ic/ic_curve_*_k20.csv 读近 121/近 20 交易日 ic_h20 均值.

    口径与 overfitting 6c 一致: long = 近 ic_drift_long_window(默认121) 日,
    short = 近 20 日. 无曲线/样本不足的因子不返回.
    """
    import csv as _csv
    c = cfg or _Cfg()
    long_w = c.ic_drift_long_window
    out = {}
    ic_dir = os.path.join(_BASE, "data", "ic")
    for name in _DRIFT_CURVE_TO_SCORE_KEY:
        p = os.path.join(ic_dir, f"ic_curve_{name}_k20.csv")
        if not os.path.exists(p):
            continue
        try:
            vals = []
            with open(p, encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    v = row.get("ic_h20")
                    if v in (None, ""):
                        continue
                    try:
                        vals.append(float(v))
                    except ValueError:
                        continue
            if len(vals) < 30:
                continue
            long_m = float(np.mean(vals[-long_w:])) if len(vals) >= long_w else float(np.mean(vals))
            short_m = float(np.mean(vals[-20:]))
            out[name] = {"long_mean": long_m, "short_mean": short_m}
        except Exception:
            continue
    return out


def factor_health_flags(factor_ics: dict[str, dict] | None = None,
                        cfg: _Cfg | None = None) -> dict:
    """按方向有效性给出打分键处置建议 (供 selector_weights 消费).

    - isolate: 因子短期方向翻转或强度收敛归零 (反转义失效), 打分权重应置 0;
    - keep:    方向保持 (含反转义负 IC 加深 = 更有效).

    Args:
        factor_ics: {因子名: {"long_mean","short_mean"}}; None 时自动读 IC 曲线.
    Returns:
        {"enabled": bool, "isolated": {打分键: {"action","reason"}},
         "unstable": [因子名], "drift_detail": {}}
    """
    c = cfg or _Cfg()
    # None = 自动读 IC 曲线; 传入 dict(可为空)则按给定数据判定
    fics = factor_ics if factor_ics is not None else load_factor_ic_from_curves(c)
    if not fics:
        return {"enabled": True, "isolated": {},
                "unstable": [], "drift_detail": {},
                "reason": "无因子 IC 曲线数据, 不做健康处置"}
    res = check_factor_ic_drift(fics, cfg=c)
    isolated = {}
    for name in res["unstable_factors"]:
        key = _DRIFT_CURVE_TO_SCORE_KEY.get(name)
        if key:
            isolated[key] = {
                "action": "isolate",
                "reason": res["drift_detail"][name].get("reason", "方向失效"),
            }
    return {"enabled": True, "isolated": isolated,
            "unstable": res["unstable_factors"],
            "drift_detail": res["drift_detail"]}


# ------------------------------------------------------------------
# Sharpe 变点检测 (2026-09-07 新增)
# ------------------------------------------------------------------
def detect_sharpe_change_point(
    sharpe_history: list[float],
    cfg: _Cfg | None = None,
) -> dict:
    """滚动窗口检测 Sharpe 跳变幅度.

    PELT 近似: 计算相邻窗口 Sharpe 的差异, 若最大跳变 > 阈值则标记为 change.

    Args:
        sharpe_history: 按时间升序排列的 Sharpe 序列.

    Returns:
        {"has_change_point": bool, "max_jump": float, "mean_jump": float,
         "n_jumps": int, "change_indices": [int]}.
    """
    c = cfg or _Cfg()
    if len(sharpe_history) < 4:
        return {"has_change_point": False, "max_jump": 0.0, "mean_jump": 0.0,
                "n_jumps": 0, "change_indices": []}

    # 仅评估最近 sharpe_change_window 个相邻跳变: 传入累积历史时, 过旧的跳变
    # 不应永远把 has_change_point 置 True (否则 cp_hold 永不衰减).
    w = max(1, int(c.sharpe_change_window))
    tail = sharpe_history[-(w + 1):]
    deltas = [abs(tail[i] - tail[i - 1]) for i in range(1, len(tail))]
    max_jump = max(deltas) if deltas else 0.0
    mean_jump = float(np.mean(deltas)) if deltas else 0.0
    jump_std = float(np.std(deltas)) if len(deltas) > 1 else 0.0

    # > 均值 + 2σ 或 > 阈值取较大者判定为变点
    threshold = max(c.sharpe_change_threshold, mean_jump + 2.0 * jump_std)
    local_change = [i for i, d in enumerate(deltas) if d > threshold]
    # 映射回原序列下标
    base = len(sharpe_history) - len(tail)
    change_indices = [base + i for i in local_change]

    return {
        "has_change_point": len(change_indices) > 0,
        "max_jump": max_jump,
        "mean_jump": mean_jump,
        "n_jumps": len(change_indices),
        "change_indices": change_indices,
    }


# ------------------------------------------------------------------
# 滞后状态机 (risk 进入/退出各需连续 N 日)
# ------------------------------------------------------------------
def _default_hyst() -> dict:
    return {"risk_streak": 0, "exit_streak": 0, "in_risk": False}


def apply_hysteresis(raw_regime: str, hyst: dict | None,
                     cfg: _Cfg | None = None) -> tuple[str, dict]:
    """把原始 regime 平滑为生效 regime (risk 带 N 日滞后).

    unknown/disabled 不参与计数 (暂停), 直接透传.
    """
    h = dict(hyst) if hyst else _default_hyst()
    if raw_regime not in ("normal", "caution", "risk"):
        return raw_regime, h
    c = cfg or _Cfg()
    if raw_regime == "risk":
        h["exit_streak"] = 0
        if not h.get("in_risk"):
            h["risk_streak"] = int(h.get("risk_streak", 0)) + 1
            if h["risk_streak"] >= c.hyst_enter:
                h["in_risk"] = True
                h["risk_streak"] = 0
    else:
        h["risk_streak"] = 0
        if h.get("in_risk"):
            h["exit_streak"] = int(h.get("exit_streak", 0)) + 1
            if h["exit_streak"] >= c.hyst_exit:
                h["in_risk"] = False
                h["exit_streak"] = 0

    if raw_regime == "risk" and not h.get("in_risk"):
        # 连续确认期(<N日)未正式进入 risk: 按温和档(caution)过渡, 不立即重手降仓
        effective = "caution"
    else:
        effective = "risk" if h.get("in_risk") else raw_regime
    return effective, h


# ------------------------------------------------------------------
# 核心判定 (纯函数, 便于测试)
# ------------------------------------------------------------------
def compute_plan(
    ic_mean: float | None,
    ic_neg_share: float | None,
    daily_loss: float | None = None,
    base_interval: int = 3,
    ic_ir: float | None = None,
    hyst: dict | None = None,
    cfg: _Cfg | None = None,
    # 2026-09-07 新增: 个体因子 IC 漂移
    factor_ic_drift_result: dict | None = None,
    # 2026-09-07 新增: Sharpe 变点检测
    sharpe_change_result: dict | None = None,
) -> dict:
    """由最近 IC 状态与当日亏损决定调仓 plan (含 risk 3 日滞后).

    Args:
        ic_mean: 最近窗口 fwd5 日均 IC (None=无数据).
        ic_neg_share: 最近窗口负 IC 占比.
        daily_loss: 当日净值相对日初跌幅.
        base_interval: 常规调仓间隔(自然日).
        ic_ir: 最近窗口 ICIR (mean/std, 非年化); 提供时 risk 联合判据强制其 < 阈值.
        hyst: 滞后状态字典 (会被就地更新并随返回值返回).
        factor_ic_drift_result: check_factor_ic_drift() 的输出.
            n_unstable > 2 时增强门控严度.
        sharpe_change_result: detect_sharpe_change_point() 的输出.
            has_change_point = True 时自动进入 caution.
    """
    c = cfg or _Cfg()
    reasons = []
    h = dict(hyst) if hyst else _default_hyst()

    # ---- 原始档位 (联合判据, 收紧 risk) ----
    if ic_mean is None or ic_neg_share is None:
        raw = "unknown"
    else:
        _joint_mean = ic_mean < c.ic_risk_mean
        _joint_neg = ic_neg_share > c.ic_neg_share_risk
        _joint_ir = True if ic_ir is None else ic_ir < c.ic_risk_ir
        if _joint_mean and _joint_neg and _joint_ir:
            raw = "risk"
        elif ic_mean >= c.ic_normal_mean:
            raw = "normal"
        else:
            raw = "caution"

    # ---- 滞后平滑 (risk 进入/退出均需连续 3 日) ----
    if raw == "risk":
        reasons.append(f"原始 risk 信号: mean={ic_mean:.4f} neg={ic_neg_share:.0%} "
                       f"ir={ic_ir if ic_ir is None else round(ic_ir, 3)} (联合判据)")
    effective, h = apply_hysteresis(raw, h, cfg=c)

    # ---- 2026-09-07 新增: 个体因子 IC 漂移增强门控 ----
    # 不稳定因子数 >= 阈值时, 在当前档位基础上加严一级.
    # 语义: 阈值 2 = 3 个被监控因子中 >= 2/3 不稳定即增强 (原实现用 > 导致需 3/3 才生效).
    _drift = factor_ic_drift_result or {}
    _n_unstable = _drift.get("n_unstable", 0)
    _unstable_limit = _drift.get("unstable_limit", c.unstable_factor_limit)
    if _n_unstable >= _unstable_limit:
        if effective == "normal":
            effective = "caution"
            reasons.append(f"因子IC漂移增强: {_n_unstable}个因子不稳定(≥{_unstable_limit}) → caution")
        elif effective == "caution":
            effective = "risk"
            reasons.append(f"因子IC漂移增强: {_n_unstable}个因子不稳定(≥{_unstable_limit}) → risk")
        else:
            reasons.append(f"因子IC漂移: {_n_unstable}个因子不稳定 (已为risk, 保持)")
    elif _n_unstable > 0:
        reasons.append(f"因子IC漂移: {_n_unstable}个因子不稳定 (<{_unstable_limit}, 不增强)")

    # ---- 2026-09-07 新增: Sharpe 变点检测 ----
    # 检测到变点时进入 caution; 若跳变幅度 >= sharpe_severe, 直接跳级 risk.
    # 触发后设置 cp_hold 保持期, 避免 IC 刚恢复就立刻满仓 (平抑变点后的二次下杀).
    _sharpe = sharpe_change_result or {}
    _jump = float(_sharpe.get("max_jump", 0.0) or 0.0)
    _hold = dict(h) if hyst is not None else dict(h)
    if _sharpe.get("has_change_point"):
        if _jump >= c.sharpe_severe:
            if effective in ("normal", "caution"):
                reasons.append(f"Sharpe严重变点: 跳变{_jump:.3f} ≥ {c.sharpe_severe:.2f} → 跳级 risk")
            effective = "risk"
            _hold["cp_hold"] = max(int(_hold.get("cp_hold", 0)), c.sharpe_change_hold)
        elif effective == "normal":
            effective = "caution"
            _hold["cp_hold"] = max(int(_hold.get("cp_hold", 0)), c.sharpe_change_hold)
            reasons.append(f"Sharpe变点检测: 最大跳变{_jump:.3f} > 阈值 → caution")
        else:
            _hold["cp_hold"] = max(int(_hold.get("cp_hold", 0)), c.sharpe_change_hold)
            reasons.append(f"Sharpe变点检测: 最大跳变{_jump:.3f} (已为{effective}, 保持)")
    else:
        # 无新变点: 保持期逐日衰减
        if int(_hold.get("cp_hold", 0)) > 0:
            _hold["cp_hold"] = int(_hold["cp_hold"]) - 1
    # 变点保持期生效: 即使 IC 已转 normal, 保持期未结束前最低 caution
    if int(_hold.get("cp_hold", 0)) > 0 and effective == "normal":
        effective = "caution"
        reasons.append(f"Sharpe变点保持期: 剩余{_hold['cp_hold']}日 → 维持caution")
    # 回写调用方持久持有的滞后状态 (引擎跨日复用)
    if hyst is not None:
        hyst.clear()
        hyst.update(_hold)
    if effective != raw and raw != "unknown":
        tag = "进入" if effective == "risk" else "退出"
        reasons.append(f"risk 滞后{tag}: 需连续 "
                       f"{c.hyst_enter if effective == 'risk' else c.hyst_exit} 日确认")

    if effective == "unknown":
        reasons.append("IC 缓存缺失/过期, 门控降级为原节奏")
    elif effective == "normal":
        reasons.append(f"门控=normal: 20日均IC={ic_mean:.4f}")
    elif effective == "caution":
        reasons.append(f"门控=caution: 20日均IC={ic_mean:.4f} 负占比={ic_neg_share:.0%}")
    else:
        reasons.append(f"门控=risk: 20日均IC={ic_mean:.4f} 负占比={ic_neg_share:.0%}")

    # ---- 暴露/节奏/冻结 (按生效档位) ----
    if effective == "risk":
        exposure = c.exp_risk
        interval = max(base_interval, base_interval + c.int_risk_step)
        freeze_new = True
    elif effective == "caution":
        exposure = c.exp_caution
        interval = base_interval + c.int_caution_step
        freeze_new = False
    else:  # normal / unknown
        exposure = 1.0
        interval = base_interval
        freeze_new = False

    loss_flag = "none"
    if daily_loss is not None and daily_loss <= c.daily_loss_heavy:
        loss_flag = "heavy"
        freeze_new = True
        exposure = min(exposure, c.exp_risk)
        interval = max(interval, base_interval + c.int_risk_step)
        reasons.append(f"单日亏损 {daily_loss:.2%} <= {c.daily_loss_heavy:.0%}: 冻结新买入")
    elif daily_loss is not None and daily_loss <= c.daily_loss_warn:
        loss_flag = "warn"
        exposure = min(exposure, c.exp_caution)
        reasons.append(f"单日亏损 {daily_loss:.2%} <= {c.daily_loss_warn:.0%}: 暴露降至 {exposure:.0%}")

    return {
        "regime": effective,
        "raw_regime": raw,
        "ic_mean": round(float(ic_mean), 5) if ic_mean is not None else None,
        "ic_neg_share": round(float(ic_neg_share), 4) if ic_neg_share is not None else None,
        "ic_ir": round(float(ic_ir), 4) if ic_ir is not None else None,
        "daily_loss": round(float(daily_loss), 6) if daily_loss is not None else None,
        "exposure_mult": round(float(exposure), 4),
        "freeze_new_buys": bool(freeze_new),
        "interval_days": int(interval),
        "loss_flag": loss_flag,
        "hyst": h,
        "reasons": reasons,
    }


# ------------------------------------------------------------------
# 读缓存并生成 plan (引擎入口)
# ------------------------------------------------------------------
def load_ic_cache(path: str | None = None) -> dict | None:
    p = path or IC_CACHE
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _window_stats(vals: list, w: int) -> dict:
    """vals: 有效 fwd5 IC 序列(升序); 返回最近 w 个的 mean/neg/ir."""
    tail = vals[-w:]
    if not tail:
        return {"mean": None, "neg": None, "ir": None}
    a = np.asarray(tail, dtype=float)
    mean = float(a.mean())
    neg = float((a < 0).mean())
    sd = float(a.std(ddof=1)) if len(a) > 1 else 0.0
    ir = (mean / sd) if sd > 1e-12 else None
    return {"mean": mean, "neg": neg, "ir": ir}


def build_plan_from_cache(
    daily_loss: float | None = None,
    base_interval: int = 3,
    cache_path: str | None = None,
    hyst: dict | None = None,
    factor_ics: dict[str, dict] | None = None,
    sharpe_history: list[float] | None = None,
) -> dict:
    """引擎入口: 从融合 IC 缓存取最近窗口状态并生成当日调仓 plan.

    hyst: 滞后状态字典 (可复用跨日, 由调用方持久持有).
    factor_ics: {因子名: {"long_mean", "short_mean"}} → 个体因子 IC 漂移监控
        (2026-09-07). None 时跳过增强门控.
    sharpe_history: 按时间升序的滚动 Sharpe 序列 → 变点检测 (2026-09-07).
        None 时跳过. 由调用方提供(无数据则优雅降级).
    """
    c = _Cfg()
    if not c.enabled:
        return {
            "regime": "disabled", "raw_regime": "disabled",
            "ic_mean": None, "ic_neg_share": None, "ic_ir": None,
            "daily_loss": round(float(daily_loss), 6) if daily_loss is not None else None,
            "exposure_mult": 1.0, "freeze_new_buys": False,
            "interval_days": int(base_interval), "loss_flag": "none",
            "hyst": dict(hyst) if hyst else _default_hyst(),
            "reasons": ["FACTOR_GATE_ENABLED=0, 门控关闭"],
        }
    cache = load_ic_cache(cache_path)
    series = (cache or {}).get("daily_ic") or []
    if not series:
        _d0 = check_factor_ic_drift(factor_ics, cfg=c) if factor_ics else None
        if _d0 is not None:
            _d0["unstable_limit"] = c.unstable_factor_limit
        _s0 = detect_sharpe_change_point(sharpe_history, cfg=c) if sharpe_history else None
        return compute_plan(None, None, daily_loss, base_interval,
                            ic_ir=None, hyst=hyst,
                            factor_ic_drift_result=_d0,
                            sharpe_change_result=_s0)

    # 剔除 fwd5 缺失日(窗口尾部), 再取最近 w 个有效 IC 日
    vals = [v for d in series
            if (v := d.get("fwd5_ic")) is not None and np.isfinite(v)]
    st = _window_stats(vals, c.ic_window)

    # (2026-09-07) 上下文(有则用, 无则降级): 漂移结果需先经 check_factor_ic_drift
    drift_result = None
    if factor_ics:
        drift_result = check_factor_ic_drift(factor_ics, cfg=c)
        drift_result["unstable_limit"] = c.unstable_factor_limit
    sharpe_result = None
    if sharpe_history:
        sharpe_result = detect_sharpe_change_point(sharpe_history, cfg=c)

    plan = compute_plan(st["mean"], st["neg"], daily_loss, base_interval,
                        ic_ir=st["ir"], hyst=hyst, cfg=c,
                        factor_ic_drift_result=drift_result,
                        sharpe_change_result=sharpe_result)
    plan["ic_as_of"] = series[-1].get("date") if series else None
    return plan


def save_gate_state(plan: dict, path: str | None = None) -> str:
    p = path or os.path.join(_BASE, "data", "factor_gate_state.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return p


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="IC 门控预览")
    ap.add_argument("--daily-loss", type=float, default=None)
    ap.add_argument("--base-interval", type=int, default=3)
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()
    h = _default_hyst()
    # 模拟连续 5 日以展示滞后 (仅预览): 逐日打印
    prev = None
    for i in range(5):
        plan = build_plan_from_cache(args.daily_loss, args.base_interval, args.cache, hyst=h)
        print(json.dumps({"day": i + 1, "regime": plan["regime"],
                          "raw": plan["raw_regime"],
                          "exposure": plan["exposure_mult"],
                          "hyst": plan["hyst"]}, ensure_ascii=False, indent=0))
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

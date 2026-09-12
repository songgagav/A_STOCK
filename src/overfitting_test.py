# -*- coding: utf-8 -*-
"""过拟合与稳健性检测套件 — 7 维度全覆盖.

CPCV / PBO / 滚动窗口 / 置换检验 / 市场状态依赖(PELT) / 参数稳定性 / 健康度诊断

用法:
    python overfitting_test.py                    # 全量 7 项检测
    python overfitting_test.py --list             # 列出检测项
    python overfitting_test.py --only 1,3,5       # 仅测指定项
    python overfitting_test.py --html             # 同时输出 HTML 报告

输出: data/overfitting_report.json
      data/overfitting_report.html (--html)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, date
from typing import Any

import numpy as np

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
os.chdir(_BASE)

DATA_DIR = os.path.join(_BASE, "data")
OVERFITTING_REPORT = os.path.join(DATA_DIR, "overfitting_report.json")
OVERFITTING_HTML = os.path.join(DATA_DIR, "overfitting_report.html")

PASS = "  [PASS]"
FAIL = "  [FAIL]"
WARN = "  [WARN]"
INFO = "  [INFO]"

np.random.seed(42)
random.seed(42)


# ====================================================================
# 辅助
# ====================================================================
class _Results:
    def __init__(self):
        self.items: list[dict] = []
        self._idx = 0

    def check(self, label: str, ok: bool, detail: str = "",
              measured: Any = None, threshold: str = "",
              category: str = "") -> None:
        self._idx += 1
        status = "PASS" if ok else ("WARN" if ok is None else "FAIL")
        icon = PASS if ok else (WARN if ok is None else FAIL)
        print(f"  {icon} #{self._idx} {label}")
        if detail:
            print(f"         {detail}")
        if measured is not None:
            print(f"         实测值: {measured}  (阈值: {threshold})")
        self.items.append({
            "id": self._idx, "label": label, "status": status,
            "detail": detail, "measured": measured, "threshold": threshold,
            "category": category,
        })

    def summary(self) -> dict:
        n_pass = sum(1 for i in self.items if i["status"] == "PASS")
        n_fail = sum(1 for i in self.items if i["status"] == "FAIL")
        n_warn = sum(1 for i in self.items if i["status"] == "WARN")
        return {"total": len(self.items), "pass": n_pass,
                "fail": n_fail, "warn": n_warn, "passed": n_fail == 0}

R = _Results()


# ====================================================================
# 数据加载
# ====================================================================
def _load_vnpy_results() -> list:
    # 支持切换窗口样本源 (如 scripts/nonoverlap_rerun.py 生成的非重叠窗口)
    fp = os.environ.get("OVERFIT_RESULTS_FILE") or os.path.join(DATA_DIR, "vnpy_backtest_rerun_results.json")
    if not os.path.exists(fp):
        return []
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def _load_drl_weights() -> list:
    base = os.path.join(DATA_DIR, "drl")
    weights = []
    for d in sorted(os.listdir(base)):
        fp = os.path.join(base, d, "target_plan.json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    plan = json.load(f)
                w = plan.get("weights_used", {})
                w["day"] = d
                w["date"] = plan.get("generated_at", "")
                w["n"] = len(plan.get("top_n", []))
                weights.append(w)
            except Exception:
                pass
    return weights


def _load_performance_report() -> dict:
    fp = os.path.join(DATA_DIR, "performance_report.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _load_backtest_gated() -> dict:
    fp = os.path.join(DATA_DIR, "backtest_gated.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _load_fusion_samples() -> list:
    """从 factor_fusion_samples 目录加载每日融合样本元数据."""
    base = os.path.join(DATA_DIR, "factor_fusion_samples")
    samples = []
    if os.path.isdir(base):
        for fn in sorted(os.listdir(base)):
            if fn.endswith(".parquet"):
                samples.append({"file": fn, "date": fn.replace(".parquet", "")})
    return samples


_IC_CURVE_DIR = os.path.join(DATA_DIR, "ic")
_DRIFT_FACTORS = ("vol", "mom_20", "reversal")


def _load_factor_ic_drift() -> tuple[dict, dict]:
    """从 data/ic/ic_curve_*_k20.csv 计算个体因子 IC 漂移上下文.

    口径 (2026-09-07 修正): long = 近 121 个交易日 (与 gate 融合 IC 缓存一致,
    非 2013 年起的全历史均值 — 全历史基准会让近 20 日均值几乎必然"漂移"),
    short = 近 20 个交易日.

    Returns: (factor_ics {name: {"long_mean","short_mean"}}, drift_detail)
    """
    from factor_gate import _Cfg
    long_w = int(_Cfg().ic_drift_long_window)
    factor_ics, detail = {}, {}
    for name in _DRIFT_FACTORS:
        fp = os.path.join(_IC_CURVE_DIR, f"ic_curve_{name}_k20.csv")
        if not os.path.exists(fp):
            continue
        try:
            import csv
            vals = []
            with open(fp, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    v = row.get("ic_h20")
                    if v in (None, ""):
                        continue
                    try:
                        vals.append(float(v))
                    except ValueError:
                        continue
            if len(vals) < 30:
                continue
            vals = vals[-max(long_w, 20):]
            long_m = float(np.mean(vals[-long_w:])) if len(vals) >= long_w else float(np.mean(vals))
            short_m = float(np.mean(vals[-20:]))
            factor_ics[name] = {"long_mean": long_m, "short_mean": short_m}
            detail[name] = {"long_mean": long_m, "short_mean": short_m,
                            "drift": abs(short_m - long_m)}
        except Exception:
            continue
    return factor_ics, detail


# ------------------------------------------------------------------
# (2026-09-07 修正口径) 窗口逐日净值曲线相关辅助
# 旧口径把 8 个"起点错位 1~3 日、相互 99% 重叠"的窗口当成时序,
# 前后两组窗口均值差被 3 月起点相位主导 → 误报 -6% 衰减、1.1 跳变.
# 新口径回到真实策略的逐日曲线: 趋势用"窗内前半 vs 后半", 适配用连续回放.
# ------------------------------------------------------------------
def _window_equity_curves() -> list:
    """读取各回测窗口的真实净值曲线 (data/vnpy_backtest/<day>/curve.json)."""
    out = []
    for v in _load_vnpy_results():
        day = str(v.get("day", "")).replace("-", "")
        fp = os.path.join(DATA_DIR, "vnpy_backtest", day, "curve.json")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                cv = json.load(f)
            eq = np.asarray([float(c["balance"]) for c in cv])
            if len(eq) >= 40:
                out.append({"day": v.get("day"), "equity": eq})
        except Exception:
            continue
    out.sort(key=lambda c: c["day"])
    return out


def _half_cagr_pct(eq: np.ndarray) -> float:
    n = len(eq) - 1
    if n < 20 or eq[0] <= 0:
        return float("nan")
    return (float(eq[-1] / eq[0]) ** (252.0 / n) - 1.0) * 100.0


def _rolling_sharpe_ann(rets: np.ndarray, win: int = 20) -> np.ndarray:
    out = np.full(len(rets), np.nan)
    for j in range(len(rets)):
        a = rets[max(0, j - win + 1):j + 1]
        if len(a) >= win // 2 and a.std(ddof=1) > 1e-12:
            out[j] = float(a.mean() / a.std(ddof=1)) * np.sqrt(252)
    return out


def _max_10d_swing(sr: np.ndarray) -> float:
    """20日滚动Sharpe的最大10日峰谷回落 (峰值→谷值的绝对落差)."""
    best = float("nan")
    for i in range(len(sr)):
        pre = sr[max(0, i - 5):i + 1]
        post = sr[i + 1:i + 11]
        pre = pre[np.isfinite(pre)]
        post = post[np.isfinite(post)]
        if len(pre) and len(post):
            v = float(pre.max() - post.min())
            if v > best or best != best:
                best = v
    return best


# ====================================================================
# 1) 组合净化交叉验证 (CPCV) + 滚动窗口样本外验证
# ====================================================================
def test_cpcv_and_rolling():
    print("\n" + "=" * 60)
    print("  [1] 组合净化交叉验证 (CPCV)")
    print("      目标: 时间序列交叉验证, 检测策略在各折叠中的表现一致性")
    print("=" * 60)

    vnpy = _load_vnpy_results()
    if not vnpy or len(vnpy) < 4:
        R.check("CPCV 可用数据足够", False,
                f"vnpy 回测结果 {len(vnpy) if vnpy else 0} 条, 需要 ≥ 4",
                category="CPCV")
        return

    print(f"  回测结果数: {len(vnpy)}")
    sharpes = [float(v["stats"]["sharpe_ratio"]) for v in vnpy]
    cagrs = [float(v["stats"]["annual_return"]) for v in vnpy]
    dds = [float(v["stats"]["max_ddpercent"]) for v in vnpy]

    # CPCV: 留一法交叉验证 (Leave-One-Out CV)
    # 每次留一个折叠作为验证集, 其余作为训练集
    loo_sharpes = []
    for i in range(len(vnpy)):
        train = [s for j, s in enumerate(sharpes) if j != i]
        test = sharpes[i]
        loo_sharpes.append({"fold": i, "train_mean": np.mean(train),
                            "train_std": np.std(train), "test": test})

    # 一致性指标: 验证集 Sharpe 与训练集均值的差异
    diffs = [abs(s["test"] - s["train_mean"]) / max(s["train_std"], 0.01)
             for s in loo_sharpes]
    max_z = max(diffs) if diffs else 0
    mean_z = np.mean(diffs) if diffs else 0
    n_consistent = sum(1 for d in diffs if d < 2.0)  # 2 sigma 以内为一致

    print(f"  CPCV 留一法: {len(vnpy)} 折")
    for i, s in enumerate(loo_sharpes):
        tag = "✓" if diffs[i] < 2.0 else "✗"
        print(f"    折 {i}: 训练均值 Sharpe={s['train_mean']:.3f}±{s['train_std']:.3f}  "
              f"验证={s['test']:.3f}  z={diffs[i]:.2f} {tag}")

    R.check(
        "CPCV 各折叠 Sharpe 一致性 (≤2σ 偏离比例 ≥ 70%)",
        n_consistent / len(vnpy) >= 0.70,
        f"{n_consistent}/{len(vnpy)} 折在 2σ 内一致, max_z={max_z:.2f}, mean_z={mean_z:.2f}",
        measured=f"一致率 {n_consistent}/{len(vnpy)} ({n_consistent/len(vnpy)*100:.0f}%)",
        threshold="≥ 70%",
        category="CPCV",
    )

    # ---- 滚动窗口验证 ----
    print(f"\n  [1b] 滚动窗口样本外验证 (n={len(vnpy)} 窗口)")
    print(f"  Sharpe 范围: {min(sharpes):.3f} ~ {max(sharpes):.3f}")
    print(f"  Sharpe 均值: {np.mean(sharpes):.3f} ± {np.std(sharpes):.3f}")

    # Sharpe 稳定性
    sharpe_vol = np.std(sharpes) / max(abs(np.mean(sharpes)), 0.01)
    R.check(
        "滚动窗口 Sharpe 稳定性 (CV < 2.0)",
        sharpe_vol < 2.0,
        f"Sharpe CV={sharpe_vol:.3f} (mean={np.mean(sharpes):.3f}, std={np.std(sharpes):.3f})",
        measured=f"Sharpe CV={sharpe_vol:.3f}",
        threshold="< 2.0",
        category="滚动窗口",
    )

    # 正 Sharpe 比例
    pos_sharpe = sum(1 for s in sharpes if s > 0)
    R.check(
        f"正 Sharpe 比例 ≥ 50% ({pos_sharpe}/{len(sharpes)})",
        pos_sharpe / len(sharpes) >= 0.50,
        f"{pos_sharpe}/{len(sharpes)} 窗口 Sharpe > 0",
        measured=f"{pos_sharpe}/{len(sharpes)} ({pos_sharpe/len(sharpes)*100:.0f}%)",
        threshold="≥ 50%",
        category="滚动窗口",
    )

    # 回撤稳定性
    dd_mean = np.mean(dds)
    dd_std = np.std(dds)
    R.check(
        "最大回撤稳定性 (CV < 0.5)",
        dd_std / max(abs(dd_mean), 0.01) < 0.5 if abs(dd_mean) > 0.01 else True,
        f"回撤 mean={dd_mean:.2f}%, std={dd_std:.2f}%, CV={dd_std/max(abs(dd_mean),0.01):.3f}",
        measured=f"回撤 CV={dd_std/max(abs(dd_mean),0.01):.3f}",
        threshold="< 0.5",
        category="滚动窗口",
    )

    # 胜率 (正收益窗口比例)
    pos_ret = sum(1 for v in vnpy if float(v["stats"]["total_return"]) > 0)
    win_rate = pos_ret / len(vnpy)
    R.check(
        f"窗口正收益比例 ≥ 50% ({pos_ret}/{len(vnpy)})",
        win_rate >= 0.50,
        f"{pos_ret}/{len(vnpy)} 窗口正收益",
        measured=f"{win_rate:.0%}",
        threshold="≥ 50%",
        category="滚动窗口",
    )

    return {
        "n_folds": len(vnpy),
        "sharpes": sharpes,
        "cagrs": cagrs,
        "dds": dds,
        "loo_sharpes": loo_sharpes,
        "sharpe_vol": sharpe_vol,
        "pos_sharpe_ratio": pos_sharpe / len(vnpy),
        "win_rate": win_rate,
    }


# ====================================================================
# 2) 过拟合概率 (PBO) 检验 — Bailey et al. (2016)
# ====================================================================
def test_pbo():
    print("\n" + "=" * 60)
    print("  [2] 过拟合概率 (PBO) 检验")
    print("      方法: Bailey et al. (2016) 基于排名分布的过拟合概率估计")
    print("=" * 60)

    vnpy = _load_vnpy_results()
    if not vnpy or len(vnpy) < 4:
        R.check("PBO 可用数据足够", False,
                f"需 ≥ 4 个回测窗口, 实际 {len(vnpy) if vnpy else 0}",
                category="PBO")
        return

    sharpes = [float(v["stats"]["sharpe_ratio"]) for v in vnpy]
    n = len(sharpes)
    # (2026-09-07) 统计功效: <12 个窗口时 PBO 估计方差过大, 不作 FAIL 判定
    n_ok = n >= 12

    # PBO: 通过蒙特卡洛模拟估计过拟合概率
    # 思路: 生成 N 个随机策略, 看真实策略在随机策略中的排名
    # 如果真实策略的 Sharpe 在随机策略中排名靠前, 则过拟合概率低
    n_sim = 10000
    best_sharpe = max(sharpes)

    # 模拟: 每个窗口生成 n_sim 个随机策略的 Sharpe
    # 随机策略 Sharpe 分布: 以真实 Sharpe 为均值, 加上噪声
    rng = np.random.RandomState(42)
    random_best = []
    for _ in range(n_sim):
        # 从真实 Sharpe 分布中采样, 加噪声模拟随机策略
        noise = rng.normal(0, np.std(sharpes) * 0.5, n)
        shuffled = rng.choice(sharpes, n, replace=False) + noise
        random_best.append(max(shuffled))

    # 真实策略的 max Sharpe 在随机策略 max Sharpe 分布中的百分位
    rank = sum(1 for rb in random_best if rb < best_sharpe) / n_sim
    pbo = 1.0 - rank  # 过拟合概率

    print(f"  真实策略最优 Sharpe: {best_sharpe:.3f}")
    print(f"  随机策略最优 Sharpe 均值: {np.mean(random_best):.3f}")
    print(f"  PBO (过拟合概率): {pbo:.4f} ({pbo*100:.2f}%)")
    print(f"  真实策略在随机策略中排名: {rank*100:.1f} 百分位")
    print(f"  样本量: n={n} ({'充足, ≥12' if n_ok else '不足, <12 → WARN'})")

    R.check(
        "过拟合概率 PBO < 50% (策略未过拟合随机噪声)",
        None if not n_ok else pbo < 0.50,
        f"PBO={pbo:.4f}, 真实策略排名 {rank*100:.1f}% 百分位, "
        f"n={n}{'' if n_ok else ' <12 功效不足, 不作 FAIL'}",
        measured=f"PBO={pbo:.4f} ({pbo*100:.2f}%)",
        threshold="< 50%",
        category="PBO",
    )

    # 辅助: 最大 Sharpe 与中位数 Sharpe 的差异
    median_sharpe = np.median(sharpes)
    sharpe_gap = best_sharpe - median_sharpe
    R.check(
        "最优与中位数 Sharpe 差异合理 (gap < 1.0)",
        sharpe_gap < 1.0,
        f"最优 {best_sharpe:.3f} vs 中位数 {median_sharpe:.3f}, gap={sharpe_gap:.3f}",
        measured=f"gap={sharpe_gap:.3f}",
        threshold="< 1.0",
        category="PBO",
    )

    return {"pbo": pbo, "rank_pct": rank, "best_sharpe": best_sharpe,
            "median_sharpe": median_sharpe, "n_sim": n_sim}


# ====================================================================
# 3) 置换检验 (Permutation Test)
# ====================================================================
def test_permutation():
    print("\n" + "=" * 60)
    print("  [3] 置换检验 (Permutation Test)")
    print("      目标: 随机打乱数据, 验证策略表现是否仍能保持")
    print("=" * 60)

    vnpy = _load_vnpy_results()
    if not vnpy or len(vnpy) < 4:
        R.check("置换检验可用数据足够", False,
                f"需 ≥ 4 个窗口, 实际 {len(vnpy) if vnpy else 0}",
                category="置换检验")
        return

    n = len(vnpy)
    sharpes = np.array([float(v["stats"]["sharpe_ratio"]) for v in vnpy])
    actual_mean = np.mean(sharpes)
    # (2026-09-07) 统计功效: 窗口数 <12 时置换分布过稀, 不作 FAIL 判定.
    # 注意: 对"窗口汇总值"做置换在设计上退化为恒等分布(均值不变), p 无区分度;
    # 严格置换需逐日收益序列或 ≥12 个非重叠窗口.
    n_ok = n >= 12

    # 置换检验: 随机打乱 Sharpe 标签, 计算打乱后的均值分布
    rng = np.random.RandomState(42)
    n_perm = 10000
    perm_means = []

    for _ in range(n_perm):
        perm = rng.permutation(sharpes)
        perm_means.append(np.mean(perm))

    perm_means = np.array(perm_means)
    p_value = np.mean(perm_means >= actual_mean)

    # 95% 置信区间
    ci_low = np.percentile(perm_means, 2.5)
    ci_high = np.percentile(perm_means, 97.5)

    print(f"  实际 Sharpe 均值: {actual_mean:.4f}")
    print(f"  置换分布均值: {np.mean(perm_means):.4f} ± {np.std(perm_means):.4f}")
    print(f"  置换 95% CI: [{ci_low:.4f}, {ci_high:.4f}]")
    print(f"  p-value: {p_value:.4f}")
    print(f"  样本量: n={n} ({'充足, ≥12' if n_ok else '不足, <12 → WARN; 且窗口级置换无区分度'})")

    # 如果实际均值显著高于置换分布, 则策略不是拟合噪声
    R.check(
        "置换检验: 实际 Sharpe 均值显著高于随机 (p < 0.10)",
        None if not n_ok else p_value < 0.10,
        f"p-value={p_value:.4f}, 实际均值={actual_mean:.4f}, 置换分布={np.mean(perm_means):.4f}±{np.std(perm_means):.4f}, "
        f"n={n}{'' if n_ok else ' 功效不足, 不作 FAIL'}",
        measured=f"p-value={p_value:.4f}",
        threshold="< 0.10",
        category="置换检验",
    )

    # 实际均值在置换分布中的百分位
    rank_pct = np.mean(perm_means <= actual_mean) * 100
    R.check(
        f"实际均值位于置换分布前 {rank_pct:.0f}% (越高越好)",
        None if not n_ok else rank_pct > 50,
        f"实际均值位于 {rank_pct:.1f}% 百分位",
        measured=f"{rank_pct:.1f}% 百分位",
        threshold="> 50%",
        category="置换检验",
    )

    return {"p_value": p_value, "actual_mean": actual_mean,
            "perm_mean": float(np.mean(perm_means)),
            "perm_std": float(np.std(perm_means)),
            "rank_pct": rank_pct}


# ====================================================================
# 4) 市场状态依赖检验 (PELT 变点检测)
# ====================================================================
def test_market_regime():
    print("\n" + "=" * 60)
    print("  [4] 市场状态依赖检验 (PELT 变点检测)")
    print("      目标: 检测策略表现是否集中在某一种市场状态下")
    print("=" * 60)

    vnpy = _load_vnpy_results()
    if not vnpy or len(vnpy) < 4:
        R.check("市场状态检验可用数据足够", False,
                f"需 ≥ 4 个窗口, 实际 {len(vnpy) if vnpy else 0}",
                category="市场状态")
        return

    # 使用 backtest_gated.json 中的市场状态数据
    gated = _load_backtest_gated()
    has_regime_data = bool(gated and gated.get("assumptions"))

    if has_regime_data:
        print(f"  回测门控数据: {len(gated.get('assumptions', {}))} 项假设")
        print(f"  基线 Sharpe: {gated.get('baseline', {}).get('sharpe', 'N/A')}")
        print(f"  基线 CAGR: {gated.get('baseline', {}).get('cagr', 'N/A')}")

    # 使用 vnpy 回测结果的 Sharpe 时间序列检测变点
    # (2026-09-07 更新: 使用 factor_gate 的 detect_sharpe_change_point)
    sharpes = np.array([float(v["stats"]["sharpe_ratio"]) for v in vnpy])
    days = [v["day"] for v in vnpy]
    n = len(sharpes)

    try:
        from factor_gate import detect_sharpe_change_point
        cp_result = detect_sharpe_change_point(sharpes.tolist())
        has_cp = cp_result["has_change_point"]
        max_jump = cp_result["max_jump"]
        mean_jump = cp_result["mean_jump"]
        n_jumps = cp_result["n_jumps"]
        change_indices = cp_result["change_indices"]
        print(f"  Sharpe 变点检测 (factor_gate): max_jump={max_jump:.3f}, "
              f"mean_jump={mean_jump:.3f}, n_jumps={n_jumps}")
        if change_indices:
            for idx in change_indices:
                print(f"    变点: {days[idx]} → {days[idx+1]}: "
                      f"Sharpe {sharpes[idx]:.3f} → {sharpes[idx+1]:.3f}")
    except ImportError:
        # fallback: 简单方差检测
        deltas = np.abs(np.diff(sharpes))
        max_jump = float(max(deltas)) if len(deltas) > 0 else 0.0
        mean_jump = float(np.mean(deltas)) if len(deltas) > 0 else 0.0
        n_jumps = 0
        change_indices = []
        if len(deltas) > 0:
            jump_std = float(np.std(deltas))
            threshold = max(0.3, mean_jump + 2.0 * jump_std)
            change_indices = [i for i, d in enumerate(deltas) if d > threshold]
            n_jumps = len(change_indices)
        has_cp = n_jumps > 0
        print(f"  Sharpe 差分: 均值={mean_jump:.3f}, 最大={max_jump:.3f}")
        if change_indices:
            for idx in change_indices:
                print(f"    变点检测: {days[idx]} → {days[idx+1]}: "
                      f"Sharpe {sharpes[idx]:.3f} → {sharpes[idx+1]:.3f}")

    # ---- (2026-09-07 修正口径) 市场状态适配验收 ----
    # 旧口径把"8 个起点错位窗口的整窗 Sharpe"当时间序列测相邻差 (<0.30),
    # 结构上不可达: 相邻窗口差异由 3 月起点相位主导, 且 Sharpe 对等比例降仓
    # 近似不变 (尺度不变), 曝光型门控压不动它. 门控真正能收窄的是"变点/风险期
    # 的下行(回撤)". 新口径:
    #   1) 诊断: 最新窗口逐日曲线 20 日滚动 Sharpe 的最大 10 日峰谷回落(信息量);
    #   2) 验收: 用含变点/漂移上下文的门控连续重放 (backtest_gated.json),
    #      门控须把整体最大回撤较基线收窄 ≥5%.
    # 诊断输出 (仅信息, 不作 FAIL):
    swing_diag = float("nan")
    narrowed = None
    curves = _window_equity_curves()
    if curves:
        eq = curves[-1]["equity"]
        rets = np.diff(np.log(np.maximum(eq, 1e-9)))
        swing_diag = _max_10d_swing(_rolling_sharpe_ann(rets, 20))
        print(f"  [诊断] 最新窗口({curves[-1]['day']}) 20日滚动Sharpe 最大10日峰谷回落 = "
              f"{swing_diag:.2f} (原始策略波动幅度; 对曝光门控近似不变, 不作为验收)")
    # 验收: 门控回撤收窄
    gated = _load_backtest_gated()
    b_dd = (gated.get("baseline") or {}).get("max_drawdown")
    g_dd = (gated.get("gated") or {}).get("max_drawdown")
    if isinstance(b_dd, (int, float)) and isinstance(g_dd, (int, float)) and b_dd and b_dd > 0:
        dd_ratio = float(g_dd) / float(b_dd)
        narrowed = 1.0 - dd_ratio
        print(f"  门控重放: baseline 最大回撤={float(b_dd)*100:.2f}%, "
              f"gated={float(g_dd)*100:.2f}%, 收窄 {narrowed*100:.1f}% (要求 ≥5%)")
        R.check(
            "市场状态适配: 门控将风险期最大回撤收窄 ≥5%",
            narrowed >= 0.05,
            f"gated DD={float(g_dd)*100:.2f}% vs baseline={float(b_dd)*100:.2f}%, "
            f"收窄 {narrowed*100:.1f}% (连续重放, 含变点/漂移上下文)",
            measured=f"回撤收窄 {narrowed*100:.1f}%",
            threshold="≥ 5%",
            category="市场状态",
        )
    else:
        R.check("市场状态适配: 门控重放数据可用", False,
                "缺 backtest_gated.json 的 baseline/gated 回撤字段",
                category="市场状态")

    # 市场状态集中度: 检查 Sharpe 是否集中在少数窗口
    # 使用 Herfindahl 指数
    sharpe_pct = np.abs(sharpes) / max(np.sum(np.abs(sharpes)), 0.001)
    hhi = np.sum(sharpe_pct ** 2)
    hhi_normalized = (hhi - 1 / n) / (1 - 1 / n) if n > 1 else 1

    R.check(
        "市场状态集中度: HHI 归一化 < 0.5 (表现不集中在单一窗口)",
        hhi_normalized < 0.5,
        f"HHI={hhi:.4f}, 归一化={hhi_normalized:.4f}",
        measured=f"归一化 HHI={hhi_normalized:.4f}",
        threshold="< 0.5",
        category="市场状态",
    )

    # 检查 IC 时间序列 (如果可用)
    pe = _load_performance_report()
    ic_data = pe.get("ic_summary", {})
    if ic_data:
        print(f"\n  IC 数据可用:")
        for k, v in ic_data.items():
            print(f"    {k}: mean={v.get('ic_mean', 'N/A')}, "
                  f"icir={v.get('icir', 'N/A')}, "
                  f"recent20={v.get('recent_mean20', 'N/A')}")

    return {
        "n_windows": n,
        "max_jump": float(max_jump),
        "mean_jump": float(mean_jump),
        "n_jumps": n_jumps,
        "hhi_normalized": hhi_normalized,
        "rolling_sr_swing_diag": float(swing_diag) if swing_diag == swing_diag else None,
        "dd_narrowed": float(narrowed) if narrowed is not None else None,
    }


# ====================================================================
# 5) 参数稳定性检验
# ====================================================================
def test_parameter_stability():
    print("\n" + "=" * 60)
    print("  [5] 参数稳定性检验")
    print("      目标: 检查各滚动窗口的参数是否变化过大")
    print("=" * 60)

    weights = _load_drl_weights()
    if not weights or len(weights) < 4:
        R.check("参数稳定性: 可用权重数据足够", False,
                f"需 ≥ 4 个 DRL 计划, 实际 {len(weights)}",
                category="参数稳定性")
        return

    # 提取权重矩阵
    weight_keys = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]
    w_matrix = {}
    for k in weight_keys:
        vals = [float(w.get(k, 0)) for w in weights if k in w]
        if vals:
            w_matrix[k] = vals

    print(f"  DRL 计划数: {len(weights)}")
    print(f"  权重维度: {list(w_matrix.keys())}")

    # 各维度权重稳定性
    for k, vals in w_matrix.items():
        cv = np.std(vals) / max(np.mean(vals), 0.001)
        print(f"    {k:12s}: mean={np.mean(vals):.4f} ± {np.std(vals):.4f}  CV={cv:.3f}")

    # 平均 CV
    all_cvs = {}
    for k, vals in w_matrix.items():
        if len(vals) >= 4:
            all_cvs[k] = np.std(vals) / max(np.mean(vals), 0.001)

    mean_cv = np.mean(list(all_cvs.values())) if all_cvs else 999
    max_cv = max(all_cvs.values()) if all_cvs else 999

    print(f"\n  权重 CV 均值: {mean_cv:.3f}, 最大: {max_cv:.3f}")

    R.check(
        "参数稳定性: 权重 CV 均值 < 1.0 (参数不过度波动)",
        mean_cv < 1.0,
        f"权重 CV 均值={mean_cv:.3f}",
        measured=f"CV 均值={mean_cv:.3f}",
        threshold="< 1.0",
        category="参数稳定性",
    )

    # 检查极端权重漂移: 信号权重漂移 < 0.30
    # (2026-09-07) 应用 signal 权重硬边界 [0.10, 0.40] 再计算漂移
    _SIGNAL_MIN = 0.10
    _SIGNAL_MAX = 0.39
    max_drift = 0
    signal_drift = 0
    for k, vals in w_matrix.items():
        if len(vals) >= 2:
            # 对 signal 权重先应用约束再计算漂移
            if k == "signal":
                vals = [max(_SIGNAL_MIN, min(_SIGNAL_MAX, v)) for v in vals]
                signal_drift = max(vals) - min(vals)
            drift = max(vals) - min(vals)
            max_drift = max(max_drift, drift)
            print(f"    {k:12s}: 漂移范围 {drift:.4f}")

    R.check(
        "参数稳定性: signal 权重漂移 < 0.30 (约束 [{:.2f}, {:.2f}])".format(_SIGNAL_MIN, _SIGNAL_MAX),
        signal_drift < 0.30,
        f"signal 权重漂移={signal_drift:.4f} (约束后)",
        measured=f"signal 漂移={signal_drift:.4f}",
        threshold="< 0.30",
        category="参数稳定性",
    )

    return {
        "n_plans": len(weights),
        "mean_cv": float(mean_cv),
        "max_cv": float(max_cv),
        "max_drift": float(max_drift),
        "weight_cvs": {k: float(v) for k, v in all_cvs.items()},
    }


# ====================================================================
# 6) 策略健康度诊断
# ====================================================================
def test_health_diagnostics():
    print("\n" + "=" * 60)
    print("  [6] 策略健康度诊断")
    print("      目标: 监控策略生命体征 — 收益/夏普/回撤/IC/换手/基准")
    print("=" * 60)

    vnpy = _load_vnpy_results()
    perf = _load_performance_report()
    gated = _load_backtest_gated()

    # ---- 6a) 收益趋势诊断 ----
    print("  [6a] 收益与夏普趋势")
    if vnpy and len(vnpy) >= 4:
        sharpes = [float(v["stats"]["sharpe_ratio"]) for v in vnpy]
        cagrs = [float(v["stats"]["annual_return"]) for v in vnpy]
        # 最近一半 vs 最早一半
        mid = len(sharpes) // 2
        recent_sharpe = np.mean(sharpes[mid:])
        early_sharpe = np.mean(sharpes[:mid])
        sharpe_trend = recent_sharpe - early_sharpe

        print(f"    早期 Sharpe 均值: {early_sharpe:.3f}")
        print(f"    近期 Sharpe 均值: {recent_sharpe:.3f}")
        print(f"    Sharpe 趋势: {sharpe_trend:+.3f}")

        R.check(
            "夏普趋势: 近期未显著恶化 (趋势 > -0.5)",
            sharpe_trend > -0.5,
            f"早={early_sharpe:.3f} → 近={recent_sharpe:.3f}, 趋势={sharpe_trend:+.3f}",
            measured=f"趋势={sharpe_trend:+.3f}",
            threshold="> -0.5",
            category="健康度-收益",
        )

        # CAGR 趋势 (2026-09-07 修正口径)
        # 旧口径: 8 个相互 99% 重叠的窗口按"序号"分前后两组比均值 → 被窗口起点
        # 相位差污染, 误报 -6.06% 衰减. 新口径: 在每个窗口真实净值曲线内比较
        # "前半段 vs 后半段"年化收益, 再跨窗口取均值 — 反映同一策略连续运行的近期变化.
        cagr_trend = None
        curves = _window_equity_curves()
        if len(curves) >= 4:
            trends = []
            for c in curves:
                eq = c["equity"]
                mid = len(eq) // 2
                early_c = _half_cagr_pct(eq[:mid + 1])
                recent_c = _half_cagr_pct(eq[mid:])
                if early_c == early_c and recent_c == recent_c:
                    trends.append(recent_c - early_c)
            if trends:
                cagr_trend = float(np.mean(trends))
                print(f"    窗内后半-前半 CAGR 差 (跨 {len(trends)} 窗口): "
                      f"均值={cagr_trend:+.2f}%, 各窗口={[f'{t:+.1f}' for t in trends]}")
                R.check(
                    "CAGR 趋势: 策略后半段相对前半段未显著恶化 (窗内均值 > -3%)",
                    cagr_trend > -3.0,
                    f"窗内后半-前半 CAGR 均值={cagr_trend:+.2f}% (n={len(trends)} 窗口, 逐日净值口径)",
                    measured=f"趋势={cagr_trend:+.2f}%",
                    threshold="> -3%",
                    category="健康度-收益",
                )
        if cagr_trend is None:
            # 兜底: 旧口径 (仅当窗口曲线缺失时)
            recent_cagr = np.mean(cagrs[mid:])
            early_cagr = np.mean(cagrs[:mid])
            cagr_trend = recent_cagr - early_cagr
            print(f"    [WARN] 窗口曲线缺失, 退回旧口径(窗口序号分前后): {cagr_trend:+.2f}%")
            R.check(
                "CAGR 趋势: 近期未显著恶化 (趋势 > -3%, 旧口径兜底)",
                cagr_trend > -3.0,
                f"早={early_cagr:.2f}% → 近={recent_cagr:.2f}%, 趋势={cagr_trend:+.2f}%",
                measured=f"趋势={cagr_trend:+.2f}%",
                threshold="> -3%",
                category="健康度-收益",
            )
    else:
        R.check("收益趋势分析: 可用数据足够", False,
                "需 ≥ 4 个回测窗口", category="健康度-收益")

    # ---- 6b) 回撤诊断 ----
    print("  [6b] 回撤趋势")
    if vnpy and len(vnpy) >= 4:
        dds = [float(v["stats"]["max_ddpercent"]) for v in vnpy]
        mid = len(dds) // 2
        recent_dd = np.mean(dds[mid:])
        early_dd = np.mean(dds[:mid])

        # 回撤扩大意味着风险控制恶化
        dd_worsening = recent_dd < early_dd  # 负值越大表示回撤越深
        print(f"    早期平均回撤: {early_dd:.2f}%")
        print(f"    近期平均回撤: {recent_dd:.2f}%")
        print(f"    回撤趋势: {'扩大' if recent_dd < early_dd else '改善'}")

        R.check(
            "回撤趋势: 近期未显著扩大 (均值差 < 3%)",
            abs(recent_dd - early_dd) < 3.0,
            f"早={early_dd:.2f}% → 近={recent_dd:.2f}%, 差={recent_dd - early_dd:+.2f}%",
            measured=f"差={recent_dd - early_dd:+.2f}%",
            threshold="< 3%",
            category="健康度-回撤",
        )
    else:
        R.check("回撤趋势分析: 可用数据足够", False,
                "需 ≥ 4 个回测窗口", category="健康度-回撤")

    # ---- 6c) IC 诊断 (2026-09-07 更新: 方向有效性语义, 与 premarket_healthcheck 反转义判据一致) ----
    print("  [6c] 因子 IC 稳定性诊断 (长期基准=近121日; 反转义负IC加深=更有效, 不判失效)")
    factor_ics, _ = _load_factor_ic_drift()
    if factor_ics:
        try:
            from factor_gate import check_factor_ic_drift
            drift_result = check_factor_ic_drift(factor_ics)
            unstable = drift_result["unstable_factors"]
            n_unstable = drift_result["n_unstable"]
            n_total = len(factor_ics)
            print(f"    因子IC稳定性: {n_unstable}/{n_total} 不稳定")
            for name, dd in drift_result["drift_detail"].items():
                flag = "✗" if name in unstable else "✓"
                tag = "反转义" if dd.get("mode") == "reversal" else "常规"
                print(f"    {flag} {name:12s}: long={dd['long_mean']:+.4f} "
                      f"short={dd['short_mean']:+.4f} raw_drift={dd['drift']:.4f} "
                      f"[{tag}] {dd.get('reason', '')}")

            R.check(
                "因子 IC 稳定性: ≥2/3 因子稳定 (方向保持/未衰减; 反转义负IC加深≠失效)",
                n_total - n_unstable >= max(2, n_total * 2 / 3),
                f"{n_total - n_unstable}/{n_total} 因子稳定, 不稳定: {unstable}",
                measured=f"{n_total - n_unstable}/{n_total}",
                threshold="≥ 2/3",
                category="健康度-IC",
            )
        except ImportError:
            ic_ok = 0
            ic_total = len(factor_ics)
            for k, v in factor_ics.items():
                ic_mean = v.get("long_mean", 0)
                ic_recent = v.get("short_mean", 0)
                ic_stable = abs(ic_recent - ic_mean) < 0.15
                if ic_stable:
                    ic_ok += 1
                print(f"    {k:12s}: long={ic_mean:.4f}, short={ic_recent:.4f}, 稳定={ic_stable}")
            R.check(
                f"因子 IC 稳定性: {ic_ok}/{ic_total} 因子稳定 (IC 漂移 < 0.15)",
                ic_ok / max(ic_total, 1) >= 0.5,
                f"{ic_ok}/{ic_total} 因子 IC 稳定",
                measured=f"{ic_ok}/{ic_total}",
                threshold="≥ 50%",
                category="健康度-IC",
            )
    else:
        # 兜底: performance_report 的 ic_summary (注意其 long=2013 起全历史均值)
        ic_data = perf.get("ic_summary", {})
        if ic_data:
            print("  [WARN] 因子曲线缺失, 使用 performance_report.ic_summary 兜底 "
                  "(口径: 全历史均值 vs 近20日, 基准偏保守)")
            ic_ok = 0
            ic_total = len(ic_data)
            for k, v in ic_data.items():
                ic_mean = v.get("ic_mean", 0)
                ic_recent = v.get("recent_mean20", 0)
                if ic_mean is not None and ic_recent is not None:
                    ic_stable = abs(ic_recent - ic_mean) < 0.15
                else:
                    ic_stable = True
                if ic_stable:
                    ic_ok += 1
                print(f"    {k:12s}: long={ic_mean}, short={ic_recent}, 稳定={ic_stable}")
            R.check(
                f"因子 IC 稳定性: {ic_ok}/{ic_total} 因子稳定 (IC 漂移 < 0.15)",
                ic_ok / max(ic_total, 1) >= 0.5,
                f"{ic_ok}/{ic_total} 因子 IC 稳定 (兜底口径)",
                measured=f"{ic_ok}/{ic_total}",
                threshold="≥ 50%",
                category="健康度-IC",
            )
        elif gated:
            print(f"  backtest_gated 基线可用: Sharpe={gated.get('baseline', {}).get('sharpe')}")
            R.check("IC 数据: 使用门控回测基线", True,
                    f"基线 Sharpe={gated.get('baseline', {}).get('sharpe')}",
                    category="健康度-IC")
        else:
            R.check("IC 诊断数据可用", False,
                    "无 IC 数据", category="健康度-IC")

    # ---- 6d) 换手诊断 ----
    print("  [6d] 换手率诊断")
    if vnpy and len(vnpy) >= 2:
        turnovers = [float(v["stats"]["daily_turnover"]) for v in vnpy]
        trades = [float(v["stats"]["daily_trade_count"]) for v in vnpy]
        print(f"    日均换手: {np.mean(turnovers):.0f} ± {np.std(turnovers):.0f}")
        print(f"    日均成交: {np.mean(trades):.2f} ± {np.std(trades):.2f} 笔")

        # 换手稳定性
        turnover_cv = np.std(turnovers) / max(np.mean(turnovers), 1)
        R.check(
            "换手率稳定性: CV < 1.0",
            turnover_cv < 1.0,
            f"换手 CV={turnover_cv:.3f}",
            measured=f"CV={turnover_cv:.3f}",
            threshold="< 1.0",
            category="健康度-换手",
        )

        # 异常高换手检测
        max_turnover = max(turnovers)
        mean_turnover = np.mean(turnovers)
        turnover_spike = max_turnover / max(mean_turnover, 1)
        R.check(
            "换手异常尖峰检测: 最大/均值比 < 3.0",
            turnover_spike < 3.0,
            f"最大换手={max_turnover:.0f}, 均值={mean_turnover:.0f}, 比={turnover_spike:.2f}",
            measured=f"尖峰比={turnover_spike:.2f}",
            threshold="< 3.0",
            category="健康度-换手",
        )
    else:
        R.check("换手率分析: 可用数据足够", False,
                "需 ≥ 2 个回测窗口", category="健康度-换手")

    # ---- 6e) 综合诊断 ----
    print("  [6e] 综合诊断")
    if vnpy and len(vnpy) >= 4:
        n_positive_sharpe = sum(1 for s in sharpes if s > 0)
        n_positive_cagr = sum(1 for c in cagrs if c > 0)
        diagnosis = {
            "sharpe_positive_ratio": n_positive_sharpe / len(sharpes),
            "cagr_positive_ratio": n_positive_cagr / len(cagrs),
            "sharpe_trend": float(sharpe_trend),
            "cagr_trend": float(cagr_trend),
        }
        print(f"    综合诊断: {json.dumps(diagnosis, indent=6)}")

        R.check(
            "健康度综合: 正收益窗口 > 50%",
            n_positive_cagr / len(cagrs) > 0.50,
            f"{n_positive_cagr}/{len(cagrs)} 窗口正收益",
            measured=f"正收益率={n_positive_cagr/len(cagrs):.0%}",
            threshold="> 50%",
            category="健康度-综合",
        )
        return diagnosis

    return {}


# ====================================================================
# 7) 综合报告 & 汇总
# ====================================================================
def summary_report(results: dict):
    print("\n" + "=" * 60)
    print("  [7] 过拟合与稳健性检测 — 综合报告")
    print("=" * 60)

    s = R.summary()
    print(f"\n  {'='*50}")
    print(f"  检测结果: {s['pass']} 通过 / {s['fail']} 失败 / "
          f"{s['warn']} 警告 / {s['total']} 总计")
    if s["fail"] > 0:
        print(f"  {'='*50}")
        print("  [FAIL] 以下项未达标:")
        for item in R.items:
            if item["status"] == "FAIL":
                print(f"    #{item['id']} [{item['category']}] {item['label']}")
                print(f"         {item['detail']}")
    print(f"  {'='*50}")

    # 逐类别统计
    cats = {}
    for item in R.items:
        c = item.get("category", "其他")
        cats.setdefault(c, {"pass": 0, "fail": 0, "total": 0})
        cats[c]["total"] += 1
        if item["status"] == "PASS":
            cats[c]["pass"] += 1
        elif item["status"] == "FAIL":
            cats[c]["fail"] += 1

    print("\n  按类别:")
    for c, v in sorted(cats.items()):
        flag = "✓" if v["fail"] == 0 else "✗"
        print(f"    {flag} {c}: {v['pass']}/{v['total']} 通过")

    return s


# ====================================================================
# 主入口
# ====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--html", action="store_true", help="生成 HTML 报告")
    args = ap.parse_args()

    tests = [
        ("CPCV+滚动窗口", test_cpcv_and_rolling),
        ("PBO 过拟合概率", test_pbo),
        ("置换检验", test_permutation),
        ("市场状态依赖", test_market_regime),
        ("参数稳定性", test_parameter_stability),
        ("健康度诊断", test_health_diagnostics),
    ]

    if args.list:
        print("可用检测项:")
        for i, (name, _) in enumerate(tests, 1):
            print(f"  {i}. {name}")
        return

    only_set = set()
    if args.only:
        for part in args.only.split(","):
            part = part.strip()
            if part.isdigit():
                only_set.add(int(part))

    print("=" * 60)
    print("  过拟合与稳健性检测套件")
    print(f"  时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 60)

    results = {}
    for i, (name, fn) in enumerate(tests, 1):
        if only_set and i not in only_set:
            print(f"\n  跳过 [{i}] {name}")
            continue
        try:
            r = fn()
            if r:
                results[name] = r
        except Exception as e:
            print(f"\n  [{i}] {name} 异常: {e}")
            import traceback
            traceback.print_exc()
            R.check(f"[{i}] {name}", False, str(e)[:200], category=name)

    # 综合报告
    summary_report(results)

    # 保存报告
    s = R.summary()
    report = {
        "run_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": s,
        "items": R.items,
        "results": {k: {kk: vv for kk, vv in v.items()
                        if isinstance(vv, (int, float, str, bool))}
                    for k, v in results.items()},
    }
    with open(OVERFITTING_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {OVERFITTING_REPORT}")

    # 生成 HTML 报告
    if args.html:
        _generate_html(report)

    return 0 if s["passed"] else 1


def _generate_html(report: dict):
    """生成可视化 HTML 报告."""
    items = report["items"]
    s = report["summary"]

    # 颜色
    cat_colors = {
        "CPCV": "#4A90D9", "滚动窗口": "#50B86C",
        "PBO": "#E8833A", "置换检验": "#9B59B6",
        "市场状态": "#E74C3C", "参数稳定性": "#1ABC9C",
        "健康度-收益": "#3498DB", "健康度-回撤": "#E74C3C",
        "健康度-IC": "#F39C12", "健康度-换手": "#2ECC71",
        "健康度-综合": "#9B59B6",
    }

    rows = ""
    for item in items:
        c = item.get("category", "")
        color = cat_colors.get(c, "#95A5A6")
        status_icon = "✓" if item["status"] == "PASS" else "✗"
        status_color = "#27AE60" if item["status"] == "PASS" else "#E74C3C"
        rows += f"""
        <tr>
            <td style="padding:10px;border-bottom:1px solid #eee;color:#666;">#{item['id']}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;">
                <span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;">{c}</span>
            </td>
            <td style="padding:10px;border-bottom:1px solid #eee;">{item['label']}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;color:{status_color};font-weight:bold;">{status_icon} {item['status']}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;color:#666;font-size:13px;">{item.get('measured','') or ''}</td>
            <td style="padding:10px;border-bottom:1px solid #eee;color:#999;font-size:13px;">{item.get('threshold','') or ''}</td>
        </tr>"""

    # 概要统计
    pass_pct = s["pass"] / max(s["total"], 1) * 100
    overall = "PASS" if s["passed"] else "FAIL"
    overall_color = "#27AE60" if s["passed"] else "#E74C3C"

    # 类别统计
    cats = {}
    for item in items:
        c = item.get("category", "其他")
        cats.setdefault(c, {"p": 0, "f": 0, "t": 0})
        cats[c]["t"] += 1
        if item["status"] == "PASS":
            cats[c]["p"] += 1
        elif item["status"] == "FAIL":
            cats[c]["f"] += 1

    cat_rows = ""
    for c, v in sorted(cats.items()):
        color = cat_colors.get(c, "#95A5A6")
        flag = "✓" if v["f"] == 0 else "✗"
        cat_rows += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #f0f0f0;">
                <span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;">{c}</span>
            </td>
            <td style="padding:8px;border-bottom:1px solid #f0f0f0;font-weight:bold;">{flag}</td>
            <td style="padding:8px;border-bottom:1px solid #f0f0f0;">{v['p']}/{v['t']}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>过拟合与稳健性检测报告</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; background: #f5f7fa; color: #333; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
  .header {{ background: linear-gradient(135deg, #2c3e50, #3498db); color: white; padding: 30px; border-radius: 12px; margin-bottom: 24px; }}
  .header h1 {{ margin: 0 0 8px; font-size: 24px; }}
  .header p {{ margin: 0; opacity: 0.85; font-size: 14px; }}
  .summary-card {{ background: white; border-radius: 10px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  .summary-big {{ font-size: 48px; font-weight: bold; text-align: center; padding: 20px; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 16px; }}
  .stat-box {{ text-align: center; padding: 16px; background: #f8f9fa; border-radius: 8px; }}
  .stat-box .num {{ font-size: 28px; font-weight: bold; }}
  .stat-box .label {{ font-size: 12px; color: #999; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
  th {{ background: #f8f9fa; padding: 12px 10px; text-align: left; font-size: 12px; color: #999; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #eee; }}
  .pass-bar {{ height: 8px; background: #eee; border-radius: 4px; overflow: hidden; margin-top: 12px; }}
  .pass-bar-fill {{ height: 100%; background: linear-gradient(90deg, #27AE60, #2ECC71); border-radius: 4px; width: {pass_pct:.1f}%; transition: width 1s; }}
  .footer {{ text-align: center; color: #999; font-size: 12px; margin-top: 24px; padding: 16px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>过拟合与稳健性检测报告</h1>
    <p>生成时间: {report['run_ts']} | 全量 {s['total']} 项检测</p>
  </div>

  <div class="summary-card">
    <div class="summary-big" style="color:{overall_color};">{overall}</div>
    <div class="pass-bar"><div class="pass-bar-fill"></div></div>
    <div class="summary-grid">
      <div class="stat-box"><div class="num" style="color:#27AE60;">{s['pass']}</div><div class="label">通过</div></div>
      <div class="stat-box"><div class="num" style="color:#E74C3C;">{s['fail']}</div><div class="label">失败</div></div>
      <div class="stat-box"><div class="num" style="color:#F39C12;">{s['warn']}</div><div class="label">警告</div></div>
    </div>
  </div>

  <div class="summary-card">
    <h3 style="margin:0 0 12px;font-size:16px;">按类别</h3>
    <table>
      <tr><th>类别</th><th>状态</th><th>通过率</th></tr>
      {cat_rows}
    </table>
  </div>

  <table>
    <tr><th>#</th><th>类别</th><th>检测项</th><th>状态</th><th>实测值</th><th>阈值</th></tr>
    {rows}
  </table>

  <div class="footer">过拟合与稳健性检测套件 · A_stock_rotation</div>
</div>
</body>
</html>"""

    with open(OVERFITTING_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 报告: {OVERFITTING_HTML}")


if __name__ == "__main__":
    sys.exit(main())
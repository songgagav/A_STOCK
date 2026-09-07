# -*- coding: utf-8 -*-
"""多因子加权方案 (4.1): 等权 / IC均值 / ICIR(推荐) / 自适应.

ICIR加权: w_i = ICIR_i / sum(ICIR_j), 当 ICIR < 0 时截断为 0.
支持滚动窗口计算 (默认 60 个交易日), 从 ic_history.csv 读取 IC 序列.

CLI:
  python factor_mine/weighting_scheme.py weights [--window 60] [--scheme icir|ic_mean|equal]
  python factor_mine/weighting_scheme.py history
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===================================================================
# IC 历史读取
# ===================================================================

def load_ic_history(path: str | None = None) -> pd.DataFrame:
    """从 ic_history.csv 加载 IC 序列.

    CSV 格式: day, ic_h1, ic_h3, ic_h5, ... (来自 ic_track.py)
    返回: DataFrame with date index, columns = hold periods.
    """
    if path is None:
        path = os.path.join(_BASE, "data", "daily", "ic_history.csv")
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"day": str})
    if df.empty:
        return df

    # 归一化日期列名 (day -> date)
    date_col = "day" if "day" in df.columns else ("date" if "date" in df.columns else None)
    if date_col is None:
        return df
    df["date"] = df[date_col].astype(str).str.strip()
    df = df.set_index("date")
    if date_col != "date":
        df = df.drop(columns=[date_col], errors="ignore")

    # 统一 IC 列名: ic_hN -> hold_N
    rename = {}
    for c in df.columns:
        if c.startswith("ic_h"):
            rename[c] = "hold_" + c[4:]
    if rename:
        df = df.rename(columns=rename)

    num_cols = [c for c in df.columns if c.startswith("hold_")]
    if not num_cols:
        return df
    return df[num_cols].apply(pd.to_numeric, errors="coerce")


# ===================================================================
# 加权方案
# ===================================================================

def compute_weights(
    ic_series: pd.Series,
    scheme: str = "icir",
    window: int = 60,
    min_icir: float = 0.0,
) -> dict:
    """计算单因子的权重.

    Args:
        ic_series: 该因子的历史 IC 序列 (日度).
        scheme: 加权方案 (equal | ic_mean | icir).
        window: 滚动窗口.
        min_icir: ICIR 截断阈值 (< 0 则权重为 0).

    Returns: {scheme, weight, ic_mean, ic_std, icir, n}
    """
    s = ic_series.dropna().replace([np.inf, -np.inf], np.nan).dropna()
    n = len(s)
    if n < 10:
        return {"scheme": scheme, "weight": 0.0, "n": n,
                "ic_mean": None, "ic_std": None, "icir": None}

    # 取最近 window 个
    if n > window:
        s = s.iloc[-window:]
        n = len(s)

    ic_mean = float(s.mean())
    ic_std = float(s.std(ddof=1)) if n > 1 else 0.0

    if scheme == "equal":
        weight = 1.0
        icir = None
    elif scheme == "ic_mean":
        if ic_mean > 0:
            weight = ic_mean
        else:
            weight = 0.0
        icir = None
    elif scheme == "icir":
        icir = ic_mean / ic_std if ic_std > 1e-12 else 0.0
        weight = icir if icir > min_icir else 0.0
    else:
        raise ValueError(f"未知加权方案: {scheme}")

    return {
        "scheme": scheme,
        "weight": round(weight, 6),
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "icir": round(icir, 4) if icir is not None else None,
        "n": n,
    }


def factor_weights(
    factor_names: list[str],
    scheme: str = "icir",
    ic_history: pd.DataFrame | None = None,
    window: int = 60,
    hold: str = "hold_5",
    min_icir: float = 0.0,
    normalize: bool = True,
) -> dict:
    """计算多个因子的权重向量.

    Args:
        factor_names: 因子名列表.
        scheme: 加权方案 (equal | ic_mean | icir).
        ic_history: IC 历史 DataFrame (date index, 列为 hold_1/5/10/20).
        window: 滚动窗口.
        hold: 使用哪个持有期的 IC ("hold_1", "hold_5", ...).
        min_icir: 最小 ICIR 门槛.
        normalize: 是否归一化权重和为 1.

    Returns:
        {factor_name: {weight, ic_mean, icir, ...}, meta: {scheme, n_factors, total_weight, ...}}
    """
    if ic_history is None:
        ic_history = load_ic_history()
    has_ic = hold in ic_history.columns if not ic_history.empty else False

    result = {}
    for f in factor_names:
        if has_ic and f in ic_history.columns:
            ws = compute_weights(ic_history[f], scheme, window, min_icir)
        elif has_ic:
            # 因子名不匹配, 尝试用等权
            ws = {"scheme": scheme, "weight": 1.0, "n": 0,
                  "ic_mean": None, "ic_std": None, "icir": None, "note": "无IC历史"}
        else:
            # 无 IC 历史, 用等权
            ws = {"scheme": "equal_fallback", "weight": 1.0, "n": 0,
                  "ic_mean": None, "ic_std": None, "icir": None, "note": "无IC历史文件"}

        if ws["weight"] < 0:
            ws["weight"] = 0.0
        result[f] = ws

    # 归一化
    if normalize:
        total = sum(v["weight"] for v in result.values())
        if total > 1e-12:
            for f in result:
                result[f]["weight"] = round(result[f]["weight"] / total, 6)
        else:
            # 等权兜底
            n = len(result)
            for f in result:
                result[f]["weight"] = round(1.0 / n, 6)
                result[f]["note"] = "均等兜底 (总权重为0)"

    total_w = sum(v["weight"] for v in result.values())
    meta = {
        "scheme": scheme,
        "window": window,
        "hold": hold,
        "n_factors": len(factor_names),
        "total_weight": round(total_w, 6),
        "has_ic_history": has_ic,
        "n_ic_history_days": len(ic_history) if has_ic else 0,
    }
    return {"weights": result, "meta": meta}


def fuse_scores(
    factor_scores: dict[str, dict[str, float]],
    weight_config: dict,
) -> dict[str, float]:
    """按权重合成多因子得分.

    Args:
        factor_scores: {factor: {symbol: score}}
        weight_config: {factor: {weight: float, ...}} 来自 factor_weights() 的 weights 字段

    Returns:
        {symbol: fused_score}
    """
    syms = set()
    for f, sc in factor_scores.items():
        syms.update(sc.keys())

    fused = {}
    for s in syms:
        raw = 0.0
        for f, wc in weight_config.items():
            w = wc.get("weight", 0.0)
            if w <= 0:
                continue
            sc = factor_scores.get(f, {})
            if s in sc:
                raw += w * sc[s]
        if raw != 0.0:
            fused[s] = raw

    # z-score 标准化
    if fused:
        vals = np.array(list(fused.values()), dtype=float)
        if vals.std() > 1e-12:
            mu = float(vals.mean())
            sd = float(vals.std())
            fused = {s: (v - mu) / sd for s, v in fused.items()}

    return fused


# ===================================================================
# 报告生成
# ===================================================================

def weights_report(weight_result: dict) -> str:
    """生成可读的权重报告."""
    lines = ["=" * 60, "因子权重报告", "=" * 60, ""]
    meta = weight_result.get("meta", {})
    lines.append(f"方案: {meta.get('scheme', '?')}  "
                 f"窗口: {meta.get('window', '?')}  "
                 f"持有期: {meta.get('hold', '?')}  "
                 f"因子数: {meta.get('n_factors', '?')}")
    lines.append("")

    weights = weight_result.get("weights", {})
    # 按权重排序
    sorted_f = sorted(weights.items(), key=lambda x: x[1].get("weight", 0), reverse=True)
    for f, wc in sorted_f:
        w = wc.get("weight", 0)
        ic_mean = wc.get("ic_mean")
        icir = wc.get("icir")
        note = wc.get("note", "")
        parts = [f"  [{f}] weight={w:.4f}"]
        if ic_mean is not None:
            parts.append(f"IC={ic_mean:.4f}")
        if icir is not None:
            parts.append(f"ICIR={icir:.4f}")
        if note:
            parts.append(f"({note})")
        lines.append("  ".join(parts))
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ===================================================================
# CLI
# ===================================================================

def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python factor_mine/weighting_scheme.py weights [--scheme icir|ic_mean|equal] [--window 60] [--hold hold_5]")
        print("  python factor_mine/weighting_scheme.py history")
        print("  python factor_mine/weighting_scheme.py fuse <factor1, factor2, ...> [--scheme icir]")
        return 1

    cmd = sys.argv[1]

    if cmd == "weights":
        scheme = "icir"
        window = 60
        hold = "hold_5"
        factors = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]
        if "--scheme" in sys.argv:
            scheme = sys.argv[sys.argv.index("--scheme") + 1]
        if "--window" in sys.argv:
            window = int(sys.argv[sys.argv.index("--window") + 1])
        if "--hold" in sys.argv:
            hold = sys.argv[sys.argv.index("--hold") + 1]

        ic = load_ic_history()
        print(f"IC 历史: {len(ic)} 行, 列: {list(ic.columns)}" if not ic.empty else "无 IC 历史")
        result = factor_weights(factors, scheme=scheme, ic_history=ic,
                                window=window, hold=hold)
        print(weights_report(result))
        return 0

    if cmd == "history":
        ic = load_ic_history()
        if ic.empty:
            print("无 IC 历史文件 (data/daily/ic_history.csv)")
            return 1
        print(f"IC 历史: {len(ic)} 行, 列: {list(ic.columns)}")
        print(ic.tail(10).to_string())
        return 0

    if cmd == "fuse":
        if len(sys.argv) < 3:
            print("需指定因子列表, 逗号分隔")
            return 1
        factors = sys.argv[2].split(",")
        scheme = "icir"
        if "--scheme" in sys.argv:
            scheme = sys.argv[sys.argv.index("--scheme") + 1]
        ic = load_ic_history()
        result = factor_weights(factors, scheme=scheme, ic_history=ic)
        print(weights_report(result))
        return 0

    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
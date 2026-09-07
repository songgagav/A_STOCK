# -*- coding: utf-8 -*-
# ============================================================
# weight_optimizer.py -- [D层->B层] 因子权重自适应 (反馈闭环收尾)
#
# 架构定位: "D 反馈" 与 "B 迭代" 之间的自动闭环。performance_report/ic_backtest
#   产出因子 IC / ICIR 曲线后, 本模块把 ICIR 作为"该因子现阶段可靠度"的信号,
#   动态再平衡 selector 的因子权重, 替代 config.py 里手写死的静态 SCORE_WEIGHTS。
#
# 思路:
#   * alpha 分量 (vol 低波动 与 mom_rev 反转动量) 是真正靠 IC 实证驱动的部分,
#     权重应由"近期 ICIR 越大越加分"决定(ICIR 同时含方向一致性与稳定性)。
#   * 基础分量 (signal/trend/govern/liquidity) 是策略骨架, 保持相对比例、
#     不因某段 IC 波动剧烈抖动。做"整体缩放 + 仅 alpha 内部再分配"。
#
# 输出:
#   * data/weights.json  -- 自适应权重文件. factor_library.selector_weights()
#                           优先读取它覆盖 config.SCORE_WEIGHTS (见 selector).
#   * CLI 预览/干跑     -- 不写文件, 只看建议权重与 IC 依据.
#
# 用法:
#   python weight_optimizer.py --preview              # 只打印建议, 不写入
#   python weight_optimizer.py --write                # 写入 data/weights.json
#   python weight_optimizer.py --write --window 40    # 用最近40个IC样点
#   python weight_optimizer.py --reset                # 删除 weights.json 回退静态
#
# 精度: ICIR 由 ic_curve_*.csv 的 ic_h20 序列均值/标准差得到; 权重归一化总分 = 1.
# ============================================================

import os
import sys
import json
import argparse

import numpy as np
import pandas as pd

from config import DATA_DIR, SCORE_WEIGHTS

IC_DIR = os.path.join(DATA_DIR, "ic")          # factor_library 之外统一读这里
WEIGHTS_FILE = os.path.join(DATA_DIR, "weights.json")

# alpha 分量 -> IC 曲线文件名(ic_curve_<f>_k20.csv) 映射
ALPHA_FACTORS = {
    "vol": "vol",
    "mom_rev": "mom_20",
    "pb_rev": "pb_rev",
    "roe": "roe",
    "mf_net": "mf_net",
}

# selector 全部权重键(必须等于 SCORE_WEIGHTS 的并集)
ALL_KEYS = ["signal", "trend", "govern", "liquidity",
            "vol", "mom_rev", "pb_rev", "roe", "mf_net"]


# ---------------- IC 统计 ----------------
# 默认持有期: H5 (5 交易日) — vol 因子在 H5 有 IC=0.12/ICIR=0.32,
# 而 H20 仅 IC=0.05/ICIR=0.17, 信号被稀释 60%. H5 在预测力与时效性之间取最优平衡.
DEFAULT_HOLD = 5

def _load_ic(name: str, window: int, hold: int = DEFAULT_HOLD) -> pd.Series:
    """读某因子 ic_curve_<name>_k20.csv 的 ic_h{hold}, 返回最近 window 个样点。"""
    p = os.path.join(IC_DIR, "ic_curve_{}_k20.csv".format(name))
    if not os.path.exists(p):
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(p)
        col = "ic_h{}".format(hold)
        if col not in df.columns:
            return pd.Series(dtype=float)
        s = df[col].dropna().astype(float)
        return s.tail(window)
    except Exception:
        return pd.Series(dtype=float)


def _icir(ic: pd.Series) -> float:
    """ICIR = 均值 / 标准差(具稳定性的方向强度). 单样本或0标准差给0.5中性。"""
    if len(ic) < 2:
        return np.nan
    sd = ic.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return np.nan
    return float(ic.mean() / sd)


# ---------------- 自适应权重 ----------------
# 可靠性门槛与集中度防护常量
MIN_ICIR = 0.3                   # |ICIR|<MIN_ICIR 视为统计上不可靠 -> 压到地板
FLOOR_W = 0.02                   # 不可靠因子的地板权重(占整体)
MAX_ALPHA_SINGLE_SHARE = 0.55    # 单个 alpha 因子在 alpha_share 内占比上限(防一家独大)
BROKEN_IC_MEAN = -0.15           # 短窗 IC 均值低于此阈值 -> 因子方向性失效, 强制抑制


def optimize_weights(window: int = 40, alpha_share: float = 0.28,
                    hold: int = DEFAULT_HOLD):
    """根据最近 window 个 IC 样点, 计算自适应因子权重。

    alpha_share: alpha 分量(vol+mom_rev)合计占权重的比例上限。基础分量
        (signal/trend/govern/liquidity) 按原静态权重相对比例取剩余的 (1-alpha_share)。
    hold: IC 持有期(交易日), 默认 H5 — vol 在 H5 有 IC=0.12, 比 H20 的 0.05 强 2.4 倍。
    返回 dict: {"weights": {...}, "meta": {...IC依据...}}。

    可靠性控制 (修复负ICIR越深权重越高的方向性盲区):
      * |ICIR| < MIN_ICIR 或 IC 样本不足 -> 权重压到 FLOOR_W, 份额让给可靠因子。
      * 单个 alpha 因子在 alpha_share 内占比封顶 MAX_ALPHA_SINGLE_SHARE, 防止
        某段极端 ICIR 让单一因子独揽过半 alpha 权, 造成选股过于押注单一信号。
    """
    base = dict(SCORE_WEIGHTS)

    # 1) alpha 分量 ICIR (长短窗都算, 用于漂移稳定性判定)
    icir = {}
    stability = {}
    for wkey, fname in ALPHA_FACTORS.items():
        ic = _load_ic(fname, window, hold=hold)
        ic_short = _load_ic(fname, min(window, 20), hold=hold)
        arel = _icir(ic)
        ashort = _icir(ic_short)
        icir[wkey] = {
            "n": int(len(ic)),
            "ic_mean": round(float(ic.mean()), 4) if len(ic) else None,
            "icir": round(arel, 3) if arel == arel else None,
            "ic_short": round(float(ic_short.mean()), 4) if len(ic_short) else None,
            "icir_short": round(ashort, 3) if ashort == ashort else None,
            "hold": hold,
        }
        # 符号背离判定: 长/短窗 ICIR 均在读取且异号 -> 近期风格翻转, 不可靠
        if arel == arel and ashort == ashort and arel != 0 and ashort != 0:
            stability[wkey] = (arel * ashort < 0)
        else:
            stability[wkey] = False

    # 2) 计算每个 alpha 因子"初始强度" = |ICIR| (反转义因子负IC为有效, 取绝对值)
    #    不可靠判据: 数据缺失 / |ICIR|<MIN_ICIR(统计不显著) / 短长窗符号背离(漂移翻转)
    alphas = {k: icir[k]["icir"] for k in ALPHA_FACTORS}
    default_share = {
        "vol": base.get("vol", 0.10),
        "mom_rev": base.get("mom_rev", 0.0),
        "pb_rev": base.get("pb_rev", 0.06),
        "roe": base.get("roe", 0.06),
        "mf_net": base.get("mf_net", 0.06),
    }
    raw = {}
    reliable = {}
    for k in ALPHA_FACTORS:
        a = alphas[k]
        if a is None or a != a:            # 无 IC 数据
            raw[k] = 0.0; reliable[k] = False
            continue
        mag = abs(a)
        ok = (mag >= MIN_ICIR) and not stability.get(k, False)
        # 方向性失效检查: 短窗 IC 均值深负 -> 因子方向已反, 强制抑制
        if ok and icir[k].get("ic_short") is not None:
            if icir[k]["ic_short"] < BROKEN_IC_MEAN:
                ok = False
                icir[k]["suppressed_to_floor"] = True
                icir[k]["suppress_reason"] = (
                    f"短窗IC均值 {icir[k]['ic_short']:.3f} < {BROKEN_IC_MEAN}, 方向性失效")
        raw[k] = mag if ok else 0.0
        reliable[k] = ok
        if not ok:
            icir[k]["suppressed_to_floor"] = True
            icir[k]["suppress_reason"] = icir[k].get("suppress_reason") or (
                "short/long ICIR 异号(风格漂移)" if stability.get(k)
                else (f"|ICIR| {mag:.2f} < {MIN_ICIR}" if mag < MIN_ICIR
                      else "ICIR 缺失"))
        else:
            icir[k]["suppressed_to_floor"] = False

    # 3) 用"可靠因子"强度分配 alpha 内部权重; 单因子封顶防集中
    reliable_keys = [k for k in ALPHA_FACTORS if reliable[k]]
    # 全部不可靠时退化为静态比例并整体压到 alpha_share 的 60%
    if not reliable_keys:
        for k in ALPHA_FACTORS:
            raw[k] = default_share[k]
        fallback_total = sum(raw.values()) or 1.0
        cap_share = alpha_share * 0.6
        alpha_w = {k: raw[k] / fallback_total * cap_share for k in ALPHA_FACTORS}
        meta_note = ("全部 alpha 因子不可靠, 按静态比例 60% 持有, 其余让给基础分量")
    else:
        total_raw = sum(raw[k] for k in reliable_keys) or 1.0
        alpha_w = {}
        for k in ALPHA_FACTORS:
            if reliable[k]:
                alpha_w[k] = raw[k] / total_raw * alpha_share
            else:
                alpha_w[k] = FLOOR_W        # 不可靠因子直接给地板
        # 单因子封顶: 超过 alpha_share 的 MAX_ALPHA_SINGLE_SHARE 则削减
        single_cap = alpha_share * MAX_ALPHA_SINGLE_SHARE
        capped = set()
        for k in list(alpha_w):
            if alpha_w[k] > single_cap:
                alpha_w[k] = single_cap
                capped.add(k)
        # 削减出的份额只回填给未触及上限(未被封顶)的可靠因子, 避免回填后重新超限
        surplus = (alpha_share - sum(alpha_w.values()))
        if surplus > 1e-9 and reliable_keys:
            refill_keys = [k for k in reliable_keys if k not in capped]
            rw = {k: alpha_w[k] for k in refill_keys}
            rsum = sum(rw.values())
            if rsum > 1e-9:
                for k in refill_keys:
                    alpha_w[k] += surplus * (alpha_w[k] / rsum)
        meta_note = ("alpha 分量由 ICIR 强度自适应(含可靠性门槛/封顶/漂移防护), "
                     "基础分量保持相对比例")

    # 4) 基础分量按静态相对比例占据剩余 (1-alpha_share)
    base_share = 1.0 - alpha_share
    base_keys = [k for k in ALL_KEYS if k not in ALPHA_FACTORS]
    base_total = sum(base.get(k, 0.0) for k in base_keys) or 1.0
    base_w = {k: base.get(k, 0.0) / base_total * base_share for k in base_keys}

    # 5) 合并并归一化(浮点兜底)
    weights = dict(base_w)
    weights.update(alpha_w)
    wsum = sum(weights.values()) or 1.0
    weights = {k: round(v / wsum, 4) for k, v in weights.items()}

    meta = {
        "window": window,
        "alpha_share": alpha_share,
        "min_icir": MIN_ICIR,
        "floor_w": FLOOR_W,
        "max_alpha_single_share": MAX_ALPHA_SINGLE_SHARE,
        "icir": icir,
        "static_base": {k: base.get(k) for k in ALL_KEYS},
        "generated": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": meta_note,
    }
    return {"weights": weights, "meta": meta}


def load_weights():
    """selector 用: 返回当前生效权重 dict。若无 weights.json 则返回 config.SCORE_WEIGHTS。"""
    if os.path.exists(WEIGHTS_FILE):
        try:
            with open(WEIGHTS_FILE, encoding="utf-8") as f:
                return json.load(f).get("weights", dict(SCORE_WEIGHTS))
        except Exception:
            pass
    return dict(SCORE_WEIGHTS)


def _print_report(opt: dict):
    w = opt["weights"]
    m = opt["meta"]
    print("=" * 56)
    print("因子权重自适应 (ICIR 驱动)  窗口={}  alpha占{:>4.0%}".format(
        m["window"], m.get("alpha_share")))
    print("-" * 56)
    for k in ALL_KEYS:
        stat = m["icir"].get(k) if k in m["icir"] else None
        if stat and stat.get("n"):
            print("  alpha {:8s} IC均值{:+.4f} ICIR{:+.3f}  -> 权重 {:5.3f}".format(
                k, stat["ic_mean"] or 0.0, stat["icir"] or 0.0, w.get(k, 0.0)))
        elif k in ALPHA_FACTORS:
            print("  alpha {:8s} (无IC数据, 待ic_backtest回算)  权重 {:5.3f}".format(
                k, w.get(k, 0.0)))
        else:
            print("  base  {:8s}                                  权重 {:5.3f}".format(
                k, w.get(k, 0.0)))
    print("-" * 56)
    print("静态 SCORE_WEIGHTS: {}".format(m["static_base"]))
    print("叠加后权重和: {:.4f}".format(sum(w.values())))


def main():
    ap = argparse.ArgumentParser(description="因子权重自适应(D->B反馈闭环)")
    ap.add_argument("--preview", action="store_true", help="仅预览建议权重, 不写入")
    ap.add_argument("--write", action="store_true", help="写入 data/weights.json")
    ap.add_argument("--reset", action="store_true", help="删除 weights.json, 回退静态权重")
    ap.add_argument("--window", type=int, default=40, help="用于统计的最近IC样点数")
    ap.add_argument("--alpha-share", type=float, default=0.28,
                    help="alpha分量合计权重占比上限")
    ap.add_argument("--hold", type=int, default=DEFAULT_HOLD,
                    help="IC持有期(交易日), 默认H5. vol在H5 IC=0.12 vs H20 IC=0.05")
    a = ap.parse_args()

    if a.reset:
        if os.path.exists(WEIGHTS_FILE):
            os.remove(WEIGHTS_FILE)
            print("已删除 {}, 回退静态权重".format(WEIGHTS_FILE))
        else:
            print("无权重文件, 无需 reset")
        return 0

    opt = optimize_weights(window=a.window, alpha_share=a.alpha_share, hold=a.hold)
    _print_report(opt)

    if a.write or not a.preview:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(opt, f, ensure_ascii=False, indent=2)
        print("\n已写: {}".format(WEIGHTS_FILE))
    else:
        print("\n[preview] 未写入 (加 --write 生效)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())

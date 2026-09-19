# -*- coding: utf-8 -*-
"""DRL-2 真实训练验证 —— 跑一次**真正的 PPO 训练**, 证明"学习中"指标真的被采集到.

为什么需要它（而不是只跑单测）
  单测用**构造的字典**喂 `drl_metrics`, 证明的是"给定数值能正确汇总";
  它**不能**证明 `METRIC_LOGGER_KEYS` 映射的 SB3 logger 键名与真实输出一致 ——
  若键名写错, 单测全绿而生产 `train_meta` 里依然**一个指标都没有**,
  即又回到 DRL-2 的老问题「结构性不可验证」。
  故本脚本跑一次真训练, 断言序列**非空**。

验证项
  ① entropy_loss / policy_loss / value_loss / grad_norm 序列**非空**（n>=1）
  ② actual_timesteps>0 且与 requested_timesteps 分开记录
  ③ duration_s>0（墙钟时长真的在测）
  ④ 汇总与序列 JSON 安全（无 NaN/Inf）
  ⑤ threshold_applied=False（METHOD-1: 本批次不判定收敛）
  ⑥ 输出可直接挂 `train_meta["train_metrics"]`

用法: python scripts/preflight_drl_metrics_realrun.py [--timesteps 200]
退出码: 0 = 全部断言通过; 1 = 有断言失败
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np  # noqa: E402

import drl_metrics  # noqa: E402
from drl_train import CVaR_PPO, FactorWeightEnv, SCORE_FACTORS, _compute_regime_features  # noqa: E402

_RESULTS: "list[tuple[bool, str]]" = []


def _check(ok: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _json_safe(obj) -> "str | None":
    """返回第一处非法 JSON 问题的描述, 全合法则 None。"""
    if isinstance(obj, float):
        return None if math.isfinite(obj) else f"非有限值 {obj}"
    if isinstance(obj, dict):
        for k, v in obj.items():
            bad = _json_safe(v)
            if bad:
                return f"{k}: {bad}"
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            bad = _json_safe(v)
            if bad:
                return f"[{i}]: {bad}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timesteps", type=int, default=200)
    ap.add_argument("--n-steps", type=int, default=32)
    ap.add_argument("--n-days", type=int, default=90,
                    help="IC 序列长度（生产 _load_factor_state 约 40 上下, 视 60 自然日内的交易日数）")
    args = ap.parse_args()

    print("=" * 78)
    print("DRL-2 学习中断指标 —— 真实训练验证（跑一次真 PPO）")
    print("=" * 78)

    rng = np.random.RandomState(20260920)
    n_days = args.n_days
    n_factors = len(SCORE_FACTORS)
    rets = rng.randn(n_days) * 0.012
    ic = rng.randn(n_days, n_factors) * 0.05
    base_w = np.ones(n_factors) / n_factors

    regime_features = _compute_regime_features(rets)
    env = FactorWeightEnv(ic, base_w, day=dt.date(2026, 9, 5), brief={},
                          tuning=None, regime_features=regime_features)
    print(f"因子          : {list(SCORE_FACTORS)}")
    print(f"环境          : n_days={n_days} obs_dim={env.observation_space.shape[0]}")
    print(f"目标步数      : {args.timesteps}  (n_steps={args.n_steps})")

    model = CVaR_PPO("MlpPolicy", env, n_steps=args.n_steps, learning_rate=3e-4,
                     n_epochs=2, verbose=0, cvar_alpha=0.05, cvar_coef=0.1)
    t0 = time.time()
    model.learn(total_timesteps=args.timesteps)
    duration_s = time.time() - t0
    print(f"训练完成      : {duration_s:.2f}s, model.num_timesteps={model.num_timesteps}")

    h = model.metric_history
    print(f"\n采集到的迭代数: n_records={h.n_records}")
    print("各指标点数    : " + ", ".join(
        f"{k}={len(v)}" for k, v in sorted(h.history.items()) if v) or "(空)")
    if h.skipped:
        print("跳过计数(前5) : " + ", ".join(
            f"{k}={v}" for k, v in sorted(h.skipped.items(), key=lambda kv: -kv[1])[:5]))

    rec = drl_metrics.summarize_run(model=model, history=h, duration_s=duration_s,
                                    requested_timesteps=args.timesteps)
    print("\n--- train_meta[\"train_metrics\"] 汇总 ---")
    for k in sorted(rec["summary"]):
        d = rec["summary"][k]
        print(f"  {k:<20} n={d['n']:<4} first={d['first']:<12} last={d['last']:<12} "
              f"slope={d['slope']}")

    print("\n--- 断言 ---")
    summ = rec["summary"]
    for key in ("entropy_loss", "policy_loss", "value_loss", "grad_norm"):
        _check(key in summ and summ[key]["n"] >= 1,
               f"{key} 序列非空（证明 logger 键名映射与真实 SB3 输出一致）",
               f"n={summ.get(key, {}).get('n', 0)}")
    _check(summ.get("grad_norm", {}).get("n", 0) >= 1,
           "grad_norm 由 clip_grad_norm_ 采集（不在 SB3 logger 中）")
    _check(h.n_records >= 1, "至少记录到 1 次迭代", f"n_records={h.n_records}")
    _check(rec["actual_timesteps"] and rec["actual_timesteps"] > 0,
           "actual_timesteps 为真实执行步数", f"{rec['actual_timesteps']}")
    _check(rec["requested_timesteps"] == args.timesteps, "requested 与 actual 分开记录",
           f"req={rec['requested_timesteps']} act={rec['actual_timesteps']}")
    _check((rec["duration_s"] or 0) > 0, "训练墙钟时长被测量", f"{rec['duration_s']}s")
    _check(rec["threshold_applied"] is False, "本批次不施加'收敛'阈值（METHOD-1）")
    bad = _json_safe(rec)
    _check(bad is None, "汇总与序列 JSON 安全（无 NaN/Inf）", bad or "")
    txt = json.dumps(rec, ensure_ascii=False)
    _check("NaN" not in txt and "Infinity" not in txt, "json.dumps 文本中无 NaN/Infinity")
    _check(len(txt) < 200000, "train_meta 体积可控", f"{len(txt)} 字节")

    fails = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结论: {'全部通过' if not fails else '有失败项'}  "
          f"({len(_RESULTS) - len(fails)}/{len(_RESULTS)} PASS)")
    for f in fails:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

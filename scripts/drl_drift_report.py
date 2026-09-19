# -*- coding: utf-8 -*-
"""DRL 权重漂移分布报告（阈值标定的数据准备）.

为什么需要它
  用户 2026-09-19 明确提醒：漂移阈值**需要数据积累才能标定**，
  「不要在只有单日数据时就定死阈值 —— 那会重复『用单日抽样得出错误结论』的问题」。
  因此先把**已落盘**的 train_meta（每天一份，含 base/prior/final_weights）回溯算出分布，
  作为标定的起点；同时把这些记录以 `source="backfill"` 追加进漂移账本，
  与后续真实运行产生的记录**可区分**但不割裂。

两种漂移（含义不同，都要看）
  · base_to_final : 单次训练把权重从 base 推离多远（用户给的检查式）
  · day_over_day  : 与上一个有 final_weights 的交易日比 —— 这才是"日间漂移"，
                    也是最终要设阈值的那个量

用法
  & <py310> scripts\\drl_drift_report.py            # 打印分布并追加账本
  & <py310> scripts\\drl_drift_report.py --no-write # 只看分布, 不写账本
输出
  data/drl_weight_drift_report.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics as st
import sys
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

from config import DATA_DIR  # noqa: E402
import drl_drift as D  # noqa: E402  (轻量模块, 无需 torch)

OUT = os.path.join(DATA_DIR, "drl_weight_drift_report.json")


def _load_all():
    """返回 [{day, base, prior, final}] ，按 day 升序。"""
    rows = []
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, "drl", "*", "train_meta.json"))):
        day = os.path.basename(os.path.dirname(fp))
        if not (day.isdigit() and len(day) == 8):
            continue
        try:
            m = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        fin = m.get("final_weights")
        if not isinstance(fin, dict) or not fin:
            continue
        rows.append({"day": day, "base": m.get("base_weights") or {},
                     "prior": m.get("prior_weights") or {}, "final": fin,
                     "ok": m.get("ok")})
    return sorted(rows, key=lambda r: r["day"])


def _dist(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "min": round(min(vals), 6),
            "median": round(st.median(vals), 6), "max": round(max(vals), 6),
            "mean": round(st.mean(vals), 6)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="只打印, 不追加账本")
    ap.add_argument("--threshold", type=float, default=D.DRIFT_THRESHOLD_DEFAULT)
    args = ap.parse_args()

    rows = _load_all()
    print(f"[drift] 扫描到 {len(rows)} 个含 final_weights 的 train_meta")
    if not rows:
        print("[drift] 无数据, 退出")
        return 1

    recs = []
    prev_final = None
    prev_day = None
    for r in rows:
        d_bf, per_bf = D.max_abs_weight_diff(r["base"], r["final"])
        d_dod = None
        if prev_final is not None:
            d_dod, _ = D.max_abs_weight_diff(prev_final, r["final"])
        recs.append({
            "day": r["day"], "ok": r.get("ok"),
            "drift_base_to_final": round(d_bf, 6),
            "per_factor_base_to_final": {k: round(v, 6) for k, v in per_bf.items()},
            "prev_day": prev_day,
            "drift_day_over_day": None if d_dod is None else round(d_dod, 6),
            # 保留原始权重: 供**因子权重边界标定**使用。
            # (第一版漏了它, 导致边界分析取不到数据 —— 后补)
            "final_weights": {k: round(float(v), 6) for k, v in r["final"].items()},
        })
        prev_final, prev_day = r["final"], r["day"]

    b2f = [x["drift_base_to_final"] for x in recs]
    dod = [x["drift_day_over_day"] for x in recs if x["drift_day_over_day"] is not None]

    print("\n=== base -> final 漂移分布（单次训练推离基准多远）===")
    print("   ", _dist(b2f))
    print(f"    超阈值 {args.threshold} 的天数: "
          f"{sum(1 for v in b2f if v > args.threshold)}/{len(b2f)}")

    print("\n=== day-over-day 漂移分布（真正的『日间漂移』, 最终要设阈值的量）===")
    print("   ", _dist(dod))
    if dod:
        print(f"    超阈值 {args.threshold} 的天数: "
              f"{sum(1 for v in dod if v > args.threshold)}/{len(dod)}")

    print("\n=== 逐日 ===")
    print(f"    {'day':10s} {'base->final':>12s} {'day-over-day':>13s}  最大变化因子")
    for x in recs:
        top = max(x["per_factor_base_to_final"], key=x["per_factor_base_to_final"].get)
        dod_s = "·" if x["drift_day_over_day"] is None else f"{x['drift_day_over_day']:.4f}"
        print(f"    {x['day']:10s} {x['drift_base_to_final']:12.4f} {dod_s:>13s}  "
              f"{top} {x['per_factor_base_to_final'][top]:.4f}")

    # ---- 极端权重**记录**（DRL-5 最小版本）：只记录, 绝不截断 ----
    # 用户 2026-09-19 决策: 本批次不部署边界, 只积累"极端值发生频率 + 下游表现"数据。
    # 这里对**记录门限**与**对照门限** [0.02,0.40] 各算一遍, 让"若当初部署会误伤多少"可对比。
    lo, hi = D.extreme_bounds()
    print(f"\n=== 极端权重记录（门限 [{lo}, {hi}]，**只记录不截断**）===")
    ext_days = 0
    ext_hits = 0
    ext_detail = []
    for r in rows:
        w = r["final"]
        hits = {k: float(v) for k, v in w.items() if float(v) < lo or float(v) > hi}
        if hits:
            ext_days += 1
            ext_hits += len(hits)
            ext_detail.append({"day": r["day"], "hits": hits})
            print(f"    {r['day']}  越界 {len(hits)} 个: "
                  + ", ".join(f"{k}={v:.4f}" for k, v in hits.items()))
    if not ext_days:
        print(f"    0 天越界 / {len(rows)} 天 —— 该门限当前**零触发**")
        print("    ⇒ 这正是用户判定『本批次不部署该边界(死代码, 零收益)』的实测依据")
    print(f"    合计: {ext_days}/{len(rows)} 天有越界, 共 {ext_hits} 处")

    # 对照: 若部署 [0.02, 0.40] 会误伤多少
    print("\n=== 对照: 若部署 [0.02, 0.40] 会触发多少（用户判定为『常态性改变行为』）===")
    c_days = c_hits = 0
    for r in rows:
        hits = {k: float(v) for k, v in r["final"].items() if float(v) < 0.02 or float(v) > 0.40}
        if hits:
            c_days += 1
            c_hits += len(hits)
    print(f"    {c_days}/{len(rows)} 天触发, 共 {c_hits} 处 —— 且**无证据表明这些极端值有害**")

    # 各因子的 final_weights 取值范围（判断"边界"该设在哪）
    print("\n=== 各因子 final_weights 取值范围（因子权重边界的标定依据）===")
    factors = sorted({k for x in rows for k in x["final"]})
    for fac in factors:
        vs = [float(x["final"][fac]) for x in rows if fac in x["final"]]
        print(f"    {fac:12s} min={min(vs):.4f}  max={max(vs):.4f}  "
              f"median={st.median(vs):.4f}")

    rep = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_days": len(recs),
        "threshold_used": args.threshold,
        "threshold_is_calibrated": False,
        "note": ("阈值需数据积累后标定(用户 2026-09-19 提醒)。本报告基于**已落盘的历史 "
                 "train_meta** 回溯计算, 不是真实运行的逐日账本 —— 后者见 "
                 "data/drl_weight_drift.jsonl (source=run)"),
        "dist_base_to_final": _dist(b2f),
        "dist_day_over_day": _dist(dod),
        "per_factor_final_range": {
            fac: {"min": min(float(x["final"][fac]) for x in rows if fac in x["final"]),
                  "max": max(float(x["final"][fac]) for x in rows if fac in x["final"]),
                  "median": st.median([float(x["final"][fac]) for x in rows if fac in x["final"]])}
            for fac in factors},
        "records": recs,
        "extreme_record_bounds": [lo, hi],
        "extreme_days": ext_days,
        "extreme_hits": ext_hits,
        "extreme_detail": ext_detail,
        "counterfactual_002_040_days": c_days,
        "counterfactual_002_040_hits": c_hits,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[drift] 报告 -> {OUT}")

    if not args.no_write:
        n = 0
        try:
            os.makedirs(os.path.dirname(D.ledger_path()), exist_ok=True)
            with open(D.ledger_path(), "a", encoding="utf-8") as f:
                for x in recs:
                    f.write(json.dumps({**x, "source": "backfill",
                                        "threshold": args.threshold,
                                        "action": "ok"}, ensure_ascii=False) + "\n")
                    n += 1
        except Exception as e:  # noqa: BLE001
            print(f"[drift] 账本写入失败: {e}")
        print(f"[drift] 已追加 {n} 条到账本(source=backfill): {D.ledger_path()}")
        # 极端权重也回填(**只记录, 不截断**) —— 立即积累"发生频率"数据
        m2 = 0
        for r in rows:
            D.check_extreme_weights(r["final"], day=r["day"], source="backfill")
            m2 += 1
        print(f"[drift] 已回填 {m2} 条极端权重记录(source=backfill): "
              f"{D.extreme_ledger_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

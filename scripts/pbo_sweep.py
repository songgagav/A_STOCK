# -*- coding: utf-8 -*-
"""PBO 参数扫描 + CSCV 估计 (pbo_sweep.py) — 2026-09-13.

产出真实的 PBO(回测过拟合概率), 供 src/overfitting_test.py 的 [PBO] 检查读取。

为什么需要扫描
--------------
CSCV 需要"配置 × 时间"的收益矩阵。项目里每个非重叠窗口只覆盖自己那段时间
(curve.json 各 120 天、互不重叠), 因此时间轴用 12 个窗口、配置轴通过在同一窗口
上跑多组参数得到。这样每列(配置)都在同一批时间块上被评估, 符合 CSCV 前提。

配置轴 (默认 18 组)
    top_n       ∈ {5, 8, 10, 12, 15, 20}   —— 持仓只数
    weight_mode ∈ {equal, signal, rank}    —— 权重构造方式
    lookback_days 固定 120(它同时决定回测区间长度, 变动会破坏各配置的时间可比性)

成本 (2026-09-13 实测)
    单窗口 PIT 选股 ≈ 151s (与 top_n/权重无关, 按 day 落盘缓存, 每个窗口只算一次)
    单次纯仿真 ≈ 3~7s
    12 窗口 × 18 配置 => 选股 ~30min(一次性) + 仿真 ~15min ≈ 45min
    已算过的 (窗口,配置) 会跳过, 可断点续跑。

用法
    python scripts/pbo_sweep.py                 # 全量 12 窗口 × 18 配置
    python scripts/pbo_sweep.py --limit-windows 2   # 先小样本试跑
    python scripts/pbo_sweep.py --report-only       # 只重算 CSCV(不跑回测)

输出
    data/pbo/curves/<day>__<tag>.json     每个 (窗口,配置) 的日净值曲线
    data/pbo/pbo_result.json              CSCV 结果(含 PBO/λ 分布/IS-OOS 斜率)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from pbo_cscv import cscv_pbo  # noqa: E402
from vnpy_backtest import _out_dir, run_vnpy_backtest  # noqa: E402

PBO_DIR = os.path.join(_BASE, "data", "pbo")
CURVE_DIR = os.path.join(PBO_DIR, "curves")
RESULT = os.path.join(PBO_DIR, "pbo_result.json")

DEFAULT_TOPN = [5, 8, 10, 12, 15, 20]
DEFAULT_WEIGHTS = ["equal", "signal", "rank"]
LOOKBACK = 120


def _default_windows() -> list[str]:
    """复用 nonoverlap_rerun 的 12 个非重叠窗口集, 保证与 OOS 口径一致."""
    sys.path.insert(0, os.path.join(_BASE, "scripts"))
    try:
        from nonoverlap_rerun import DEFAULT_ENDS
        return list(DEFAULT_ENDS)
    except Exception:  # noqa: BLE001
        return []


def _tag(top_n: int, weight_mode: str) -> str:
    return f"pbo_tn{top_n}_{weight_mode}"


def _returns_from_curve(curve: list) -> pd.Series:
    """净值序列 -> 日收益(去掉首个 NaN)."""
    bal = [c.get("balance") for c in curve]
    idx = [str(c.get("date"))[:10] for c in curve]
    s = pd.Series(pd.to_numeric(pd.Series(bal), errors="coerce").values, index=idx)
    s = s[s.notna()]
    if len(s) < 3:
        return pd.Series(dtype=float)
    r = s.pct_change().dropna()
    r = r[np.isfinite(r)]
    return r


def run_one(day: str, top_n: int, weight_mode: str,
            sel_n: int) -> tuple[pd.Series | None, str]:
    """跑(或复用)一个 (窗口, 配置), 返回 (日收益, 状态)."""
    os.makedirs(CURVE_DIR, exist_ok=True)
    tag = _tag(top_n, weight_mode)
    cache_fp = os.path.join(CURVE_DIR, f"{day}__{tag}.json")
    if os.path.exists(cache_fp):
        try:
            with open(cache_fp, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("ok") and d.get("returns"):
                s = pd.Series(d["returns"], index=d["dates"], dtype=float)
                return s, "cached"
        except Exception:  # noqa: BLE001
            pass

    res = run_vnpy_backtest(day, top_n=top_n, lookback_days=LOOKBACK,
                            sel_n=sel_n, out_tag=tag, persist_arctic=False,
                            weight_mode=weight_mode)
    if not res.get("ok"):
        return None, f"FAIL: {res.get('error')}"
    cpath = os.path.join(_out_dir(day.replace("-", ""), tag), "curve.json")
    if not os.path.exists(cpath):
        return None, "FAIL: 无 curve.json"
    with open(cpath, encoding="utf-8") as f:
        curve = json.load(f)
    s = _returns_from_curve(curve)
    if s.empty:
        return None, "FAIL: 曲线不足"
    with open(cache_fp, "w", encoding="utf-8") as f:
        json.dump({"day": day, "top_n": top_n, "weight_mode": weight_mode,
                   "lookback_days": LOOKBACK, "ok": True,
                   "dates": [str(x) for x in s.index],
                   "returns": [float(x) for x in s.values]},
                  f, ensure_ascii=False, indent=2)
    return s, "run"


def build_blocks(windows: list[str], topn: list[int], wms: list[str]) -> tuple[list, list, dict]:
    """跑扫描并对齐, 返回 (blocks, 可用配置列表, 诊断)."""
    sel_n = max(topn)
    series: dict[tuple, dict] = {}
    diag = {"skipped_config": [], "per_window_days": {}}
    grid = [(tn, wm) for tn in topn for wm in wms]

    t_all = time.time()
    for wi, day in enumerate(windows, 1):
        t0 = time.time()
        print(f"\n{'=' * 62}\n[{wi}/{len(windows)}] 窗口 {day}\n{'=' * 62}", flush=True)
        for k, (tn, wm) in enumerate(grid, 1):
            t1 = time.time()
            s, st = run_one(day, tn, wm, sel_n)
            series.setdefault((tn, wm), {})[day] = s
            el = time.time() - t1
            print(f"  ({k}/{len(grid)}) top{tn:<3}{wm:<7} {st:<6} {el:6.1f}s", flush=True)
        print(f"  窗口完成, 用时 {time.time() - t0:.1f}s", flush=True)
    print(f"\n扫描总用时 {(time.time() - t_all) / 60:.1f} 分钟", flush=True)

    # 只保留"所有窗口都成功"的配置, 保证矩阵是矩形
    ok_cfgs = []
    for cfg, per_win in series.items():
        if not cfg:
            continue
        if all(per_win.get(d) is not None and len(per_win[d]) >= 3 for d in windows):
            ok_cfgs.append(cfg)
    ok_cfgs.sort()
    dropped = [f"top{tn}_{wm}" for (tn, wm) in grid if (tn, wm) not in ok_cfgs]
    diag["skipped_config"] = dropped

    blocks = []
    for day in windows:
        per_cfg = {cfg: series[cfg][day] for cfg in ok_cfgs}
        common = None
        for s in per_cfg.values():
            common = s.index if common is None else common.intersection(s.index)
        common = list(common)
        if len(common) < 10:
            print(f"  [warn] 窗口 {day} 公共交易日仅 {len(common)} 天, 跳过")
            continue
        m = np.column_stack([per_cfg[cfg].reindex(common).to_numpy() for cfg in ok_cfgs])
        blocks.append(m)
        diag["per_window_days"][day] = len(common)
    return blocks, [f"top{tn}_{wm}" for tn, wm in ok_cfgs], diag


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", nargs="*", default=None)
    ap.add_argument("--topn", type=str, default=",".join(map(str, DEFAULT_TOPN)))
    ap.add_argument("--weights", type=str, default=",".join(DEFAULT_WEIGHTS))
    ap.add_argument("--limit-windows", type=int, default=0)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    windows = args.windows or _default_windows()
    if not windows:
        print("ERROR: 无法取得窗口集(检查 scripts/nonoverlap_rerun.py)")
        sys.exit(1)
    if args.limit_windows:
        windows = windows[:args.limit_windows]
    topn = [int(x) for x in args.topn.split(",") if x.strip()]
    wms = [x.strip() for x in args.weights.split(",") if x.strip()]
    print(f"窗口 {len(windows)} 个: {windows}")
    print(f"配置 {len(topn) * len(wms)} 组: top_n={topn} × weight={wms}, lookback={LOOKBACK}")

    os.makedirs(PBO_DIR, exist_ok=True)
    if args.report_only:
        blocks, diag = [], {}
        need = [(tn, wm) for tn in topn for wm in wms]
        cfgs = [f"top{tn}_{wm}" for tn, wm in need]
        for day in windows:
            rows = []
            for tn, wm in need:
                fp = os.path.join(CURVE_DIR, f"{day}__{_tag(tn, wm)}.json")
                if not os.path.exists(fp):
                    continue
                try:
                    with open(fp, encoding="utf-8") as f:
                        d = json.load(f)
                    rows.append(pd.Series(d["returns"], index=d["dates"], dtype=float))
                except Exception:  # noqa: BLE001
                    continue
            if len(rows) != len(need):
                print(f"  [skip] 窗口 {day} 配置不全 ({len(rows)}/{len(need)})")
                continue
            common = None
            for s in rows:
                common = s.index if common is None else common.intersection(s.index)
            common = list(common)
            if len(common) < 10:
                continue
            blocks.append(np.column_stack([s.reindex(common).to_numpy() for s in rows]))
            diag.setdefault("per_window_days", {})[day] = len(common)
    else:
        blocks, cfgs, diag = build_blocks(windows, topn, wms)

    if len(blocks) < 4:
        print(f"ERROR: 可用块数 {len(blocks)} < 4, 无法做 CSCV(需 >=4 且为偶数)")
        sys.exit(2)

    n_blocks = len(blocks) if len(blocks) % 2 == 0 else len(blocks) - 1
    res = cscv_pbo(blocks[:n_blocks])
    out = {
        "run_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "windows": windows,
        "blocks_used": n_blocks,
        "configs": cfgs,
        "lookback_days": LOOKBACK,
        "diag": diag,
        **res,
    }
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 62}\n  CSCV-PBO 结果\n{'=' * 62}")
    print(f"  时间块 S={res['n_blocks']}  配置 N={res['n_configs']}  组合数={res['n_combos']}")
    print(f"  PBO = {res['pbo']:.4f}  ({res['pbo']*100:.2f}%)   [越低越好, >50% 视为过拟合]")
    print(f"  λ 分布: mean={res['lambda_mean']:.3f} std={res['lambda_std']:.3f} "
          f"p05={res['lambda_p05']:.3f} p50={res['lambda_p50']:.3f} p95={res['lambda_p95']:.3f}")
    print(f"  归一化 OOS 排名均值 w̄ = {res['omega_mean']:.3f} (0.5 为无筛选力)")
    print(f"  IS 最优配置 OOS 亏损概率 = {res['prob_oos_loss']:.3f}")
    print(f"  IS 最优: IS 均值 Sharpe={res['is_best_mean_is']:.3f} -> "
          f"OOS 均值 Sharpe={res['is_best_mean_oos']:.3f}")
    print(f"  OOS~IS 回归斜率 = {res['is_oos_slope']:.3f} (过拟合时趋 0/负)")
    print(f"\n  结果已保存: {RESULT}")


if __name__ == "__main__":
    main()

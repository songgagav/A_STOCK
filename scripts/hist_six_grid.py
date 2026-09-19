# -*- coding: utf-8 -*-
"""2010-2017 历史窗口的多口径复算 (hist_six_grid.py).

背景 (docs/pit-valuation.md §⑨/§⑪)
  §⑨ 裁定: 2010-2017 历史窗口**仅用于跨环境稳健性验证**, 不用于凑 n>=20 的显著性计数。
  方向一致性检查已把"环境外"窗口单独标记。本脚本在**同一协议**下 (与 2018+ 的
  `recompute_six_grid.py` 完全同口径) 为历史窗口派生 生产排序 / 截尾3% / 5% / 8% / 纯融合
  五格, 供"按市场状态分层报告"使用。

与 recompute_six_grid.py 的差异 (刻意隔离, 避免覆盖 2018+ 基线)
  - 输出后缀统一加 `h17` 前缀: `OOS_OUT_SUFFIX=h17p / h17t05fix / ...`
    ⇒ 产物为 data/vnpy_backtest_nonoverlap_fwd_results_h17p.json 等, 不动 `_p/_t05fix` 基线。
  - 矩阵落 data/hist_six_grid.json (不动 data/six_grid_recompute.json)。
  - 口径参数从 recompute_six_grid.MODES 直接复用, 保证"同一协议"。

用法
    python scripts/hist_six_grid.py                      # 探口径(秒级): 快照派生 top10 是否清晰
    python scripts/hist_six_grid.py --write [--only t05] # 预置缓存 + 逐口径影子回测
    python scripts/hist_six_grid.py --days-from data/vnpy_backtest_nonoverlap_fwd_results_hist1017.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import subprocess
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

import pool_snapshot as ps  # noqa: E402
from recompute_six_grid import MODES as BASE_MODES  # noqa: E402
from vnpy_backtest import _pit_cache_path  # noqa: E402

DEF_RESULTS = os.path.join(_BASE, "data",
                           "vnpy_backtest_nonoverlap_fwd_results_hist1017.json")
OUT = os.path.join(_BASE, "data", "hist_six_grid.json")

SUF = {"prod": "h17p", "t03": "h17t03fix", "t05": "h17t05fix",
       "t08": "h17t08fix", "rf1": "h17rf1fix"}

#: 口径 -> 配置(后缀换成 h17 前缀)
MODES = {}
for _m, _c in BASE_MODES.items():
    _cc = dict(_c)
    _cc["suffix"] = SUF[_m]
    _cc["env"] = dict(_c["env"])
    _cc["env"]["OOS_OUT_SUFFIX"] = SUF[_m]
    MODES[_m] = _cc


def _load_days(results: str) -> list[str]:
    if not os.path.exists(results):
        print(f"[fatal] 无结果文件: {results}")
        sys.exit(2)
    data = json.load(open(results, encoding="utf-8"))
    days = sorted(x["day"] for x in data if x.get("ok"))
    print(f"读取 {len(days)} 个历史窗口: {days}")
    return days


def _snap_top(day: str, mode: str, n: int = 10) -> list[dict]:
    df = ps.load(day, n)
    if df is None:
        return []
    cfg = MODES[mode]
    return ps.top_from_snapshot(df, n=n, rank_by=cfg["rank_by"],
                                alpha=cfg["alpha"], trim_q=cfg["trim_q"])


def probe(days: list[str], modes: list[str], n: int = 10) -> None:
    print("\n=== 探口径(从 pool_snapshot 派生 top-10) ===")
    for mode in modes:
        missing = [d for d in days if not _snap_top(d, mode, n)]
        print(f"[{MODES[mode]['label']:<6}] 快照命中 {len(days) - len(missing)}/{len(days)}"
              f"{'  缺: ' + str(missing) if missing else ''}")
    # 生产口径自检: 快照派生 top-10 必须与**实时基线缓存**逐位一致, 否则预置缓存会
    # 用派生值静默覆盖真实选股。历史窗口无既有基线可比, 但有本轮现场跑出的缓存。
    from vnpy_backtest import _pit_cache_path as _pcp
    bad = []
    checked = 0
    for d in days:
        fp = _pcp(d, n)          # 默认 env = 生产口径
        if not os.path.exists(fp):
            continue
        try:
            live = [t.get("canon") for t in
                    (json.load(open(fp, encoding="utf-8")).get("targets") or [])][:n]
        except Exception:  # noqa: BLE001
            continue
        snap = [t["canon"] for t in _snap_top(d, "prod", n)]
        if not live or not snap:
            continue
        checked += 1
        if live != snap:
            bad.append(d)
    print(f"[自检] 生产口径 快照派生==实时缓存 {checked - len(bad)}/{checked}"
          + (f"  **不一致: {bad}**" if bad else "  一致"))
    if bad:
        print("       -> 不一致时不应预置缓存(会覆盖真实选股); 请先排查口径漂移。")
    print("注: 历史窗口无既有实时基线可比, 生产口径的忠实性由 2018+ 的 recompute_six_grid"
          " 探口径结论背书(同版本同代码路径)。")


def _seed_then_run(days: list[str], mode: str, n: int) -> None:
    cfg = MODES[mode]
    saved = {k: os.environ.get(k) for k in cfg["env"]}
    os.environ.update(cfg["env"])
    try:
        seeded = 0
        for day in days:
            top = _snap_top(day, mode, n)
            if not top:
                print(f"[warn] {mode} {day} 无快照, 无法预置")
                continue
            fp = _pit_cache_path(day, n)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump({"day": day, "n": n, "targets": top,
                           "generated_at": "hist_six_grid",
                           "source": "pool_snapshot"}, f, ensure_ascii=False, indent=2)
            seeded += 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(f"[{cfg['label']}] 预置 {seeded}/{len(days)} 窗口选股缓存 -> 跳过选股, 纯仿真")
    e = dict(os.environ)
    e.update(cfg["env"])
    print(f"[{cfg['label']}] 运行: nonoverlap_rerun.py --fresh --ends <{len(days)} 个历史日>"
          f"  (OOS_OUT_SUFFIX='{cfg['suffix']}')")
    subprocess.run([sys.executable, "scripts/nonoverlap_rerun.py", "--fresh",
                    "--ends", *days], env=e, check=False)


def _stat(data: list, field: str, days: set):
    vals = [float((x.get("stats") or {}).get(field, 0.0))
            for x in data if x.get("ok") and x.get("day") in days]
    if not vals:
        return None, []
    return st.mean(vals), vals


def write_matrix(days: list[str], modes: list[str], n: int = 10) -> None:
    for mode in modes:
        _seed_then_run(days, mode, n)
    set_days = set(days)
    rows = {}
    for mode in modes:
        cfg = MODES[mode]
        out = os.path.join(_BASE, "data",
                           f"vnpy_backtest_nonoverlap_fwd_results_{cfg['suffix']}.json")
        if not os.path.exists(out):
            print(f"[warn] 缺产物: {out}")
            continue
        data = json.load(open(out, encoding="utf-8"))
        mr, rets = _stat(data, "total_return", set_days)
        ms, _ = _stat(data, "sharpe_ratio", set_days)
        mm, _ = _stat(data, "max_ddpercent", set_days)
        n_ok = len([x for x in data if x.get("ok") and x.get("day") in set_days])
        rows[mode] = dict(label=cfg["label"], n=n_ok,
                          mean_ret=round(mr, 4) if mr is not None else None,
                          median_ret=round(st.median(rets), 4) if rets else None,
                          pos_windows=sum(1 for r in rets if r > 0),
                          mean_sharpe=round(ms, 4) if ms is not None else None,
                          mean_mdd=round(mm, 4) if mm is not None else None)
        print(f"[{cfg['label']:<6}] 历史: mean={rows[mode]['mean_ret']}% "
              f"中位={rows[mode]['median_ret']}% 正窗口={rows[mode]['pos_windows']}/"
              f"{n_ok} Sharpe={rows[mode]['mean_sharpe']} MDD={rows[mode]['mean_mdd']}%")
    if rows.get("prod", {}).get("n"):
        pb = rows["prod"]["mean_ret"]
        for mode in modes:
            if mode == "prod" or not rows.get(mode, {}).get("mean_ret") or pb is None:
                continue
            rows[mode]["rel_prod_pp"] = round(rows[mode]["mean_ret"] - pb, 4)
            print(f"  [{MODES[mode]['label']}] vs 生产: {rows[mode]['rel_prod_pp']:+.2f}pp")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(dict(days=days, rows=rows,
                       note="2010-2017 历史窗口(仅跨环境稳健性验证用, 不计入显著性计数)"),
                  f, ensure_ascii=False, indent=2)
    print("\n已保存:", OUT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--days-from", default=DEF_RESULTS)
    ap.add_argument("--only", default="")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    modes = [m.strip() for m in a.only.split(",") if m.strip()] or list(MODES)
    bad = [m for m in modes if m not in MODES]
    if bad:
        print(f"[fatal] 未知口径: {bad}; 可用: {list(MODES)}")
        sys.exit(2)
    print("口径: " + ", ".join(f"{m}({MODES[m]['label']},suffix='{MODES[m]['suffix']}')"
                               for m in modes))
    days = _load_days(a.days_from)
    probe(days, modes, a.n)
    if a.write:
        write_matrix(days, modes, a.n)
    else:
        print("\n探口径完成. 若需全仿真复算请加 --write.")


if __name__ == "__main__":
    main()

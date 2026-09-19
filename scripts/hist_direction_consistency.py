# -*- coding: utf-8 -*-
"""方向一致性检查 (§⑨ 执行路径第 3-4 步): 2010-2017 四因子 IC 方向 vs 2018+.

背景
  §⑨ 已裁定: 2010-2017 历史窗口**仅用于跨环境稳健性验证**, 不用于凑 n>=20 的
  显著性计数。融合四因子权重 (pb_inv 0.73 / ep 0.7456 / ocf_ps 0.546 /
  roe_yy_chg 1.0618) 全部在 **2018+ 月度中性化面板**上核定, 历史段直接套用存在
  "校准域外推 -> 伪显著"的风险。故扩展历史窗口前必须先验证: 这四个因子在
  2010-2017 的 IC 方向是否与 2018+ 一致。

做法 (与生产同管道, 不另起口径)
  对每个候选决策日 D:
    1) 复用 factor_fusion 的截面装配 (_Snap PIT 财务排期 + valuation 同日可见 +
       当日 bar + _assemble_snapshot), 取四因子原值。两套截面口径:
         prod: 截面 = 当前 active sz/sh  —— 与融合权重校准口径一致 (主判据)
         pit : 截面 = 当日有 bar 的全部标的 —— 无幸存者偏差, 作稳健性对照
    2) 前向收益 = (D, D+120 交易日] 的 change_pct 复利 (与 factor_ic_forward 同口径)
    3) 每因子算 Spearman RankIC, 两种取值:
         raw: 因子原值
         neu: Winsorize(1/99) -> OLS 残差(ln_size + 行业) -> z (与生产 _residualize 同函数)

判定 (按 §⑨ 约定)
  基准方向 = 2018+ 参照窗口 (--ref-file 里的成功决策日) 的同口径 IC 方向。
    - 4 因子全部同向  -> 计入跨环境验证 (in_env)
    - 部分因子反向    -> 标记"环境外" (mixed), 不计入截尾 alpha 显著性计数
    - 全部反向        -> 该窗口不可用于验证 (reverse), 仅作参考

用法
  python scripts/hist_direction_consistency.py                # 全量: 2010-2017 + 2018+ 参照
  python scripts/hist_direction_consistency.py --days 2015-06-30 2015-12-31   # 指定日期自检
输出
  data/hist_direction_consistency.json
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

import factor_fusion as ff  # noqa: E402
from vnpy_backtest import forward_window_days  # noqa: E402

FW = os.path.join(_BASE, "data", "vnpy_backtest_nonoverlap_fwd_results.json")
OUT = os.path.join(_BASE, "data", "hist_direction_consistency.json")

FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]
FWD_TD = 120           # 前向交易日数 (与 OOS 窗口一致)
MIN_N = 50             # 单因子 IC 的最小对齐样本

# 2010-2017 候选决策日: 沿用 nonoverlap_rerun 的"年中/年末"约定 (6-12 个月间隔),
# 每个窗口 = [决策日, +120 交易日]。落在 2018-06-29 之前的最后一个候选日
# (2017-12-29) 与既有 OOS 首窗首尾相接, 不重叠。
CAND_HIST = [
    "2010-06-30", "2010-12-31", "2011-06-30", "2011-12-30",
    "2012-06-29", "2012-12-31", "2013-06-28", "2013-12-31",
    "2014-06-30", "2014-12-31", "2015-06-30", "2015-12-31",
    "2016-06-30", "2016-12-30", "2017-06-30", "2017-12-29",
]


def _fwd_ret(day: str, end: str) -> pd.DataFrame:
    """(day, end] 区间复利收益(%) — 与 factor_ic_forward.fwd_return 同口径。"""
    q = ("SELECT symbol AS sym, "
         "EXP(SUM(LN(1 + change_pct / 100.0))) - 1 AS ret "
         "FROM daily_bars "
         f"WHERE CAST(ts AS DATE) > DATE '{day}' AND CAST(ts AS DATE) <= DATE '{end}' "
         "AND change_pct IS NOT NULL "
         "GROUP BY symbol")
    df = ff._sql(q)
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol", "ret"])
    df["symbol"] = df["sym"].astype(str).str.zfill(6)
    df["ret"] = pd.to_numeric(df["ret"], errors="coerce") * 100.0
    return df[["symbol", "ret"]]


def _xsec(day: str):
    """当日两套截面的因子原值表 (prod / pit)。无交易日返回 (None, None, None)。"""
    D = ff._snap_date(day)
    if D is None:
        return None, None, None
    fin = ff._fin_frame()
    val = ff._val_frame(D - pd.Timedelta(days=730))
    snap = ff._Snap(fin, val)
    snap.advance(D)
    bar = ff._bar_day(D)
    if bar.empty:
        return D, None, None
    all_syms = set(bar["symbol"].astype(str))
    prod = ff._assemble_snapshot(snap, bar, ff._active_szsh())
    pit = ff._assemble_snapshot(snap, bar, all_syms)
    return D, prod, pit


def _ic(df: pd.DataFrame, ret: pd.DataFrame, factor: str, neu: bool):
    """Spearman RankIC(因子, 前向收益). 样本不足返回 (None, n)。"""
    if df is None or df.empty or ret.empty:
        return None, 0
    if neu:
        z, _meta = ff._residualize(df, factor)
        if not z:
            return None, 0
        sub = pd.DataFrame({"symbol": list(z.keys()), "v": list(z.values())})
    else:
        sub = df[["symbol", factor]].rename(columns={factor: "v"}).copy()
        sub["v"] = pd.to_numeric(sub["v"], errors="coerce")
    m = sub.merge(ret, on="symbol", how="inner")
    m["v"] = pd.to_numeric(m["v"], errors="coerce")
    m["ret"] = pd.to_numeric(m["ret"], errors="coerce")
    m = m[np.isfinite(m["v"]) & np.isfinite(m["ret"])]
    if len(m) < MIN_N:
        return None, int(len(m))
    return float(m["v"].astype(float).corr(m["ret"].astype(float), method="spearman")), int(len(m))


def _one_day(day: str) -> dict:
    t0 = time.time()
    D, prod, pit = _xsec(day)
    if D is None:
        return {"day": day, "ok": False, "error": "no trading day"}
    cal = forward_window_days(D.date(), FWD_TD)
    if not cal:
        return {"day": day, "ok": False, "error": "forward data insufficient"}
    end = str(cal[-1])[:10]
    ret = _fwd_ret(D.strftime("%Y-%m-%d"), end)
    if ret.empty:
        return {"day": day, "ok": False, "error": "no forward returns"}
    rec = {"day": D.strftime("%Y-%m-%d"), "end": end, "ok": True,
           "n_end": len(cal), "n_ret": int(len(ret)),
           "n_prod": int(len(prod)) if prod is not None else 0,
           "n_pit": int(len(pit)) if pit is not None else 0,
           "elapsed": None, "ic": {}}
    for tag, df in (("prod", prod), ("pit", pit)):
        rec["ic"][tag] = {}
        for f in FACTORS:
            raw, n_raw = _ic(df, ret, f, neu=False)
            neu, n_neu = _ic(df, ret, f, neu=True)
            rec["ic"][tag][f] = {"raw": raw, "neu": neu, "n_raw": n_raw, "n_neu": n_neu}
    rec["elapsed"] = round(time.time() - t0, 1)
    return rec


def _sign(x):
    if x is None or not np.isfinite(x) or abs(x) < 1e-12:
        return 0
    return 1 if x > 0 else -1


def _classify(rec: dict, ref: dict, tag: str, key: str) -> dict:
    """按基准方向判定单窗口一致性。"""
    flags, ics = {}, {}
    for f in FACTORS:
        v = (rec.get("ic", {}).get(tag, {}).get(f) or {}).get(key)
        ics[f] = v
        flags[f] = (_sign(v) == ref[f]) if _sign(v) != 0 else False
    n_ok = sum(1 for f in FACTORS if flags[f])
    n_bad = sum(1 for f in FACTORS if (_sign(ics[f]) != 0 and not flags[f]))
    if n_ok == len(FACTORS):
        cls = "in_env"
    elif n_ok == 0 and n_bad == len(FACTORS):
        cls = "reverse"
    else:
        cls = "mixed"
    return {"class": cls, "n_same": n_ok, "n_opposite": n_bad,
            "flags": flags, "ic": ics}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*", default=None,
                    help="只跑指定日期(自检用); 省略则跑 2010-2017 候选日 + 2018+ 参照日")
    ap.add_argument("--no-ref", action="store_true", help="跳过 2018+ 参照窗口")
    args = ap.parse_args()

    # 2018+ 参照日: 既有前向窗口里 ok 的决策日 (同管道同口径)
    ref_days = []
    if os.path.exists(FW):
        ref_days = [x["day"] for x in json.load(open(FW, encoding="utf-8")) if x.get("ok")]
    ref_days = sorted(ref_days)

    if args.days:
        hist_days = list(args.days)
    else:
        hist_days = list(CAND_HIST)

    print(f"方向一致性检查: 历史候选日 {len(hist_days)} 个, "
          f"2018+ 参照日 {0 if args.no_ref else len(ref_days)} 个")
    print(f"因子: {', '.join(FACTORS)}; 前向 {FWD_TD} 交易日; 最小样本 {MIN_N}\n")

    done: dict[str, dict] = {}
    order = sorted(set(hist_days) | (set() if args.no_ref else set(ref_days)))
    for day in order:
        r = _one_day(day)
        done[day] = r
        if not r.get("ok"):
            print(f"  {day}: 跳过 ({r.get('error')})", flush=True)
            continue
        pr = r["ic"]["prod"]
        cells = "  ".join(
            f"{f}={'n/a' if pr[f]['neu'] is None else format(pr[f]['neu'], '+.3f')}"
            for f in FACTORS)
        print(f"  {day}  end={r['end']}  n_prod={r['n_prod']:>5} n_pit={r['n_pit']:>5}  "
              f"[neu/prod] {cells}  ({r['elapsed']}s)", flush=True)

    # ---- 基准方向: 2018+ 参照窗口的 IC 均值符号 (prod + neu 为主判据) ----
    def _mean_ic(days, tag, key):
        out = {}
        for f in FACTORS:
            vs = [(done[d].get("ic", {}).get(tag, {}).get(f) or {}).get(key)
                  for d in days if done.get(d, {}).get("ok")]
            vs = [v for v in vs if v is not None and np.isfinite(v)]
            out[f] = (float(np.mean(vs)) if vs else None, len(vs))
        return out

    ref_ok = [d for d in ref_days if done.get(d, {}).get("ok")]
    ref_mean = {tag: {key: _mean_ic(ref_ok, tag, key) for key in ("raw", "neu")}
                for tag in ("prod", "pit")}
    ref_sign = {key: {f: _sign(ref_mean["prod"][key][f][0]) for f in FACTORS}
                for key in ("raw", "neu")}

    def _fmt_cell(f: str, key: str) -> str:
        mean, n = ref_mean["prod"][key][f]
        arrow = "+" if ref_sign[key][f] > 0 else ("-" if ref_sign[key][f] < 0 else "0")
        if mean is None:
            return f"{f}: n/a(n=0)->{arrow}"
        return f"{f}: {mean:+.4f}(n={n})->{arrow}"

    print("\n=== 2018+ 参照方向 (prod 截面) ===")
    for key in ("raw", "neu"):
        print(f"  [{key}] " + "  ".join(_fmt_cell(f, key) for f in FACTORS))

    # ---- 逐历史窗口判定 (主判据 prod+neu; 另有 raw / pit 对照) ----
    rows = []
    for d in hist_days:
        r = done.get(d)
        if not r or not r.get("ok"):
            rows.append({"day": d, "ok": False, "error": (r or {}).get("error")})
            continue
        item = {"day": d, "ok": True, "end": r["end"], "n_prod": r["n_prod"],
                "n_pit": r["n_pit"], "judge": {}, "ic": r["ic"]}
        for tag in ("prod", "pit"):
            for key in ("raw", "neu"):
                item["judge"][f"{tag}_{key}"] = _classify(r, ref_sign[key], tag, key)
        rows.append(item)

    primary = [x for x in rows if x.get("ok")]
    n_mixed = sum(1 for x in primary if x["judge"]["prod_neu"]["class"] == "mixed")
    n_rev = sum(1 for x in primary if x["judge"]["prod_neu"]["class"] == "reverse")
    n_in = sum(1 for x in primary if x["judge"]["prod_neu"]["class"] == "in_env")
    out_of_env = n_mixed + n_rev
    share = (out_of_env / len(primary)) if primary else 0.0

    print(f"\n=== 2010-2017 判定 (主判据: prod 截面 + 中性化 IC) ===")
    print(f"  同向(in_env) {n_in}/{len(primary)}   部分反向(mixed) {n_mixed}   "
          f"全部反向(reverse) {n_rev}")
    print(f"  '环境外'占比 = {out_of_env}/{len(primary)} = {share:.0%}  "
          f"-> 阈值 50% {'**超过, 扩展价值有限**' if share > 0.5 else '未超过, 可按协议推进'}")
    per_factor = {}
    for f in FACTORS:
        bad = sum(1 for x in primary if not x["judge"]["prod_neu"]["flags"][f])
        per_factor[f] = bad
    print("  逐因子反向窗口数: " + "  ".join(f"{f}={per_factor[f]}" for f in FACTORS))

    result = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "params": {"fwd_td": FWD_TD, "min_n": MIN_N, "factors": FACTORS},
        "ref_days_2018plus": ref_days,
        "ref_mean_ic": {tag: {k: {f: {"mean": ref_mean[tag][k][f][0],
                                      "n": ref_mean[tag][k][f][1]} for f in FACTORS}
                              for k in ("raw", "neu")} for tag in ("prod", "pit")},
        "ref_sign": ref_sign,
        "hist": rows,
        "summary": {"n_hist_ok": len(primary), "in_env": n_in, "mixed": n_mixed,
                    "reverse": n_rev, "out_of_env_share": round(share, 4),
                    "per_factor_opposite": per_factor},
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print("\n已保存:", OUT)


if __name__ == "__main__":
    main()

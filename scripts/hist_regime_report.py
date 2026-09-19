# -*- coding: utf-8 -*-
"""2010-2017 历史窗口的跨环境稳健性报告 (hist_regime_report.py).

把三份产物合成一张"按市场状态分层"的表:
  1. data/vnpy_backtest_nonoverlap_fwd_results_hist1017.json  —— 历史窗口生产排序 OOS
     (可选 --alt 加一份对照口径, 如 data/..._h17t05fix.json)
  2. data/hist_direction_consistency.json  —— 每窗口四因子 IC 方向一致性判定
  3. data/hist_six_grid.json (若存在)      —— 历史窗口多口径均值

分层 (§⑨ 约定)
  2010-2012 壳价值期(审核稀缺) / 2013-2015 成长+杠杆牛(含 2015 崩盘) /
  2016-2017 熔断后价值回归

口径声明
  历史窗口**仅用于跨环境稳健性验证**, 不计入截尾 alpha 的显著性计数(n>=20 仍由 2018+
  未来窗口达成); "环境外"窗口单列, 不参与同向统计。

用法
    python scripts/hist_regime_report.py
    python scripts/hist_regime_report.py --alt data/vnpy_backtest_nonoverlap_fwd_results_h17t05fix.json --alt-label 截尾5%
输出
    data/hist_regime_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
os.chdir(_BASE)

PROD = os.path.join(_BASE, "data",
                    "vnpy_backtest_nonoverlap_fwd_results_hist1017.json")
DIRC = os.path.join(_BASE, "data", "hist_direction_consistency.json")
GRID = os.path.join(_BASE, "data", "hist_six_grid.json")
OUT = os.path.join(_BASE, "data", "hist_regime_report.json")

REGIMES = [("2010-2012 壳价值期", 2010, 2012),
           ("2013-2015 成长+杠杆牛", 2013, 2015),
           ("2016-2017 熔断后价值回归", 2016, 2017)]
#: 已知口径降级窗口(fusion 覆盖 <0.8 → f_ml 断链 → used=equal): 排序不含融合加成
DEGRADED = {"2015-06-30": "fusion 覆盖 0.69(<0.80) 且 f_ml 链路断(duckdb daily_bars 缺失) "
                          "→ used=equal, 生产排序退化为旧复合分"}


def _engine(stats: dict) -> str:
    """vnpy 全仿真 vs 自研简化 fallback (后者无 Sharpe/MDD 口径, 不可与前者同表)."""
    return "fallback" if "method" in stats else "vnpy"


def _regime(day: str) -> str:
    y = int(day[:4])
    for name, lo, hi in REGIMES:
        if lo <= y <= hi:
            return name
    return "其它"


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    return {x["day"]: x for x in json.load(open(path, encoding="utf-8")) if x.get("ok")}


def _agg(rows: list[dict], field: str):
    vals = [float((r.get("stats") or {}).get(field, 0.0)) for r in rows]
    return (st.mean(vals) if vals else None), (st.median(vals) if vals else None), len(vals)


#: 五口径 -> (后缀, 标签)：与 hist_six_grid.MODES 一致
MODE_SUF = [("h17p", "生产排序"), ("h17t03fix", "截尾3%"), ("h17t05fix", "截尾5%"),
            ("h17t08fix", "截尾8%"), ("h17rf1fix", "纯融合")]
ENVCLASS_OUT = os.path.join(_BASE, "data", "hist_six_grid_by_envclass.json")


def envclass_split(cls: dict, prod_days: list[str]) -> None:
    """按 §⑨ 第 4 条把五口径拆成"方向同向 / 环境外"两组。

    存在的理由：全量聚合会被"环境外"窗口主导而得出与协议相反的结论
    （2026-09-18 实测：全量 15 窗截尾 +2.0~+6.1pp，但同向 8 窗为 -0.3~-1.0pp）。
    """
    modes = {}
    for suf, lab in MODE_SUF:
        fp = os.path.join(_BASE, "data",
                          f"vnpy_backtest_nonoverlap_fwd_results_{suf}.json")
        if not os.path.exists(fp):
            print(f"[warn] 缺口径产物, 跳过: {os.path.basename(fp)}")
            continue
        modes[lab] = {x["day"]: x for x in json.load(open(fp, encoding="utf-8"))
                      if x.get("ok")}
    if "生产排序" not in modes:
        print("[warn] 无生产排序产物, 跳过环境分层")
        return

    def _usable(day: str) -> bool:
        return "method" not in ((modes["生产排序"][day].get("stats") or {}))

    in_env = [d for d in prod_days if cls.get(d) == "in_env" and _usable(d)]
    out_env = [d for d in prod_days if cls.get(d) not in (None, "in_env") and _usable(d)]

    def _ret(lab: str, d: str) -> float:
        return float((modes[lab][d].get("stats") or {}).get("total_return", 0.0))

    def _tbl(ws: list[str]) -> dict:
        out = {}
        for _, lab in MODE_SUF:
            if lab not in modes or not ws:
                continue
            out[lab] = round(st.mean([_ret(lab, d) for d in ws]), 4)
        if "生产排序" in out:
            pb = out["生产排序"]
            for lab in list(out):
                if lab != "生产排序":
                    out[lab + "_vs_prod_pp"] = round(out[lab] - pb, 4)
        return out

    print(f"\n=== 按方向一致性分层（协议口径: 只有'同向'窗口可计数） ===")
    print(f"  同向可用 {len(in_env)} 窗: {in_env}")
    print(f"  环境外可用 {len(out_env)} 窗: {out_env}")
    a, b = _tbl(in_env), _tbl(out_env)
    print(f"  {'口径':<8}{'同向均值%':>11}{'vs生产':>9}{'环境外均值%':>13}{'vs生产':>9}")
    for _, lab in MODE_SUF:
        if lab not in a:
            continue
        print(f"  {lab:<8}{a[lab]:>11.2f}{a.get(lab + '_vs_prod_pp', 0.0):>+9.2f}"
              f"{b.get(lab, float('nan')):>13.2f}{b.get(lab + '_vs_prod_pp', 0.0):>+9.2f}")
    with open(ENVCLASS_OUT, "w", encoding="utf-8") as f:
        json.dump({"in_env_windows": in_env, "out_env_windows": out_env,
                   "in_env": a, "out_env": b,
                   "note": "§⑨ 第4条: 环境外窗口不计入截尾 alpha 显著性计数"},
                  f, ensure_ascii=False, indent=2)
    print("  已保存:", ENVCLASS_OUT)


RULE_OUT = os.path.join(_BASE, "data", "hist_envclass_rule_compare.json")
#: 参与方向一致性的四因子(与 hist_direction_consistency.FACTORS 一致)
FACTORS = ["pb_inv", "ep", "ocf_ps", "roe_yy_chg"]


def rule_compare(prod_days: list[str]) -> None:
    """阈值敏感性: "0 个反向才计入" vs "反向数 < 2 即计入" 两种判据下重算五口径.

    存在的理由(2026-09-19 实测): 只差 2 个"1 反向"窗口, 两个关键问题的答案就互换 ——
      规则A(n=8): 截尾无增益(-0.31~-1.00pp) / 纯融合 -2.77pp(与 2018+ 同号, 劣);
      规则B(n=10): 截尾有增益(+1.73~+4.52pp) / 纯融合 +2.32pp(与 2018+ **反号**)。
    故本表是 docs/hist-window-protocol.md 规定的**必报项**: 两阈值结论不同时,
    结论应表述为"历史样本不足以判别", 不得择一取用。
    """
    from collections import Counter
    modes = {}
    for suf, lab in MODE_SUF:
        fp = os.path.join(_BASE, "data",
                          f"vnpy_backtest_nonoverlap_fwd_results_{suf}.json")
        if not os.path.exists(fp):
            print(f"[warn] 缺口径产物, 跳过阈值敏感性: {os.path.basename(fp)}")
            return
        modes[lab] = {x["day"]: x for x in json.load(open(fp, encoding="utf-8"))
                      if x.get("ok")}
    if "生产排序" not in modes or not os.path.exists(DIRC):
        return

    def _usable(day: str) -> bool:
        return "method" not in ((modes["生产排序"][day].get("stats") or {}))

    def _ret(lab: str, d: str) -> float:
        return float((modes[lab][d].get("stats") or {}).get("total_return", 0.0))

    hist = json.load(open(DIRC, encoding="utf-8"))
    nrev = {}
    for x in hist.get("hist", []):
        if x.get("ok"):
            nrev[x["day"]] = sum(1 for f in FACTORS
                                 if not x["judge"]["prod_neu"]["flags"][f])

    rules = {"A_0reverse": (lambda n: n == 0, "0 个反向才计入"),
             "B_lt2reverse": (lambda n: n < 2, "反向数 < 2 即计入")}
    dist = Counter(v for d, v in nrev.items() if _usable(d))
    print("\n=== 阈值敏感性 (协议必报项) ===")
    print(f"  历史段反向因子数分布: {dict(sorted(dist.items()))}")
    print(f"  {'规则':<14}{'计入':>5}" + "".join(f"{lab:>12}" for _, lab in MODE_SUF))
    out = {"hist_reverse_distribution": dict(sorted(dist.items())),
           "ref_days_2018plus": hist.get("ref_days_2018plus") or [],
           "rules": {},
           "note": "两阈值结论不同 -> 历史样本不足以判别, 不得择一取用"}
    for rname, (pred, desc) in rules.items():
        sel = [d for d, v in nrev.items() if pred(v) and _usable(d)]
        means = {lab: round(st.mean([_ret(lab, d) for d in sel]), 4)
                 for _, lab in MODE_SUF}
        pb = means.get("生产排序")
        rel = {lab: round(v - pb, 4) for lab, v in means.items()
               if lab != "生产排序" and pb is not None}
        out["rules"][rname] = {"desc": desc, "n": len(sel), "windows": sorted(sel),
                               "mean_ret": means, "vs_prod_pp": rel}
        print(f"  {rname:<14}{len(sel):>5}"
              + "".join(f"{means.get(lab, float('nan')):>12.2f}" for _, lab in MODE_SUF))
    a = out["rules"].get("A_0reverse", {}).get("vs_prod_pp", {})
    b = out["rules"].get("B_lt2reverse", {}).get("vs_prod_pp", {})
    flip = [k for k in a if k in b and a[k] * b[k] < 0]
    out["flipped_modes"] = flip
    if flip:
        print(f"  **两阈值下结论变号({', '.join(flip)}) -> 历史样本不足以判别**")
    with open(RULE_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("  已保存:", RULE_OUT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", default=PROD)
    ap.add_argument("--alt", default="")
    ap.add_argument("--alt-label", default="对照口径")
    ap.add_argument("--no-envclass", action="store_true",
                    help="跳过'按方向一致性分层'的五口径核算")
    a = ap.parse_args()

    prod = _load(a.prod)
    alt = _load(a.alt) if a.alt else {}
    dirc = json.load(open(DIRC, encoding="utf-8")) if os.path.exists(DIRC) else {}
    cls = {x["day"]: x["judge"]["prod_neu"]["class"] for x in dirc.get("hist", [])
           if x.get("ok")}
    days = sorted(prod)

    print(f"历史窗口 {len(days)} 个 (生产排序: {os.path.basename(a.prod)})")
    if alt:
        print(f"对照口径: {a.alt_label} ({os.path.basename(a.alt)})")
    print("=" * 118)
    hdr = (f"{'决策日':<12}{'市场状态':<22}{'方向':<9}{'收益%':>9}{'Sharpe':>9}"
           f"{'MDD%':>9}")
    if alt:
        hdr += f"{a.alt_label + '收益%':>14}{'Δpp':>9}"
    print(hdr)
    print("-" * 118)

    per_regime: dict[str, list] = {}
    excluded: list[str] = []
    for d in days:
        r = prod[d]
        stats = r.get("stats") or {}
        ret = float(stats.get("total_return", 0.0))
        eng = _engine(stats)
        row = {"day": d, "regime": _regime(d), "dir_class": cls.get(d, "n/a"),
               "total_return": round(ret, 4),
               "sharpe": stats.get("sharpe_ratio"),
               "mdd": stats.get("max_ddpercent"),
               "engine": eng, "degraded": DEGRADED.get(d)}
        line = (f"{d:<12}{row['regime']:<22}{row['dir_class']:<9}{ret:>9.2f}"
                f"{(stats.get('sharpe_ratio') if stats.get('sharpe_ratio') is not None else float('nan')):>9.2f}"
                f"{(stats.get('max_ddpercent') if stats.get('max_ddpercent') is not None else float('nan')):>9.2f}")
        if alt:
            ar = alt.get(d)
            if ar:
                aret = float((ar.get("stats") or {}).get("total_return", 0.0))
                row["alt_return"] = round(aret, 4)
                row["alt_delta_pp"] = round(aret - ret, 4)
                line += f"{aret:>14.2f}{aret - ret:>9.2f}"
            else:
                line += f"{'--':>14}{'--':>9}"
        if eng != "vnpy":
            line += "   ← 排除(自研 fallback 引擎, 与 vnpy 口径不可比)"
            excluded.append(d)
        print(line)
        if eng == "vnpy":
            per_regime.setdefault(row["regime"], []).append(row)

    print("-" * 118)
    print("\n=== 按市场状态分层 (生产排序) ===")
    summary = {}
    for name, _, _ in REGIMES:
        rs = per_regime.get(name, [])
        if not rs:
            continue
        m, med, n = _agg([prod[x["day"]] for x in rs], "total_return")
        sm, _, _ = _agg([prod[x["day"]] for x in rs], "sharpe_ratio")
        mm, _, _ = _agg([prod[x["day"]] for x in rs], "max_ddpercent")
        same = [x for x in rs if x["dir_class"] == "in_env"]
        oth = [x for x in rs if x["dir_class"] != "in_env"]
        item = {"n": n, "mean_ret": round(m, 4) if m is not None else None,
                "median_ret": round(med, 4) if med is not None else None,
                "mean_sharpe": round(sm, 4) if sm is not None else None,
                "mean_mdd": round(mm, 4) if mm is not None else None,
                "pos_windows": sum(1 for x in rs if x["total_return"] > 0),
                "n_in_env": len(same), "n_out_env": len(oth)}
        if same:
            m2, _, _ = _agg([prod[x["day"]] for x in same], "total_return")
            item["in_env_mean_ret"] = round(m2, 4) if m2 is not None else None
        if alt:
            am, _, _ = _agg([alt[x["day"]] for x in rs if x["day"] in alt],
                            "total_return")
            item["alt_mean_ret"] = round(am, 4) if am is not None else None
            if am is not None and m is not None:
                item["alt_rel_prod_pp"] = round(am - m, 4)
        summary[name] = item
        extra = (f"  | 同向 {len(same)}/{n}"
                 + (f" 同向组均值 {item.get('in_env_mean_ret'):+.2f}%" if same else ""))
        if alt and item.get("alt_rel_prod_pp") is not None:
            extra += f"  | {a.alt_label} 均值 {item['alt_mean_ret']:+.2f}% "\
                     f"({item['alt_rel_prod_pp']:+.2f}pp)"
        print(f"  {name:<22} n={n}  均值 {item['mean_ret']:+.2f}%  中位 "
              f"{item['median_ret']:+.2f}%  正 {item['pos_windows']}/{n}  "
              f"Sharpe {item['mean_sharpe']:+.2f}  MDD {item['mean_mdd']:+.2f}%{extra}")

    usable = [d for d in days if d not in excluded]
    m_all, med_all, n_all = _agg([prod[d] for d in usable], "total_return")
    inenv = [d for d in usable if cls.get(d) == "in_env"]
    m_in, _, n_in = _agg([prod[d] for d in inenv], "total_return") if inenv else (None, None, 0)
    prog = dict(n_windows=n_all, n_excluded=len(excluded), excluded_windows=excluded,
                mean_ret=round(m_all, 4) if m_all is not None else None,
                median_ret=round(med_all, 4) if med_all is not None else None,
                n_in_env=n_in,
                in_env_mean_ret=round(m_in, 4) if m_in is not None else None,
                n_degraded=len([d for d in days if d in DEGRADED]))
    print(f"\n=== 全历史合计 ===\n  n={n_all}  均值 {prog['mean_ret']:+.2f}%  "
          f"中位 {prog['median_ret']:+.2f}%")
    print(f"  方向同向窗口 {n_in}/{n_all}  同向组均值 "
          f"{prog['in_env_mean_ret']:+.2f}%" if n_in else "  无同向窗口")
    print(f"  口径降级窗口 {prog['n_degraded']} 个: {list(DEGRADED)}")
    if excluded:
        print(f"  已排除(非 vnpy 引擎) {len(excluded)} 个: {excluded}")
    if alt:
        am, _, _ = _agg([alt[d] for d in usable if d in alt], "total_return")
        if am is not None and m_all is not None:
            prog["alt_label"] = a.alt_label
            prog["alt_mean_ret"] = round(am, 4)
            prog["alt_rel_prod_pp"] = round(am - m_all, 4)
            print(f"  {a.alt_label} 均值 {am:+.2f}%  vs 生产 {am - m_all:+.2f}pp")

    if not a.no_envclass:
        envclass_split(cls, days)
        rule_compare(days)

    print("\n注: 历史窗口仅作跨环境稳健性验证, 不计入截尾 alpha 的显著性计数。")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"prod_file": os.path.basename(a.prod),
                   "alt_file": os.path.basename(a.alt) if a.alt else None,
                   "windows": [x for k in per_regime for x in per_regime[k]],
                   "excluded_windows": excluded,
                   "by_regime": summary, "overall": prog,
                   "degraded": DEGRADED,
                   "note": "2010-2017 仅跨环境稳健性验证; 不计入显著性计数"},
                  f, ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()

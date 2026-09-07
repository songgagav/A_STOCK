# -*- coding: utf-8 -*-
# ============================================================
# fusion_daily_validate.py -- 方案A 逐日验证 (只读复放, 不触碰生产持仓/盘口)
#
# 用途
# ----
#   用 factor_fusion.score_series_hist 对最近 N(=60) 个历史交易日逐日
#   (as_of = 当日收盘后可见数据)计算 fused 分数, 与次日/5日真实收益对齐:
#     - 日度 IC 序列 (spearman) -> 日均 IC / ICIR / 胜率
#     - 5 分组 (Q0<Q4) 组均收益 / Q4-Q0 / 单调性
#     - 融合分数与 ln_size / 一级行业的正交 r
#     - 样本覆盖 (pool_n / scored_n / coverage)
#   输出 data/factor_mine/fusion_daily_validate.json (中文摘要 + 逐日明细)
#
#   另附:
#     - fallback 决策自检 (monkeypatch, 不依赖外部 ml_fusion venv)
#     - BacktestRunner(days=6) 只读复放期末权益 (证明撮合引擎仍工作)
#
# 运行: cd A_stock_rotation && python fusion_daily_validate.py [--days 60]
# ============================================================
from __future__ import annotations

import json
import logging
import math
import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

import factor_fusion as ff  # noqa: E402

OUT_JSON = os.path.join(_BASE, "data", "factor_mine", "fusion_daily_validate.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("fusion_daily_validate")


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------
def _nan(x):
    return None if (x is None or (isinstance(x, float) and math.isnan(x))) else x


def _ic_day(scores: dict, rets: dict) -> dict:
    sym = [s for s in scores if s in rets and np.isfinite(rets[s])]
    if len(sym) < 30:
        return {"n": len(sym), "ic": None}
    x = np.array([scores[s] for s in sym], dtype=float)
    y = np.array([rets[s] for s in sym], dtype=float)
    try:
        rho, _p = spearmanr(x, y)
    except Exception:
        return {"n": len(sym), "ic": None}
    if not np.isfinite(rho):
        return {"n": len(sym), "ic": None}
    return {"n": len(sym), "ic": float(rho)}


def _quintile(scores: dict, rets: dict):
    sym = [s for s in scores if s in rets and np.isfinite(rets[s])]
    if len(sym) < 50:
        return None
    x = np.array([scores[s] for s in sym], dtype=float)
    y = np.array([rets[s] for s in sym], dtype=float)
    try:
        q = pd.qcut(pd.Series(x), 5, labels=False, duplicates="drop")
    except Exception:
        return None
    g = pd.Series(y).groupby(q.values).mean()
    if len(g) < 5 or any(not np.isfinite(g.get(i, np.nan)) for i in range(5)):
        return None
    return [float(g[i]) for i in range(5)]


def _orth_r(scores: dict, ln_size: dict, industry: dict) -> dict:
    sym = [s for s in scores if ln_size.get(s) is not None]
    if len(sym) < 50:
        return {}
    x = np.array([scores[s] for s in sym], dtype=float)
    sz = np.array([ln_size[s] for s in sym], dtype=float)
    r_size = float(np.corrcoef(x, sz)[0, 1]) if sz.std() > 0 else np.nan
    inds = sorted({industry.get(s) for s in sym})
    rs = []
    for k in inds:
        d = np.array([1.0 if industry.get(s) == k else 0.0 for s in sym])
        if d.std() > 0:
            rs.append(float(np.corrcoef(x, d)[0, 1]))
    return {
        "r_ln_size": (_nan(r_size) if np.isfinite(r_size) else None),
        "mean_abs_r_ind": (round(float(np.mean(np.abs(rs))), 5) if rs else None),
        "n_ind": len(rs),
    }


# ---------------------------------------------------------------------------
# 逐日验证主流程
# ---------------------------------------------------------------------------
def run_daily_validate(days: int = 60, end: str = None) -> dict:
    t0 = time.time()
    series = ff.score_series_hist(end, days=days)
    if "error" in series:
        raise SystemExit(f"score_series_hist 失败: {series['error']}")
    samples = series["samples"]
    _LOG.info("逐日出分 %s..%s n=%d",
              series["as_of_start"], series["as_of_end"], series["n_days"])

    per_day = []
    ic1, ic5, n1v, n5v = [], [], [], []
    q1_rows, q5_rows = [], []
    r_sz1, r_sz5, r_ind1, r_ind5 = [], [], [], []
    covs, pool_ns, scored_ns = [], [], []

    for row in samples:
        sc = row["scores"]
        pool_n = row["n_pool"]
        scored_n = row["n_scored"]
        cov = row["coverage"] if row.get("coverage") is not None else (
            len(sc) / pool_n if pool_n else 0.0)
        pool_ns.append(pool_n)
        scored_ns.append(scored_n)
        covs.append(cov)
        rec = {"date": row["date"], "n_pool": pool_n, "n_scored": scored_n,
               "coverage": round(float(cov), 4)}

        r1 = _ic_day(sc, row["fwd1"])
        r5 = _ic_day(sc, row["fwd5"])
        rec["ic_fwd1"] = (round(r1["ic"], 5) if r1["ic"] is not None else None)
        rec["n_fwd1"] = r1["n"]
        rec["ic_fwd5"] = (round(r5["ic"], 5) if r5["ic"] is not None else None)
        rec["n_fwd5"] = r5["n"]
        if r1["ic"] is not None:
            ic1.append(r1["ic"]); n1v.append(r1["n"])
        if r5["ic"] is not None:
            ic5.append(r5["ic"]); n5v.append(r5["n"])

        q1 = _quintile(sc, row["fwd1"])
        q5 = _quintile(sc, row["fwd5"])
        if q1:
            q1_rows.append(q1)
            rec["q_fwd1"] = [round(v, 6) for v in q1]
        if q5:
            q5_rows.append(q5)
            rec["q_fwd5"] = [round(v, 6) for v in q5]

        o1 = _orth_r(sc, row["ln_size"], row["industry"])
        if o1.get("r_ln_size") is not None:
            r_sz1.append(o1["r_ln_size"])
        if o1.get("mean_abs_r_ind") is not None:
            r_ind1.append(o1["mean_abs_r_ind"])
        rec["orth"] = {"r_ln_size": _nan(o1.get("r_ln_size")),
                       "mean_abs_r_ind": _nan(o1.get("mean_abs_r_ind"))}
        per_day.append(rec)

    def _agg(ics):
        if not ics:
            return {"mean": None, "std": None, "icir": None,
                    "win_rate_gt0": None, "n_days": 0}
        a = np.asarray(ics, dtype=float)
        return {"mean": float(a.mean()), "std": float(a.std(ddof=1)) if len(a) > 1 else None,
                "icir": (float(a.mean() / a.std(ddof=1)) if len(a) > 1
                         and a.std(ddof=1) > 1e-12 else None),
                "win_rate_gt0": float((a > 0).mean()) if len(a) else None,
                "n_days": len(a)}

    def _qagg(rows):
        if not rows:
            return None
        a = np.asarray(rows, dtype=float)   # (days,5)
        qm = a.mean(axis=0)
        mono_up = bool(all(qm[i] <= qm[i + 1] + 1e-9 for i in range(4)))
        mono_dn = bool(all(qm[i] >= qm[i + 1] - 1e-9 for i in range(4)))
        return {"q_means": [round(float(v), 6) for v in qm],
                "spread_q4_minus_q0": round(float(qm[4] - qm[0]), 6),
                "monotone_asc": mono_up, "monotone_desc": mono_dn,
                "n_days": len(rows)}

    ag1, ag5 = _agg(ic1), _agg(ic5)
    qg1, qg5 = _qagg(q1_rows), _qagg(q5_rows)
    report = {
        "meta": {
            "script": "fusion_daily_validate.py",
            "mode": "read-only replay (不落生产持仓/盘口)",
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_start": series["as_of_start"],
            "as_of_end": series["as_of_end"],
            "days_scored": series["n_days"],
            "factors": ff.FACTOR_WEIGHTS,
            "directions": ff.DIRECTIONS,
            "pool_rule": "active(sz/sh symbols.parquet) + 当日有bar + |change_pct|<=9.5",
            "neutralization": "逐日 Winsorize 1%/99% -> OLS残差(ln_size+一级行业哑变量,"
                              "缺失归'未知') -> z; 合成权重线性后整截面再z",
            "pit_rule": "financials 报告期披露日映射(年报次年04-30/Q1 04-30/中报08-31/"
                        "Q3 10-31)取 as_of 时最新可见; valuation ts<=as_of 最近一期",
            "fwd_rule": "daily_bars.change_pct 连乘, 每股票自身后续 bar 行",
            "forward_ic_horizons": {"fwd1": "次日收益", "fwd5": "5日累计收益"},
            "data_limitation": ("valuation 全市场日频覆盖 2025-08 后降至 ~360 只跟踪池, "
                                "其余标的使用其最近可见(可能滞后约1年)估值, pb_inv/ep 存在滞后,"
                                "验证样本如实标注; 未来收益仅到数据最新日(2026-09-04), "
                                "窗口尾部 fwd1/fwd5 自然缺失"),
        },
        "coverage": {
            "avg_pool_n": round(float(np.mean(pool_ns)), 1) if pool_ns else None,
            "avg_scored_n": round(float(np.mean(scored_ns)), 1) if scored_ns else None,
            "avg_coverage": round(float(np.mean(covs)), 4) if covs else None,
            "min_coverage": round(float(np.min(covs)), 4) if covs else None,
        },
        "ic": {
            "fwd1": {"mean": _nan(ag1["mean"]), "std": _nan(ag1["std"]),
                     "icir": _nan(ag1["icir"]),
                     "win_rate_gt0": _nan(ag1["win_rate_gt0"]),
                     "n_days": ag1["n_days"],
                     "avg_n": round(float(np.mean(n1v)), 0) if n1v else None},
            "fwd5": {"mean": _nan(ag5["mean"]), "std": _nan(ag5["std"]),
                     "icir": _nan(ag5["icir"]),
                     "win_rate_gt0": _nan(ag5["win_rate_gt0"]),
                     "n_days": ag5["n_days"],
                     "avg_n": round(float(np.mean(n5v)), 0) if n5v else None},
        },
        "quintile": {"fwd1": qg1, "fwd5": qg5},
        "orthogonality": {   # 正交性只与因子/截面有关, 与持有期无关
            "mean_r_ln_size": _nan(np.mean(r_sz1) if r_sz1 else None),
            "mean_mean_abs_r_ind": _nan(np.mean(r_ind1) if r_ind1 else None),
            "n_days": len(r_sz1),
        },
        "per_day": per_day,
        "elapsed_s": round(time.time() - t0, 1),
    }
    return report


# ---------------------------------------------------------------------------
# fallback 决策自检 (monkeypatch, 不依赖外部 ml_fusion venv)
# ---------------------------------------------------------------------------
def selftest_fallback() -> dict:
    _LOG.info("自检: fusion_or_fml 决策分支 (monkeypatch)")
    env = dict(os.environ)
    out = {}

    def _items(n=6):
        return [{"canon": f"00{i:04d}.SZ"} for i in range(1, n + 1)]

    _codes = [f"00{i:04d}" for i in range(1, 7)]
    _canons = [f"00{i:04d}.SZ" for i in range(1, 7)]
    fake_ok = {"scores": {c: float(i + 1) for i, c in enumerate(_codes)},
               "n_scored": 5040, "n_pool": 5122, "coverage": 0.98}

    def _fake_css(subset=6):
        return {"scores": {c: float(i + 1) for i, c in enumerate(_codes[:subset])},
                "n_scored": 5040, "n_pool": 5122}

    def _fake_fml(base=10.0):
        return lambda canons, as_of: {
            "scores": {c: float(base + i) for i, c in enumerate(_canons)},
            "available": True}

    # 1) fusion 正常 + 覆盖100% -> fusion
    os.environ.pop("FORCE_FML", None); os.environ["FUSION_SCORE"] = "1"
    _orig_css, _orig_fml = ff.cross_section_scores, None
    ff.cross_section_scores = lambda as_of, symbols=None: dict(fake_ok)
    import ml_fusion_bridge as mb
    _orig_compute = mb.compute_fml
    mb.compute_fml = lambda canons, as_of: {"scores": {}, "available": False}
    try:
        _, used = ff.fusion_or_fml(_items(), "2026-09-03")
        out["fusion_ok_used"] = used
    finally:
        ff.cross_section_scores = _orig_css
        mb.compute_fml = _orig_compute

    # 2) fusion 覆盖 < 80% (6 只仅回 3 只) -> fml_fallback
    os.environ.pop("FORCE_FML", None); os.environ["FUSION_SCORE"] = "1"
    ff.cross_section_scores = lambda as_of, symbols=None: _fake_css(3)
    mb.compute_fml = _fake_fml(10.0)
    try:
        sc, used = ff.fusion_or_fml(_items(), "2026-09-03")
        out["low_cov_used"] = used
        out["low_cov_n"] = len(sc)
    finally:
        ff.cross_section_scores = _orig_css
        mb.compute_fml = _orig_compute

    # 3) fusion 异常 -> fml_fallback
    os.environ.pop("FORCE_FML", None); os.environ["FUSION_SCORE"] = "1"
    def _boom(as_of, symbols=None):
        raise RuntimeError("boom-test")
    ff.cross_section_scores = _boom
    mb.compute_fml = _fake_fml(20.0)
    try:
        sc, used = ff.fusion_or_fml(_items(), "2026-09-03")
        out["fusion_exc_used"] = used
        out["fusion_exc_n"] = len(sc)
    finally:
        ff.cross_section_scores = _orig_css
        mb.compute_fml = _orig_compute

    # 4) FORCE_FML=1 (融合被禁用) + f_ml 可用 -> fml_fallback
    os.environ["FORCE_FML"] = "1"
    mb.compute_fml = _fake_fml(30.0)
    try:
        sc, used = ff.fusion_or_fml(_items(), "2026-09-03")
        out["force_fml_used"] = used
        out["force_fml_n"] = len(sc)
    finally:
        ff.cross_section_scores = _orig_css
        mb.compute_fml = _orig_compute

    # 5) 两者皆空 -> equal
    os.environ.pop("FORCE_FML", None); os.environ["FUSION_SCORE"] = "1"
    ff.cross_section_scores = lambda as_of, symbols=None: {"scores": {}, "n_scored": 0}
    mb.compute_fml = lambda canons, as_of: {"scores": {}, "available": False}
    try:
        sc, used = ff.fusion_or_fml(_items(), "2026-09-03")
        out["both_empty_used"] = used
        out["both_empty_n"] = len(sc)
    finally:
        ff.cross_section_scores = _orig_css
        mb.compute_fml = _orig_compute

    os.environ.clear(); os.environ.update(env)
    ok = (out.get("fusion_ok_used") == "fusion"
          and out.get("low_cov_used") == "fml_fallback"
          and out.get("fusion_exc_used") == "fml_fallback"
          and out.get("force_fml_used") == "fml_fallback"
          and out.get("both_empty_used") == "equal")
    out["all_pass"] = bool(ok)
    _LOG.info("自检结果: %s", json.dumps(out, ensure_ascii=False))
    return out


# ---------------------------------------------------------------------------
# 引擎只读复放 (BacktestRunner days=6), 不触碰生产持仓/盘口
# ---------------------------------------------------------------------------
def replay_engine(days: int = 6) -> dict:
    bt_file = os.path.join(os.path.join(_BASE, "data"), "backtest_latest.json")
    backup = None
    if os.path.exists(bt_file):
        with open(bt_file, encoding="utf-8") as f:
            backup = f.read()
    try:
        from backtest_engine import BacktestRunner
        tag = "fusion_validate_6d"
        result = BacktestRunner(days=days).run(tag=tag)
        return {k: v for k, v in result.items() if k != "curve"}
    finally:
        if backup is not None:
            with open(bt_file, "w", encoding="utf-8") as f:
                f.write(backup)


def engine_advice() -> dict:
    return {
        "note": "引擎实际成交观察建议 (只读计算, 不注入)",
        "when_fusion_writes": ("target_weighting.ensure_target_weights fml 分支已接入 "
                               "factor_fusion.fusion_or_fml; 命中融合时每项会写 "
                               "fml(融合z) 与 fml_source='factor_fusion', 权重仍走 "
                               "allocate_target_weights 收缩+封顶."),
        "recommended_log_fields": [
            {"field": "fml_source", "expect": "factor_fusion|ml_fusion_bridge",
             "note": "缺失=该轮未写fml(等权回退)"},
            {"field": "fml", "note": "融合横截面z或旧f_ml预测"},
            {"field": "target_weight", "note": "收缩后目标权重, 和=1"},
            {"field": "fusion_coverage", "note": "请求标的覆盖率(>=80%才用融合)"},
            {"field": "fusion_n_scored", "note": "当日截面可出分股票数"},
        ],
        "fallback_alert": ("出现 used=fml_fallback (覆盖不足/异常/被禁用) 或 used=equal "
                           "(旧f_ml也不可用) 应记 WARN/告警并抄送运维."),
        "monitor_metrics": ["fwd5 日均IC(fusion)", "5分组Q4-Q0与单调性",
                            "融合分 vs ln_size 正交r", "覆盖率"],
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="滚动验证交易日数")
    ap.add_argument("--end", type=str, default=None, help="截止日 YYYY-MM-DD")
    ap.add_argument("--skip-replay", action="store_true", help="跳过 BacktestRunner 复放")
    a = ap.parse_args()

    report = run_daily_validate(days=a.days, end=a.end)
    report["fallback_selftest"] = selftest_fallback()
    report["engine_replay"] = (None if a.skip_replay
                               else replay_engine(days=6))
    report["engine_observation_advice"] = engine_advice()
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(report, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)

    _LOG.info("写入: %s", OUT_JSON)
    ic1 = report["ic"]["fwd1"]; ic5 = report["ic"]["fwd5"]
    print("\n===== 逐日验证摘要 =====")
    print(f"窗口: {report['meta']['as_of_start']} .. {report['meta']['as_of_end']} "
          f"({report['meta']['days_scored']}个交易日)")
    print(f"覆盖: 平均池 {report['coverage']['avg_pool_n']} / 出分 "
          f"{report['coverage']['avg_scored_n']} / 平均覆盖 "
          f"{report['coverage']['avg_coverage']}")
    print(f"fwd1 IC: 均值 {ic1['mean']}  ICIR {ic1['icir']}  胜率 {ic1['win_rate_gt0']}")
    print(f"fwd5 IC: 均值 {ic5['mean']}  ICIR {ic5['icir']}  胜率 {ic5['win_rate_gt0']}")
    q1 = report["quintile"]["fwd1"]; q5 = report["quintile"]["fwd5"]
    if q1:
        print("fwd1 五分组Q0..Q4:", q1["q_means"], "spread=", q1["spread_q4_minus_q0"],
              "mono_asc=", q1["monotone_asc"])
    if q5:
        print("fwd5 五分组Q0..Q4:", q5["q_means"], "spread=", q5["spread_q4_minus_q0"],
              "mono_asc=", q5["monotone_asc"])
    o = report["orthogonality"]
    print(f"正交性: 平均 |r(fused,ln_size)|={o['mean_r_ln_size']}  "
          f"平均 mean|r(industry)|={o['mean_mean_abs_r_ind']}")
    print("fallback 自检:", json.dumps(report["fallback_selftest"], ensure_ascii=False))
    er = report.get("engine_replay")
    if er:
        print(f"BacktestRunner(days=6): {er['start']}~{er['end']} 期末权益 "
              f"{er['final_equity']:,} ({er['total_return']:+.2f}%), 回撤 "
              f"{er['max_drawdown_pct']}%")
    else:
        print("engine_replay: skipped")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""m5 收口: 因子注册表 / 每日融合打分样本(parquet) / 滚动 IC-分组监控 / 收口报告
CLI:
  python factor_m5_close.py registry      # 重建统一注册表
  python factor_m5_close.py record 2026-09-04   # 记录单日样本(缺省=最近交易日)
  python factor_m5_close.py backfill 10   # 回补最近 N 个交易日样本
  python factor_m5_close.py status 60     # 滚动 IC/ICIR/分组(已实现收益日)
"""
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
_FC = os.path.join(_BASE, "data", "factor_mine")
_SD = os.path.join(_BASE, "data", "factor_fusion_samples")
os.makedirs(_SD, exist_ok=True)


def _latest_trading_day() -> str:
    from h5i_bar_store import H5iBarStore
    days = H5iBarStore().trading_days()
    return days[-1]


# ---------------- 1. 统一注册表 ----------------
def registry() -> dict:
    """聚合 m5 台账+中性化结论+融合权重为单一注册表 factor_registry.json."""
    import pandas as pd
    base = {
        "pb_inv": {"expr": "1/pb(close/bvps PIT)", "direction": "+1", "weight": 0.73, "role": "fusion"},
        "ep": {"expr": "1/pe_ttm", "direction": "+1", "weight": 0.7456, "role": "fusion"},
        "ocf_ps": {"expr": "每股经营现金流(最新可见报告期)", "direction": "+1", "weight": 0.546, "role": "fusion"},
        "roe_yy_chg": {"expr": "roe - 上年同期 roe", "direction": "-1 反转(降序做多)", "weight": 1.0618, "role": "fusion"},
        "ln_mcap": {"expr": "ln(close*float_shares)", "direction": "-1", "weight": 0, "role": "style_monitor"},
    }
    # 从中性化台账读取 fwd20 post 指标
    ledger = os.path.join(_FC, "ledger_m5_neutral.jsonl")
    post = {}
    if os.path.exists(ledger):
        for line in open(ledger, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            name = (r.get("factor") or r.get("name") or "").replace("_neu", "")
            post[name] = {k: r.get(k) for k in
                          ("rankic_post", "rank_ic_post", "ic_post", "icir_post", "verdict", "final_advice")
                          if r.get(k) is not None}
    for name, meta in base.items():
        if name in post:
            meta["post_metrics"] = post[name]
    # 融合验证与口径引用
    refs = {
        "monthly_panel": "data/h5i/factors/monthly_panel.parquet",
        "monthly_panel_neutral": "data/h5i/factors/monthly_panel_neutral.parquet",
        "daily_validate_v3": "data/factor_mine/fusion_daily_validate_v3.json",
        "value_pair_corr": "data/factor_mine/value_pair_corr.json",
        "neutralization_report": "data/factor_mine/neutralization_report.json",
        "m5_ledger": "data/factor_mine/ledger_m5_factors.jsonl",
        "neutral_ledger": "data/factor_mine/ledger_m5_neutral.jsonl",
    }
    # 加载基础因子库注册表 (factor_library.py)
    lib_factors = {}
    try:
        from factor_library import FACTOR_REGISTRY as FL_REG
        for name, meta in FL_REG.items():
            lib_factors[name] = {"category": meta["category"], "desc": meta["desc"]}
    except Exception as e:
        lib_factors = {"_error": f"factor_library 未加载: {e}"}

    out = {
        "version": "1.0", "updated_at": _now(),
        "fusion": {"method": "逐截面 Winsorize(1/99%) -> OLS残差(ln_size+一级行业) -> z -> 方向xICIR权重合成",
                   "factors": base,
                   "fallback": {"FUSION_SCORE": "默认1; 覆盖<80%/异常/空/FORCE_FML=1 -> 旧 ml_fusion(或等权)",
                                "source_fields": "fml_source/fml/fusion_coverage"}},
        "library": {
            "source": "factor_library.py",
            "n_factors": len(lib_factors),
            "factors": lib_factors,
            "categories": {
                "momentum": "价格动量类: ret_N, mom_fast_slow, rsi_14, kdj",
                "volatility": "波动率类: hist_vol_N, atr_14, max_drawdown_N",
                "volume": "量价结合类: volume_change_N, volume_ratio, turnover",
                "fundamental": "基本面类: ep, bp, roe, roe_yy_chg, rev_yoy, np_yoy",
            },
        },
        "monitor": {"samples_dir": _SD, "record_step": "run_daily 收盘步骤 fusion_sample_record"},
        "references": refs,
    }
    p = os.path.join(_FC, "factor_registry.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return out


# ---------------- 2. 每日样本记录 ----------------
def record_day(day: str | None = None) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq
    day = day or _latest_trading_day()
    fp = os.path.join(_SD, f"{day}.parquet")
    if os.path.exists(fp):
        return {"ok": True, "day": day, "exists": True, "appended": 0}
    from factor_fusion import cross_section_scores
    r = cross_section_scores(day)
    sc = r.get("scores") or {}
    import pandas as pd
    df = pd.DataFrame([{"symbol": k, "fused_z": v} for k, v in sc.items()])
    if df.empty:
        return {"ok": False, "day": day, "error": "no scores"}
    # 附 ln_size 供正交监控(close*float_shares 由 fusion meta 暂不导出, 留空由 status 计算? 简化: 记为 NA)
    df["as_of"] = day
    df = df.sort_values("symbol").reset_index(drop=True)
    sch = pa.schema([("symbol", pa.string()), ("fused_z", pa.float64()), ("as_of", pa.string())])
    pq.write_table(pa.Table.from_pandas(df, schema=sch, preserve_index=False), fp)
    return {"ok": True, "day": day, "exists": False, "rows": int(len(df)),
            "coverage": r.get("coverage"), "n_pool": r.get("n_pool"), "n_scored": r.get("n_scored")}


def backfill(n: int = 10) -> dict:
    from h5i_bar_store import H5iBarStore
    days = H5iBarStore().trading_days()[-n:]
    out = {"days": [], "ok_count": 0}
    for d in days:
        r = record_day(d)
        out["days"].append(r)
        out["ok_count"] += int(r.get("ok"))
    return out


# ---------------- 3. 滚动监控 ----------------
def _fwd_returns(symbol: str, d: str, horizon: int):
    """用 daily_bars change_pct 连乘的 fwd 收益(次日即日起 horizon 日)."""
    from h5i_bar_store import H5iBarStore
    df = H5iBarStore().bars(symbol, start=d)
    if df is None or len(df) == 0:
        return None
    sub = df[df["d"].astype(str) >= d]
    if len(sub) <= 1:
        return None
    vals = sub["change_pct"].astype(float).fillna(0).iloc[1:horizon + 1].values
    if len(vals) == 0:
        return None
    import numpy as np
    return float(np.prod(1 + vals / 100.0) - 1)


def status(days: int = 60, horizon: int = 5) -> dict:
    """对已实现收益的样本日计算 IC 序列/ICIR/五分组, 写 summary + 返回."""
    import glob
    import numpy as np
    files = sorted(glob.glob(os.path.join(_SD, "*.parquet")))
    ics = {}
    groups = []
    for fp in files[-days:]:
        import pandas as pd
        df = pd.read_parquet(fp)
        day = str(df["as_of"].iloc[0])
        df = df.drop_duplicates("symbol")
        ret = {}
        for sym in df["symbol"].tolist()[:None]:
            pass
        # 实现收益(逐票查 bars, 限前200只避免过重)
        rows = []
        for _, r in df.head(400).iterrows():
            rv = _fwd_returns(r["symbol"], day, horizon)
            if rv is not None:
                rows.append((r["fused_z"], rv))
        if len(rows) < 100:
            continue
        z, rv = zip(*rows)
        z = np.asarray(z, float); rv = np.asarray(rv, float)
        from scipy.stats import spearmanr, rankdata
        ic = spearmanr(z, rv)[0]
        if not np.isfinite(ic):
            continue
        ics[day] = float(ic)
        q = pd.qcut(rankdata(z), 5, labels=False)
        gm = pd.Series(rv).groupby(q).mean()
        groups.append({"day": day, "q": [round(float(v), 6) for v in gm.values]})
    s = np.asarray(list(ics.values()))
    rep = {
        "n_days_matured": int(len(s)),
        "days": list(ics.keys()),
        "daily_ic_mean": round(float(s.mean()), 4) if len(s) else None,
        "icir": round(float(s.mean() / s.std()), 4) if len(s) and s.std() else None,
        "win": round(float((s > 0).mean()), 4) if len(s) else None,
        "group_mean_by_day": groups[-10:],
        "horizon": horizon,
    }
    json.dump(rep, open(os.path.join(_SD, "summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return rep


def closure_report() -> dict:
    """收口报告: 引用全部 m5 交付物 + 运行手册."""
    reg = os.path.join(_FC, "factor_registry.json")
    if os.path.exists(reg):
        rr = json.load(open(reg, encoding="utf-8"))
    else:
        rr = registry()
    summ = os.path.join(_SD, "summary.json")
    st = json.load(open(summ, encoding="utf-8")) if os.path.exists(summ) else {}
    out = {
        "title": "m5 重建因子收口报告",
        "updated_at": _now(),
        "registry": reg,
        "sample_dir": _SD,
        "rolling_status": st,
        "runbook": [
            "python factor_m5_close.py registry    # 重建统一注册表",
            "python factor_m5_close.py backfill 10 # 回补最近N个交易日打分样本",
            "python factor_m5_close.py status 60   # 滚动 IC/ICIR/分组",
        ],
        "integration": "target_weighting.ensure_target_weights / selector._blend_fml 默认 FUSION_SCORE=1; "
                       "fml_source=factor_fusion; 覆盖<80%自动回退旧 ml_fusion 或等权",
    }
    p = os.path.join(_FC, "m5_closure.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return out


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "registry"
    if cmd == "registry":
        r = registry(); print("registry ok", r["version"], len(r["fusion"]["factors"]))
    elif cmd == "record":
        r = record_day(sys.argv[2] if len(sys.argv) > 2 else None)
        print(json.dumps(r, ensure_ascii=False))
    elif cmd == "backfill":
        r = backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
        print("ok_count", r["ok_count"], "of", len(r["days"]))
    elif cmd == "status":
        r = status(int(sys.argv[2]) if len(sys.argv) > 2 else 60)
        print(json.dumps({k: r[k] for k in ("n_days_matured", "daily_ic_mean", "icir", "win") if k in r},
                         ensure_ascii=False))
    elif cmd == "closure":
        r = closure_report(); print(json.dumps({k: r[k] for k in ("title", "updated_at")}, ensure_ascii=False))
    else:
        print("usage: registry|record [day]|backfill [n]|status [days]|closure")

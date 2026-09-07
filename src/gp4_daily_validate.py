# -*- coding: utf-8 -*-
# ============================================================
# gp4_daily_validate.py -- 观察候选 gp4 日频验证/每日记录/滚动监控 (只读复放)
#
# gp4 = (|rev_yoy_neu| - (np_yoy_neu + roe_yy_chg_neu)) / (|roe_neu| + sqrt(bvps_neu))
#   (GP 月频挖掘候选, 表达式/域见 factor_fusion.GP4_WATCH)
#
# 用法:
#   python gp4_daily_validate.py [--days 60]        # 历史窗口回放验证 -> gp4_daily_validate.json
#   python gp4_daily_validate.py record [day]       # 记录单日 gp4 截面样本(缺省=最近交易日)
#   python gp4_daily_validate.py status [days] [h]  # 对已实现收益样本日滚动 IC/ICIR/分组
#   python gp4_daily_validate.py alert [w] [rq]     # 提级门槛判定(连续rq日达标->GP4_READY)
#   python gp4_daily_validate.py ack                # 人工处理确认, 复位 pending_alert
# 其中 alert 亦作为 run_daily 收尾步骤 1.0990 自动调用。
#
# 日频口径与 fusion_daily_validate 同构:
#   - 每个 as_of 交易日 5 分量各自 Winsorize1/99 -> OLS残差(ln_size+行业) -> z
#   - 代入公式得当日 gp4 分 (bvps_z<0 / 非有限 -> 无分, 与月频评估一致)
#   - fwd1/fwd5 RankIC / 五分位 / 覆盖; 同窗 fusion 分 IC 作参照
# 样本落盘: data/factor_gp4_samples/{day}.parquet (symbol/gp4_raw/as_of)
# ============================================================
from __future__ import annotations

import glob
import json
import logging
import os
import sys
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
os.chdir(_BASE)

import factor_fusion as ff  # noqa: E402
import fusion_daily_validate as fdv  # noqa: E402  (复用 _ic_day/_quintile)

OUT_JSON = os.path.join(_BASE, "data", "factor_mine", "gp4_daily_validate.json")
SAMPLE_DIR = os.path.join(_BASE, "data", "factor_gp4_samples")
os.makedirs(SAMPLE_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("gp4_daily_validate")


def _agg(ics: list[float]) -> dict:
    if not ics:
        return {"mean": None, "std": None, "icir": None,
                "win_rate_gt0": None, "n_days": 0}
    a = np.asarray(ics, dtype=float)
    sd = a.std(ddof=1) if len(a) > 1 else 0.0
    return {"mean": round(float(a.mean()), 5),
            "std": (round(float(sd), 5) if sd > 1e-12 else 0.0),
            "icir": (round(float(a.mean() / sd), 3) if sd > 1e-12 else None),
            "win_rate_gt0": round(float((a > 0).mean()), 3),
            "n_days": len(a)}


def _qagg(rows):
    if not rows:
        return None
    a = np.asarray(rows, dtype=float)
    qm = a.mean(axis=0)
    return {"q_means": [round(float(v), 6) for v in qm],
            "spread_q4_minus_q0": round(float(qm[4] - qm[0]), 6),
            "monotone_asc": bool(all(qm[i] <= qm[i + 1] + 1e-9 for i in range(4))),
            "n_days": len(rows)}


# ---------------- 每日样本持久化 ----------------
def _write_sample(day: str, gp4_sc: dict) -> dict:
    fp = os.path.join(SAMPLE_DIR, f"{day}.parquet")
    if os.path.exists(fp):
        return {"ok": True, "day": day, "exists": True, "rows": 0}
    if not gp4_sc:
        return {"ok": True, "day": day, "exists": False, "rows": 0, "note": "当日无 gp4 分"}
    df = pd.DataFrame([{"symbol": s, "gp4_raw": v, "as_of": day}
                       for s, v in gp4_sc.items()])
    df = df.sort_values("symbol").reset_index(drop=True)
    sch = pa.schema([("symbol", pa.string()), ("gp4_raw", pa.float64()),
                     ("as_of", pa.string())])
    pq.write_table(pa.Table.from_pandas(df, schema=sch, preserve_index=False), fp)
    return {"ok": True, "day": day, "exists": False, "rows": int(len(df))}


def record_day_gp4(day: str | None = None) -> dict:
    """单日记录: 缺省 as_of = h5i 最近交易日 (供 run_daily 收盘钩子)."""
    from h5i_bar_store import H5iBarStore
    day = day or H5iBarStore().trading_days()[-1]
    df = ff.snapshot_day_df(day)
    if df is None or len(df) == 0:
        return {"ok": False, "day": day, "error": "当日无截面(非交易日?)"}
    sc, meta = {}, {}
    try:
        sc, meta = ff.gp4_watch_scores(df)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "day": day, "error": f"{type(e).__name__}: {e}"}
    wr = _write_sample(day, sc)
    return {"ok": wr["ok"], "day": day, "n_pool": int(len(df)),
            "defined_n": len(sc), "meta": meta, "write": wr}


def backfill_gp4(days: int = 60) -> dict:
    """回补最近 N 个交易日样本 (逐日 snapshot_day_df + record, 幂等)."""
    from h5i_bar_store import H5iBarStore
    trading = H5iBarStore().trading_days()[-days:]
    out, ok = [], 0
    for d in trading:
        r = record_day_gp4(d)
        out.append(r)
        ok += int(r.get("ok", 0) and r.get("write", {}).get("rows", 0) > 0)
    return {"days": len(trading), "days_with_new_sample": ok, "detail": out[-3:]}


# ---------------- 历史窗口回放验证 ----------------
def run(days: int = 60, end: str | None = None) -> dict:
    t0 = time.time()
    series = ff.score_series_hist(end, days=days)
    if "error" in series:
        raise SystemExit(f"score_series_hist 失败: {series['error']}")
    samples = series["samples"]
    _LOG.info("逐日出分 %s..%s n=%d", series["as_of_start"],
              series["as_of_end"], series["n_days"])

    per_day = []
    ic1, ic5 = [], []
    fic1, fic5 = [], []
    q1r, q5r = [], []
    defs, pools, covs = [], [], []
    writes = []

    for row in samples:
        gp = row.get("gp4_scores") or {}
        gm = row.get("gp4_meta") or {}
        writes.append(_write_sample(row["date"], gp))
        pool = row.get("n_pool", 0)
        rec = {"date": row["date"], "n_pool": pool, "gp4_defined": len(gp),
               "gp4_coverage": (round(len(gp) / pool, 4) if pool else None)}
        if gm.get("component_n"):
            rec["component_n"] = gm["component_n"]
        if gm.get("n_drop"):
            rec["n_drop"] = gm["n_drop"]

        r1 = fdv._ic_day(gp, row["fwd1"])
        r5 = fdv._ic_day(gp, row["fwd5"])
        rec["gp4_ic_fwd1"] = (round(r1["ic"], 5) if r1["ic"] is not None else None)
        rec["gp4_ic_fwd5"] = (round(r5["ic"], 5) if r5["ic"] is not None else None)
        if r1["ic"] is not None:
            ic1.append(r1["ic"])
        if r5["ic"] is not None:
            ic5.append(r5["ic"])

        fr1 = fdv._ic_day(row.get("scores") or {}, row["fwd1"])
        fr5 = fdv._ic_day(row.get("scores") or {}, row["fwd5"])
        rec["fusion_ic_fwd1"] = (round(fr1["ic"], 5) if fr1["ic"] is not None else None)
        rec["fusion_ic_fwd5"] = (round(fr5["ic"], 5) if fr5["ic"] is not None else None)
        if fr1["ic"] is not None:
            fic1.append(fr1["ic"])
        if fr5["ic"] is not None:
            fic5.append(fr5["ic"])

        q1 = fdv._quintile(gp, row["fwd1"])
        q5 = fdv._quintile(gp, row["fwd5"])
        if q1:
            q1r.append(q1)
            rec["gp4_q_fwd1"] = [round(v, 6) for v in q1]
        if q5:
            q5r.append(q5)
            rec["gp4_q_fwd5"] = [round(v, 6) for v in q5]

        defs.append(len(gp))
        pools.append(pool)
        if pool:
            covs.append(len(gp) / pool)
        per_day.append(rec)

    return {
        "meta": {
            "script": "gp4_daily_validate.py",
            "mode": "read-only replay (观察候选 gp4, 不影响融合权重/生产)",
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_start": series["as_of_start"],
            "as_of_end": series["as_of_end"],
            "days_scored": series["n_days"],
            "factor": ff.GP4_WATCH["expr_zh"],
            "components": ff.GP4_WATCH["components"],
            "domain": "bvps_z<0 处 sqrt 无定义, 与月频面板评估同口径; "
                      "观察口径明确计入 defined/pool 覆盖",
            "fwd_rule": "daily_bars.change_pct 连乘 (同 fusion_daily_validate)",
        },
        "gp4_ic": {"fwd1": _agg(ic1), "fwd5": _agg(ic5)},
        "fusion_ref_ic": {"fwd1": _agg(fic1), "fwd5": _agg(fic5)},
        "gp4_quintile": {"fwd1": _qagg(q1r), "fwd5": _qagg(q5r)},
        "coverage": {
            "avg_pool_n": (round(float(np.mean(pools)), 1) if pools else None),
            "avg_gp4_defined": (round(float(np.mean(defs)), 1) if defs else None),
            "avg_coverage": (round(float(np.mean(covs)), 4) if covs else None),
            "min_coverage": (round(float(np.min(covs)), 4) if covs else None),
            "days_with_zero_gp4": int(sum(1 for d in defs if d == 0)),
        },
        "sample_writes": writes,
        "per_day": per_day,
        "elapsed_s": round(time.time() - t0, 1),
    }


# ---------------- 滚动状态 (样本日已实现收益) ----------------
def status_gp4(days: int = 60, horizon: int = 5) -> dict:
    files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.parquet")))[-days:]
    if not files:
        return {"n_days_matured": 0, "error": "no sample files"}
    first_day = os.path.basename(files[0]).replace(".parquet", "")
    last_day = os.path.basename(files[-1]).replace(".parquet", "")
    # bars 窗口向后放宽 horizon+2 个交易日以便 fwd 对齐
    cal = ff._calendar()
    if last_day not in {d.strftime("%Y-%m-%d") for d in cal}:
        return {"n_days_matured": 0, "error": "last sample day not in calendar"}
    idx = [i for i, d in enumerate(cal) if d.strftime("%Y-%m-%d") == last_day][0]
    hi = cal[min(idx + horizon + 3, len(cal) - 1)]
    bars = ff._sql("SELECT CAST(ts AS DATE) d, symbol, change_pct FROM daily_bars "
                   f"WHERE CAST(ts AS DATE) >= DATE '{first_day}' "
                   f"AND CAST(ts AS DATE) <= DATE '{hi.strftime('%Y-%m-%d')}'")
    bars["d"] = pd.to_datetime(bars["d"])
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars = bars.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    r1 = (1.0 + bars["change_pct"].astype(float) / 100.0).to_numpy()
    bars = bars.assign(r1=r1)
    g = bars.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd = g.cumprod().groupby(bars["symbol"], sort=False).shift(-horizon) / cump - 1.0
    bars["fwd"] = np.where(np.isfinite(fwd), fwd, np.nan)

    ics, groups = [], []
    for fp in files:
        df = pd.read_parquet(fp)
        day = str(df["as_of"].iloc[0])
        df = df.drop_duplicates("symbol")
        h = bars[(bars["d"] == pd.Timestamp(day)) & (bars["symbol"].isin(df["symbol"]))]
        hm = h.set_index("symbol")
        pts = []
        for _, r in df.iterrows():
            fv = hm["fwd"].get(r["symbol"])
            if pd.notna(fv):
                pts.append((r["gp4_raw"], float(fv)))
        if len(pts) < 100:
            continue
        z, rv = zip(*pts)
        z = np.asarray(z, float)
        rv = np.asarray(rv, float)
        from scipy.stats import spearmanr, rankdata
        ic = spearmanr(z, rv)[0]
        if not np.isfinite(ic):
            continue
        ics.append(ic)
        q = pd.qcut(rankdata(z), 5, labels=False)
        gm = pd.Series(rv).groupby(q).mean()
        groups.append({"day": day, "q": [round(float(v), 6) for v in gm.values]})
    s = np.asarray(ics, float)
    rep = {
        "horizon": horizon,
        "n_days_matured": int(len(s)),
        "days": [fp.split(os.sep)[-1].replace(".parquet", "") for fp in files],
        "daily_ic_mean": round(float(s.mean()), 5) if len(s) else None,
        "icir": (round(float(s.mean() / s.std()), 4) if len(s) and s.std() > 0 else None),
        "win": round(float((s > 0).mean()), 4) if len(s) else None,
        "group_mean_by_day": groups[-10:],
        "sample_dir": SAMPLE_DIR,
    }
    json.dump(rep, open(os.path.join(SAMPLE_DIR, "summary_gp4.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    return rep


# ---------------- 提级门槛收尾告警 (run_daily 1.0990) ----------------
_ALERT_STATE_FP = os.path.join(SAMPLE_DIR, "gp4_alert_state.json")
_REG_FP = os.path.join(_BASE, "data", "factor_mine", "factor_registry.json")
_ALERT_CFG = {"window_valid_days": 20, "ic_threshold": 0.0,
              "win_threshold": 0.55, "require_consecutive": 3}


def _load_alert_state() -> dict:
    if os.path.exists(_ALERT_STATE_FP):
        try:
            st = json.load(open(_ALERT_STATE_FP, encoding="utf-8"))
            if isinstance(st, dict):
                return st
        except Exception:  # noqa: BLE001
            pass
    return {"pending_alert": False, "pass_streak": 0, "last_matured_day": None,
            "last_check": None, "last_alert_at": None,
            "fwd5_ic_mean": None, "win_rate": None}


def _save_alert_state(st: dict) -> None:
    json.dump(st, open(_ALERT_STATE_FP, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _sync_registry(st: dict, alert_level: str = "INFO",
                   fired: bool = False, msg: str = "") -> None:
    """pending_alert + 最近判定数值写入 factor_registry gp4_watch 条目 (只改该节点)."""
    import datetime as _dt
    reg = (json.load(open(_REG_FP, encoding="utf-8"))
           if os.path.exists(_REG_FP) else {})
    node = (reg.setdefault("mining", {})
              .setdefault("gp_deep_mine", {})
              .setdefault("watch_candidates", {})
              .setdefault("gp4", {}))
    node["pending_alert"] = bool(st.get("pending_alert"))
    node["promotion_alert"] = {
        "require_consecutive": _ALERT_CFG["require_consecutive"],
        "window_valid_days": _ALERT_CFG["window_valid_days"],
        "ic_threshold": _ALERT_CFG["ic_threshold"],
        "win_threshold": _ALERT_CFG["win_threshold"],
        "last_check": st.get("last_check"),
        "last_matured_day": st.get("last_matured_day"),
        "fwd5_ic_mean": st.get("fwd5_ic_mean"),
        "win_rate": st.get("win_rate"),
        "pass_streak": int(st.get("pass_streak", 0)),
        "last_alert_at": st.get("last_alert_at"),
        "last_alert_msg": (msg if fired else None),
        "level": alert_level,
    }
    reg["updated_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json.dump(reg, open(_REG_FP, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def _matured_valid_days(days_cap: int = 40, horizon: int = 5) -> list[dict]:
    """最近 days_cap 个样本文件中的"已实现收益有效日"(有 gp4 分覆盖且可算 IC).
    返回按日升序的 [{"day","ic","n"}], 供提级判定取尾窗."""
    files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.parquet")))[-days_cap:]
    if not files:
        return []
    first_day = os.path.basename(files[0])[:10]
    last_day = os.path.basename(files[-1])[:10]
    cal = ff._calendar()
    days = [d.strftime("%Y-%m-%d") for d in cal]
    if last_day not in days:
        return []
    idx = days.index(last_day)
    hi = cal[min(idx + horizon + 3, len(cal) - 1)]
    bars = ff._sql("SELECT CAST(ts AS DATE) d, symbol, change_pct FROM daily_bars "
                   f"WHERE CAST(ts AS DATE) >= DATE '{first_day}' "
                   f"AND CAST(ts AS DATE) <= DATE '{hi.strftime('%Y-%m-%d')}'")
    bars["d"] = pd.to_datetime(bars["d"])
    bars["symbol"] = bars["symbol"].astype(str).str.zfill(6)
    bars = bars.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    r1 = (1.0 + bars["change_pct"].astype(float) / 100.0).to_numpy()
    bars = bars.assign(r1=r1)
    g = bars.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd = g.cumprod().groupby(bars["symbol"], sort=False).shift(-horizon) / cump - 1.0
    bars["fwd"] = np.where(np.isfinite(fwd), fwd, np.nan)

    from scipy.stats import spearmanr
    out = []
    for fp in files:
        df = pd.read_parquet(fp)
        day = str(df["as_of"].iloc[0])
        df = df.drop_duplicates("symbol")
        h = bars[(bars["d"] == pd.Timestamp(day)) & (bars["symbol"].isin(df["symbol"]))]
        hm = h.set_index("symbol")
        z, rv = [], []
        for _, r in df.iterrows():
            fv = hm["fwd"].get(r["symbol"])
            if pd.notna(fv):
                z.append(float(r["gp4_raw"]))
                rv.append(float(fv))
        if len(z) < 100:
            continue
        ic = spearmanr(z, rv)[0]
        if not np.isfinite(ic):
            continue
        out.append({"day": day, "ic": float(ic), "n": len(z)})
    return out


def promotion_alert_check(window: int | None = None, horizon: int = 5,
                          require_consecutive: int | None = None) -> dict:
    """gp4 提级门槛判定 (run_daily 1.0990 收尾调用).

    规则:
      - 取最近 window(=20) 个"有分覆盖"的已实现收益样本日
      - fwd5 IC 均值 > ic_threshold 且 胜率 > win_threshold 计 1 个连续达标日
      - 连续达标 >= require_consecutive(=3) 且 pending_alert=False -> GP4_READY
      - 触发后置 pending_alert=True (registry gp4_watch), 防重复告警;
        人工 ack 处理或指标转负时自动复位, 允许再次达标后重新告警.
      - 无新成熟样本日(如非交易日/重复运行)只刷 INFO, 不累计达标天数.
    """
    import datetime as _dt
    window = window or _ALERT_CFG["window_valid_days"]
    require_consecutive = (require_consecutive
                           if require_consecutive is not None
                           else _ALERT_CFG["require_consecutive"])
    st = _load_alert_state()
    matured = _matured_valid_days(days_cap=max(2 * window, 40), horizon=horizon)
    if not matured:
        _save_alert_state(st)
        return {"level": "INFO", "alert": False, "msg": "尚无已实现收益样本日",
                "pending_alert": st.get("pending_alert"), "pass_streak": 0}
    win = matured[-window:]
    fwd5_ic_mean = float(np.mean([x["ic"] for x in win]))
    win_rate = float(np.mean([x["ic"] > 0 for x in win]))
    last_day = win[-1]["day"]
    new_data = (st.get("last_matured_day") != last_day)
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["last_check"] = now
    st["last_matured_day"] = last_day
    st["fwd5_ic_mean"] = round(fwd5_ic_mean, 5)
    st["win_rate"] = round(win_rate, 4)

    passing = (fwd5_ic_mean > _ALERT_CFG["ic_threshold"]
               and win_rate > _ALERT_CFG["win_threshold"])
    if not new_data:
        # 无新成熟样本日: 只记录数值, 不累计达标天数 (防非交易日重复计数)
        _save_alert_state(st)
        _sync_registry(st, "INFO")
        return {"level": "INFO", "alert": False,
                "msg": f"无新成熟样本日(last={last_day}), 保持状态; "
                       f"fwd5_ic={st['fwd5_ic_mean']} win={st['win_rate']}",
                "fwd5_ic_mean": st["fwd5_ic_mean"], "win_rate": st["win_rate"],
                "n_valid_days": len(matured), "window": len(win),
                "pass_streak": int(st.get("pass_streak", 0)),
                "require_consecutive": require_consecutive,
                "pending_alert": st.get("pending_alert")}

    if passing:
        st["pass_streak"] = int(st.get("pass_streak", 0)) + 1
    else:
        st["pass_streak"] = 0
        if st.get("pending_alert"):
            _LOG.warning("gp4 提级指标转负 -> pending_alert 自动复位, 允许后续重新告警")
            st["pending_alert"] = False

    fired = (st["pass_streak"] >= require_consecutive
             and not st.get("pending_alert"))
    level = "GP4_READY" if fired else "INFO"
    if fired:
        st["pending_alert"] = True
        st["last_alert_at"] = now
        msg = "gp4 已达提级标准，请人工评估"
        _LOG.warning("[%s] %s (fwd5_ic=%.5f win=%.2f streak=%d)",
                     level, msg, fwd5_ic_mean, win_rate, st["pass_streak"])
    else:
        msg = (f"gp4 连续达标 {st['pass_streak']}/{require_consecutive} "
               f"(pending={st.get('pending_alert')})" if passing
               else f"gp4 未达标 (pass_streak={st['pass_streak']})")
        _LOG.info("[INFO] %s fwd5_ic=%.5f win=%.2f", msg, fwd5_ic_mean, win_rate)
    _save_alert_state(st)
    _sync_registry(st, level, fired, msg if fired else "")
    return {"level": level, "alert": fired, "msg": msg,
            "fwd5_ic_mean": st["fwd5_ic_mean"], "win_rate": st["win_rate"],
            "n_valid_days": len(matured), "window": len(win),
            "pass_streak": int(st["pass_streak"]),
            "require_consecutive": require_consecutive,
            "pending_alert": st.get("pending_alert")}


def ack_promotion() -> dict:
    """人工处理确认: 复位 pending_alert (提级或决定不提级后调用, 避免重复告警)."""
    import datetime as _dt
    st = _load_alert_state()
    st["pending_alert"] = False
    st["acknowledged_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_alert_state(st)
    _sync_registry(st, "ACK")
    _LOG.info("gp4 提级告警已人工 ack (pending_alert=False)")
    return {"ok": True, "msg": "gp4 pending_alert 已复位, 后续达标可再次触发告警",
            "acknowledged_at": st["acknowledged_at"]}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "run"
    if cmd == "run":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--days", type=int, default=60)
        ap.add_argument("--end", type=str, default=None)
        a = ap.parse_args(sys.argv[1:])
        rep = run(days=a.days, end=a.end)
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        json.dump(rep, open(OUT_JSON, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2, default=str)
        _LOG.info("写入: %s", OUT_JSON)
        print("\n===== gp4 日频验证摘要 =====")
        print(f"窗口: {rep['meta']['as_of_start']} .. {rep['meta']['as_of_end']} "
              f"({rep['meta']['days_scored']}个交易日)")
        g4 = rep["gp4_ic"]; fu = rep["fusion_ref_ic"]
        print(f"[gp4]    fwd1 IC {g4['fwd1']['mean']} ICIR {g4['fwd1']['icir']} "
              f"胜率 {g4['fwd1']['win_rate_gt0']} | fwd5 IC {g4['fwd5']['mean']} "
              f"ICIR {g4['fwd5']['icir']} 胜率 {g4['fwd5']['win_rate_gt0']}")
        print(f"[fusion] fwd1 IC {fu['fwd1']['mean']} | fwd5 IC {fu['fwd5']['mean']} "
              f"(同窗参照)")
        c = rep["coverage"]
        print(f"覆盖: 池均 {c['avg_pool_n']} / gp4有分均 {c['avg_gp4_defined']} / "
              f"平均覆盖 {c['avg_coverage']} (0分日 {c['days_with_zero_gp4']})")
        q = rep["gp4_quintile"]["fwd5"]
        if q:
            print("gp4 fwd5 五分组Q0..Q4:", q["q_means"], "spread=",
                  q["spread_q4_minus_q0"], "mono_asc=", q["monotone_asc"])
    elif cmd == "record":
        day = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(record_day_gp4(day), ensure_ascii=False, indent=1, default=str))
    elif cmd == "backfill":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        print(json.dumps(backfill_gp4(n), ensure_ascii=False, indent=1, default=str))
    elif cmd == "status":
        d = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        h = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(json.dumps({k: status_gp4(d, h)[k] for k in
                          ("n_days_matured", "daily_ic_mean", "icir", "win")},
                         ensure_ascii=False, default=str))
    elif cmd == "alert":
        w = int(sys.argv[2]) if len(sys.argv) > 2 else None
        rq = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(json.dumps(promotion_alert_check(window=w, require_consecutive=rq),
                         ensure_ascii=False, indent=1, default=str))
    elif cmd == "ack":
        print(json.dumps(ack_promotion(), ensure_ascii=False, indent=1, default=str))
    else:
        print("usage: [run|record [day]|backfill [n]|status [days] [horizon]|"
              "alert [window] [require]|ack]")


if __name__ == "__main__":
    main()

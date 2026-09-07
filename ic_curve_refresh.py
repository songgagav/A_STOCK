# -*- coding: utf-8 -*-
# ============================================================
# ic_curve_refresh.py -- 打通 weight_optimizer 的实时 ICIR 依据
#
# 背景/根因 (点3):
#   weight_optimizer.optimize_weights() 读 data/ic/ic_curve_<f>_k20.csv 的 ic_h5
#   (默认 H5 — vol 在 H5 IC=0.12 比 H20 IC=0.05 强 2.4 倍)
#   取近期 window=40 个样点算 ICIR 来自适应因子权重。但 ic_curve 是 ic_backtest
#   的"全历史重建"产物, 全历史重建一次约 3.2 分钟, daily 管道从不调用, 导致
#   ICIR 依据长期冻结在最近一次重建日。
#
# 本模块提供两级刷新, 兼顾"实时"与"正确性":
#   * refresh(full=False)  增量刷新: 仅重算最近 WINDOW_DAYS 个交易日, 与既有
#     ic_curve 历史 **合并** (不整文件覆盖, 保留窗口前历史), 开销 ~2s.
#     -> 挂 daily 管道 full 模式, 每天收盘后让 ic_h20 追平到最新已结算交易日.
#   * refresh(full=True)   全量重建: 调用 ic_backtest.run 重建全历史, 修正上市公司
#     更替/因子口径漂移. 开销 ~3.2min. -> 挂 maint 模式(周末/节假日)低频执行.
#
# 关键: ic_backtest.run 总是覆盖写整个文件, 这里由本模块负责"增量=重算近端+
# 合并存底+回写", 绝不裸调 ic_backtest.run 去覆盖全历史文件。
# ============================================================
import os
import sys
import argparse
import datetime

import numpy as np
import pandas as pd

from config import DUCKDB_PATH, DATA_DIR

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ic_backtest

IC_DIR = os.path.join(DATA_DIR, "ic")
K = 20
HOLDS = [1, 3, 5, 10, 20]          # ic_backtest 生成的全部持有期列
# weight_optimizer 默认依赖 ic_h5; window>40 但留裕量, 保证 tail(40) 始终是最新结算样本
WINDOW_DAYS = 75                    # 增量时重算的交易日天数
# 与 weight_optimizer.ALPHA_FACTORS 保持一致: {权重键: ic_curve 文件名因}
ALPHA_FACTORS = {"vol": "vol", "mom_rev": "mom_20"}


def _latest_settled_day():
    """daily_bars 最新交易日(用于 ic_h20 需要未来20日的收益, 实际可用样本会更早)。"""
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        row = con.execute("SELECT MAX(date) FROM daily_bars").fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _window_start(latest, days=WINDOW_DAYS):
    """增量区间的起始日: 往前数 days 个(去重)交易日。缺 GROUP BY 时直接 MIN 会被
    同一日期的高频行淹没, 故子查询先 GROUP BY 成"每交易日一行"再取倒数第 days 个。"""
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        rows = con.execute("""
            SELECT MIN(date) FROM (
                SELECT date FROM daily_bars WHERE change_pct IS NOT NULL
                 AND date <= ? GROUP BY date ORDER BY date DESC LIMIT ?
            )
        """, [latest, days]).fetchone()
    finally:
        con.close()
    return rows[0] if rows and rows[0] else None


def _read_curve(factor):
    """读既有 ic_curve_<factor>_k20.csv; 返回 DataFrame(day str), 不存在则空表。"""
    p = os.path.join(IC_DIR, "ic_curve_{}_k{}.csv".format(factor, K))
    if not os.path.exists(p):
        return pd.DataFrame(columns=["day"] + ["ic_h{}".format(h) for h in HOLDS])
    try:
        df = pd.read_csv(p, dtype={"day": str})
        for h in HOLDS:
            col = "ic_h{}".format(h)
            if col not in df.columns:
                df[col] = np.nan
        return df
    except Exception:
        return pd.DataFrame(columns=["day"] + ["ic_h{}".format(h) for h in HOLDS])


def _write_curve(factor, df):
    os.makedirs(IC_DIR, exist_ok=True)
    p = os.path.join(IC_DIR, "ic_curve_{}_k{}.csv".format(factor, K))
    cols = ["day"] + ["ic_h{}".format(h) for h in HOLDS]
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(p, index=False)
    return p


def refresh(factor_names=None, full: bool = False, window_days: int = WINDOW_DAYS):
    """刷新指定因子的 ic_curve 文件。

    factor_names: 传 weight_optimizer 的权重键(vol/mom_rev), 或 None=全部.
    full: True=全量重建；False=增量合并.
    返回: {"ok": True, "factors": {f: {"mode":..., "days":..., "file":...}}}
    """
    names = factor_names or list(ALPHA_FACTORS.keys())
    out = {"ok": True, "factors": {}}
    latest = _latest_settled_day()
    for wkey in names:
        factor = ALPHA_FACTORS.get(wkey, wkey)   # 直接传文件因子名也可
        entry = {"factor": factor}
        try:
            if full:
                start = "2013-01-01"
                end = (latest.isoformat() if latest else
                       datetime.date.today().isoformat())
                ic_series, curve_file = ic_backtest.run(
                    start, end, K, HOLDS, factor, use_adj=False)
                entry["mode"] = "full"
                entry["file"] = curve_file
                entry["days"] = len(_read_curve(factor))
            else:
                # 增量: 重算近端窗口, 与既有历史合并
                start = _window_start(latest, window_days)
                if start is None:
                    raise RuntimeError("daily_bars 无数据")
                end = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
                # write_curve=False: 先算近端, 不落盘, 避免覆盖既有全历史后再合并
                ic_series, curve_file = ic_backtest.run(
                    start, end, K, HOLDS, factor, use_adj=False, write_curve=False)
                # ic_series: {hold_h: pd.Series(index=date)}; 转 long df
                rows = []
                for h, s in ic_series.items():
                    for dt, v in s.dropna().items():
                        rows.append((dt.strftime("%Y%m%d"), h, float(v)))
                if not rows:
                    # 区间内无有效 IC 样本, 不破坏既有文件
                    entry["mode"] = "incremental"
                    entry["days"] = len(_read_curve(factor))
                    entry["file"] = curve_file
                    out["factors"][wkey] = entry
                    continue
                new_df = pd.DataFrame(rows, columns=["day", "hold", "ic"])
                new_df = new_df.pivot_table(
                    index="day", columns="hold", values="ic").reset_index()
                new_df.columns = ["day"] + ["ic_h{}".format(int(c)) for c in new_df.columns[1:]]

                old = _read_curve(factor)
                # 砍掉旧文件里 >= start 的部分(由新算覆盖), 再拼接新近端.
                # 注意: old.day 与 new_df 均为 YYYYMMDD, start 是 date, 统一转 YYYYMMDD 再比.
                start_s = start.strftime("%Y%m%d") if hasattr(start, "strftime") else str(start)
                old_tail_kept = old[old["day"].astype(str) < start_s]
                merged = pd.concat([old_tail_kept, new_df], ignore_index=True)
                merged = merged.drop_duplicates(subset="day", keep="last")
                merged = merged.sort_values("day").reset_index(drop=True)
                p = _write_curve(factor, merged)
                entry["mode"] = "incremental"
                entry["days"] = int(len(merged))
                entry["new_samples"] = int(len(new_df))
                entry["file"] = p
        except Exception as e:
            entry["error"] = str(e)[:200]
            out["ok"] = False
        out["factors"][wkey] = entry
    return out


def main():
    ap = argparse.ArgumentParser(description="ic_curve 刷新(打通 ICIR 实时依据)")
    ap.add_argument("--full", action="store_true", help="全量重建(慢~3min)")
    ap.add_argument("-f", "--factor", nargs="+", default=None,
                    help="权重键(vol/mom_rev)或文件因子名; 缺省=全部")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS,
                    help="增量时重算的交易日数")
    a = ap.parse_args()
    res = refresh(factor_names=a.factor, full=a.full, window_days=a.window)
    print("ok={}".format(res["ok"]))
    import json
    for wkey, e in res["factors"].items():
        print(json.dumps({wkey: e}, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
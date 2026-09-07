# -*- coding: utf-8 -*-
# ic_track.py -- A股轮动因子 IC（信息系数）衰减跟踪
#
# 数据源:
#   * data/daily/<day>/selection.json 的 "top_n" 列表（元素含 canon + signal）
#   * DuckDB 日线（StockDB.get_bars 前复权 close），用于计算 signal 日之后 N 日收益
#
# 计算 signal 与 未来 N 日收益 的 Spearman 秩相关系数(IC)，按持有期累积，
# 追加写入 data/daily/ic_history.csv，用于监控 P08 信号/因子的时效性。
#
# 用法:
#   python ic_track.py <day>                # 单日(YYYYMMDD)回算并追加
#   python ic_track.py --all                # 遍历所有含 selection.json 的天批量回算
#   python ic_track.py <day> -H 1 3 5 10    # 自定义未来收益持有期(交易日)
# ============================================================
import os
import sys
import json
import argparse

import numpy as np

from config import DATA_DIR
from db import StockDB

DAILY_DIR = os.path.join(DATA_DIR, "daily")
IC_HISTORY = os.path.join(DAILY_DIR, "ic_history.csv")


def _spearman_rank_ic(signal_vals, ret_vals):
    """手写 Spearman 秩相关(不依赖 scipy)。两序列长度一致且不足5返回 NaN。"""
    xs = np.asarray(signal_vals, dtype=float)
    ys = np.asarray(ret_vals, dtype=float)
    mask = ~(np.isnan(xs) | np.isnan(ys))
    xs, ys = xs[mask], ys[mask]
    n = len(xs)
    if n < 5 or n != len(ys):
        return float("nan")

    def _rank(v):
        # 用 argsort 两次得到升序排名(1..n)，重名不做平均秩(样本少影响小)
        order = np.argsort(np.argsort(v)).astype(float) + 1.0
        return order

    rx, ry = _rank(xs), _rank(ys)
    xm, ym = rx.mean(), ry.mean()
    num = float(np.sum((rx - xm) * (ry - ym)))
    den = float(np.sqrt(np.sum((rx - xm) ** 2) * np.sum((ry - ym) ** 2)))
    if den == 0:
        return float("nan")
    return num / den


def _norm_date(v):
    """把日期归一为 YYYYMMDD 字符串；兼容 '2026-08-24' 与 '20260824'。"""
    s = str(v).strip().replace("-", "")
    if len(s) >= 8 and s[:4].isdigit():
        return s[:8]
    return s


def _fwd_returns(db, canon, asof, holds):
    """返回个股 canon 在 asof(信号日,YYYYMMDD) 之后各持有期收益 {hold: pct}。

    以 asof 当日(或之后最近一根)收盘为基准，取其后 hold 根的收盘价计算收益。
    """
    max_hold = max(holds) if holds else 10
    bars = db.get_bars(canon, n=max_hold + 80)
    if bars is None or getattr(bars, "empty", True):
        return {}
    dates = [_norm_date(d) for d in bars["date"]]
    closes = bars["close"].astype(float).values

    base_idx = None
    for i, d in enumerate(dates):
        if d >= asof:
            base_idx = i
            break
    if base_idx is None:
        return {}

    out = {}
    for h in holds:
        end_idx = base_idx + h
        if end_idx >= len(closes):
            continue
        p0 = float(closes[base_idx])
        p1 = float(closes[end_idx])
        if p0 is None or p0 <= 0 or p1 is None or p1 <= 0:
            continue
        out[h] = p1 / p0 - 1.0
    return out


def compute_day_ic(day, db, holds):
    """读取指定天 selection.json 的 top_n，配对未来 N 日收益，返回 {hold: ic}。"""
    sel_path = os.path.join(DAILY_DIR, day, "selection.json")
    if not os.path.isfile(sel_path):
        return {}

    with open(sel_path, encoding="utf-8") as f:
        sel = json.load(f)
    pool = sel.get("top_n", [])
    if not pool:
        return {}

    pairs = [(r.get("canon"), r.get("signal")) for r in pool]
    pairs = [(c, float(s)) for c, s in pairs if c and s is not None]

    hold_returns = {h: [] for h in holds}
    for canon, sig in pairs:
        ret_by_hold = _fwd_returns(db, canon, day, holds)
        for h in holds:
            if h in ret_by_hold:
                hold_returns[h].append((sig, ret_by_hold[h]))

    ic = {}
    for h in holds:
        sample = hold_returns[h]
        if len(sample) >= 5:
            ic[h] = round(_spearman_rank_ic(
                [p[0] for p in sample], [p[1] for p in sample]), 4)
    return ic


def _read_history_rows():
    """读取 ic_history.csv 全部行(保留原始字符串), 无文件返回 (header, [])."""
    if not os.path.isfile(IC_HISTORY):
        return None, []
    with open(IC_HISTORY, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines:
        return None, []
    return lines[0], lines[1:]   # 首行为表头, 其余为数据行


def append_ic(day, ic):
    """把当日各持有期 IC 写入 ic_history.csv (幂等: 当日已存在则覆盖该行而非追加).

    表头 = 历史已有持有期列 与 当日新持有期列 的并集, 保证不同日期持有期集合
    不一致时不会相互覆盖/丢列; 缺失持有期补空字符串. 文件不存在则新建.
    """
    os.makedirs(DAILY_DIR, exist_ok=True)
    keys_new = sorted(set(ic.keys()))

    old_header, old_rows = _read_history_rows()
    old_keys = []
    if old_header:
        old_keys = [int(c.split("ic_h", 1)[1]) for c in old_header.split(",")[1:]]  # "ic_h3" -> 3
    all_keys = sorted(set(old_keys) | set(keys_new))  # 并集, 升序

    header = "day" + "".join(",ic_h{}".format(h) for h in all_keys)

    def _col(row_txt, keys_txt):
        """旧行解析: 拆分值并映射到 {hold: value}. 兼容列缺缺失."""
        parts = row_txt.split(",")
        d = str(parts[0])
        vals = {}
        for k, cell in zip(keys_txt, parts[1:]):
            if cell.strip():
                vals[k] = cell.strip()
        return d, vals

    # 旧行统一为并集表头格式
    rows = []
    for r in old_rows:
        if not r.strip():
            continue
        d, vals = _col(r, old_keys)
        rows.append((d, vals))
    # 覆盖当日已有行 (去重), 无则追加 当日行
    rows = [(d, v) for d, v in rows if d != str(day)]
    rows.append((str(day), {k: ic.get(k) for k in keys_new}))

    # 写回: 每行按并集表头补空
    with open(IC_HISTORY, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for d, vals in sorted(rows, key=lambda x: x[0]):
            cells = [vals.get(h, "") if vals.get(h) is not None else "" for h in all_keys]
            f.write(d + "".join(",{}".format(c) for c in cells) + "\n")


def available_days():
    """返回所有含 selection.json 的天，升序。"""
    if not os.path.isdir(DAILY_DIR):
        return []
    days = []
    for d in os.listdir(DAILY_DIR):
        if len(d) == 8 and d.isdigit() and os.path.isfile(os.path.join(DAILY_DIR, d, "selection.json")):
            days.append(d)
    return sorted(days)


def settle_mature_days(db, holds, today=None):
    """滞后回填: 遍历所有含 selection.json 的天, 对"已到期"的天增量补写 IC.

    背景: _fwd_returns 以 signal 日当根K线为基准取其后 hold 根K线收益, 持有期 h
    需要 signal 日后第 h 根K线已经发生才能算. 当天跑当天时未来收益尚未发生,
    因此当天永远算不出任何持有期 -> 这解释了 ic_history 从未被管道自动生成.
    本函数对每一个已过去的天做一次 compute_day_ic + append_ic: 只要 DB 里该日后
    至少 max(holds) 根K线已入库, 对应持有期就能算; append_ic 幂等覆盖, 故重复调用
    安全, 且天然"能力内补齐"(当天空, 滞后1天得 h1, 滞后>=max_hold 全量).

    参数:
      holds: 未来收益持有期(交易日).
      today: 当前数据截止日 YYYYMMDD (缺省取 DB 最新 bar 日期), 仅用于跳过 future，
             不强制——避免把"DB 尚无该日之后K线"的天误判为未到期而反复试算(无害)。
    返回: 本次新增/补齐 IC 的天数.
    """
    written = 0
    for d in available_days():
        if today is not None and d > today:
            continue
        # 幂等: 只有能算出至少一个持有期才写; 空 ic 跳过(当天/样本不足).
        ic = compute_day_ic(d, db, holds)
        if ic:
            append_ic(d, ic)
            written += 1
    return written


def main():
    parser = argparse.ArgumentParser(description="A股轮动因子 IC 跟踪")
    parser.add_argument("day", nargs="?", help="回算日 YYYYMMDD；缺省自动取最新")
    parser.add_argument("--all", action="store_true", help="遍历所有含 selection 的天")
    parser.add_argument("-H", "--hold", type=int, nargs="+", default=[1, 3, 5, 10],
                        help="未来收益持有期(交易日)，默认 1 3 5 10")
    args = parser.parse_args()

    holds = sorted(set(args.hold))
    day = args.day
    if day is None:
        days_all = available_days()
        day = days_all[-1] if days_all else None
        if day is None:
            print("无任何含 selection.json 的天")
            return 1

    db = StockDB()
    try:
        if args.all:
            total = 0
            for d in available_days():
                ic = compute_day_ic(d, db, holds)
                if ic:
                    append_ic(d, ic)
                    total += 1
                    print("  {0}: {1}".format(d, ic))
            print("批量回算完成，新写入 {} 天".format(total))
        else:
            ic = compute_day_ic(day, db, holds)
            if not ic:
                print("日 {} 样本不足(<5只配对未来收益)，未产生 IC".format(day))
                return 1
            append_ic(day, ic)
            print("day={0} IC={1}".format(day, ic))
            print("已追加到 {0}".format(IC_HISTORY))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
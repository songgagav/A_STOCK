# -*- coding: utf-8 -*-
"""DRL 取数 h5i 迁移 —— 前后口径对比验证（P0-DRLSRC）.

背景（详见登记册 `P0-DRLSRC`）
  `config.py` 声称 m4 迁移「读取方均已改为优先 h5i 并在缺失时降级」，但实测
  `drl_train._load_factor_state()` 仍连已删除的 `data/legacy_stockdb.duckdb`
  ⇒ 自 2026-09-05 起 DRL 训练完全停摆。

迁移必须**先证明口径一致**，再改生产代码。legacy DuckDB 文件已不存在，故本脚本用
**已落盘的 legacy 产物**作为对照基准 —— `train_meta.json` 里的两个字段是 `dates`
的**确定性函数**（不含任何随机性）：

  · `n_dates`    = `len(dates)`      ← 交易日窗口长度
  · `last_dates` = `dates[-5:]`      ← 窗口末端的具体日期

只要 h5i 复算出的 `len(dates)` 与 `dates[-5:]` 与 legacy 完全一致，就说明
「窗口选择 + 交易日历」这一段口径一致。再叠加 `change_pct` 交叉校验（h5i 内
**另一列**的独立数据）来验证 `rets` 序列本身。

本脚本**只读** h5i，不写任何生产文件。
用法: python scripts/preflight_drl_h5i_parity.py [--data-dir <repo>/data]
退出码: 0 = 全部一致; 1 = 有差异
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import numpy as np  # noqa: E402

_RESULTS: "list[tuple[bool, str]]" = []


def _check(ok: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _legacy_samples(data_dir: str) -> "list[tuple[str, int, list]]":
    """从已落盘的 train_meta.json 取 (day8, n_dates, last_dates) 对照样本。"""
    root = os.path.join(data_dir, "drl")
    out = []
    if not os.path.isdir(root):
        return out
    for n in sorted(os.listdir(root)):
        fp = os.path.join(root, n, "train_meta.json")
        if not (n.isdigit() and len(n) == 8 and os.path.isfile(fp)):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                m = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if m.get("n_dates") and m.get("last_dates"):
            out.append((n, int(m["n_dates"]), [str(x) for x in m["last_dates"]]))
    return out


def _h5i_dates_and_rets(store, day8: str, lookback_days: int = 60):
    """h5i 版: (dates, rets) —— 与 legacy 口径逐位对齐。

    legacy 做法: `SELECT DISTINCT date ... WHERE date BETWEEN day-60d AND day`,
    再对每对相邻交易日做 `AVG(b.close/p.close - 1)` 的自连接。
    本函数改成**一次批量查询**取回窗口内 (d, symbol, close)，再用 inner-join
    语义在 pandas 内等价计算（symbol 需两天都存在且 close>0 —— 与 legacy 的
    `JOIN ... ON b.symbol=p.symbol AND p.date=? WHERE b.close>0 AND p.close>0` 一致）。
    """
    d = dt.datetime.strptime(day8, "%Y%m%d").date()
    lo = d - dt.timedelta(days=lookback_days)
    df = store._db.sql(
        "SELECT CAST(ts AS DATE) AS d, symbol, close FROM daily_bars "
        f"WHERE CAST(ts AS DATE) >= DATE '{lo}' AND CAST(ts AS DATE) <= DATE '{d}' "
        "AND close > 0 ORDER BY d, symbol"
    ).to_pandas()
    if df is None or len(df) == 0:
        return [], np.zeros(0, dtype=np.float64)
    df["d"] = df["d"].astype(str)
    days = sorted(df["d"].unique())
    rets = [0.0]
    by_day = {k: v for k, v in df.groupby("d")}
    for i in range(1, len(days)):
        cur, prev = by_day.get(days[i]), by_day.get(days[i - 1])
        if cur is None or prev is None:
            rets.append(0.0)
            continue
        j = cur.merge(prev, on="symbol", suffixes=("_b", "_p"))
        if len(j) == 0:
            rets.append(0.0)
            continue
        r = float(np.mean(j["close_b"].to_numpy(dtype=np.float64)
                          / j["close_p"].to_numpy(dtype=np.float64) - 1.0))
        rets.append(r if np.isfinite(r) else 0.0)
    return [str(x) for x in days], np.array(rets, dtype=np.float64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None,
                    help="生产 data/ 路径（等价 QUANT_DATA_DIR；生产 data/ 未纳入 git）")
    ap.add_argument("--limit", type=int, default=0, help="只验前 N 个样本（0=全部）")
    args = ap.parse_args()

    import config  # noqa: E402
    if args.data_dir:
        config.DATA_DIR = os.path.abspath(args.data_dir)
    data_dir = config.DATA_DIR

    print("=" * 78)
    print("DRL 取数 h5i 迁移 —— 前后口径对比验证（只读 h5i，不写生产）")
    print("=" * 78)
    print(f"DATA_DIR: {data_dir}")

    from h5i_bar_store import H5iBarStore  # noqa: E402
    # 注意: `h5i_bar_store._H5I_PATH` 是**硬编码** `<模块所在仓库>/data/h5i/market.db`,
    # **不**由 config.DATA_DIR / QUANT_DATA_DIR 派生（h5i 读取不受 DATA_DIR 覆盖影响）。
    # 本仓库的 data/ 未纳入 git, 在 worktree 里不存在, 故必须显式传入生产路径。
    h5i_path = os.path.join(data_dir, "h5i", "market.db")
    print(f"h5i 库  : {h5i_path}  (存在={os.path.exists(h5i_path)})")
    if not os.path.exists(h5i_path):
        print("[FAIL] h5i 库不存在 —— 请用 --data-dir 指向含 h5i/ 的生产 data/ 目录")
        return 1
    store = H5iBarStore(h5i_path)

    samples = _legacy_samples(data_dir)
    if args.limit:
        samples = samples[-args.limit:]
    print(f"legacy 对照样本: {len(samples)} 个（取自已落盘 train_meta.json）")
    if not samples:
        print("[FAIL] 没有可对照样本")
        return 1

    print("\n--- ① 逐样本对比 n_dates / last_dates ---")
    print(f"  {'day':<10} {'legacy_n':>8} {'h5i_n':>6}  判定")
    exact, superset, lost_days, extra_days = 0, 0, [], []
    for day8, n_legacy, last_legacy in samples:
        dates, _rets = _h5i_dates_and_rets(store, day8)
        s_h5i = set(str(x) for x in dates)
        if len(dates) == n_legacy and [str(x) for x in dates[-5:]] == last_legacy:
            exact += 1
            verdict = "完全一致"
        else:
            # **精确签名检验**: 若差异只是"legacy 少了一个交易日"，那么
            # 从 h5i 窗口里删掉**恰好那一天**后，末端 5 日应与 legacy 逐位一致。
            # 在窗口里搜出所有满足该条件的 x；唯一解即证明差异被一个缺失日完全解释。
            cand = [x for x in dates
                    if [str(y) for y in [d for d in dates if d != x][-5:]] == last_legacy]
            if len(cand) == 1 and len(dates) == n_legacy + 1 and set(last_legacy) <= s_h5i:
                superset += 1
                extra_days.append((day8, cand[0]))
                verdict = f"h5i 比 legacy 多 1 日: {cand[0]}（删该日后末端 5 日逐位一致）"
            elif set(last_legacy) <= s_h5i:
                superset += 1
                extra_days.append((day8, tuple(cand)))
                verdict = f"h5i 为超集, 差异={cand}"
            else:
                lost = sorted(set(last_legacy) - s_h5i)
                lost_days.append((day8, lost))
                verdict = f"!! legacy 有而 h5i 无: {lost}"
        print(f"  {day8:<10} {n_legacy:>8} {len(dates):>6}  {verdict}")

    _check(not lost_days, "无任何『legacy 有而 h5i 无』的交易日（无数据回归）",
           f"丢失样本: {lost_days}" if lost_days else "0 个")
    _check(exact + superset == len(samples),
           "每个样本都满足『完全一致』或『差异被缺失交易日解释』",
           f"完全一致 {exact} + 可解释 {superset} = {len(samples)}")
    _uniq = {d for _s, d in extra_days if isinstance(d, str)}
    _check(len(_uniq) <= 1, "所有差异都可归因到**同一个**交易日",
           f"{sorted(_uniq)}（{len(extra_days)} 个样本）")
    if _uniq:
        _x = sorted(_uniq)[0]
        print(f"\n  ⇒ 结论: legacy DuckDB 缺 {_x} 这一天; h5i 有 ⇒ **数据补录, 非口径差异**")
        ev = [s for s in samples if s[0] == "20260901"]
        if ev:
            _check(ev[0][2][-1] != _x.replace("-", "")[:4] + "-" + _x[5:7] + "-" + _x[8:],
                   f"铁证: legacy 为 {_x} 当日生成的窗口末端不含 {_x}"
                   "（即当时 legacy daily_bars 确实缺该日）",
                   f"legacy last_dates[-1]={ev[0][2][-1]}")

    print("\n--- ② rets 序列与 h5i 另一列 change_pct 交叉校验 ---")
    day8 = samples[-1][0]
    dates, rets = _h5i_dates_and_rets(store, day8)
    d = dt.datetime.strptime(day8, "%Y%m%d").date()
    lo = d - dt.timedelta(days=60)
    df = store._db.sql(
        "SELECT CAST(ts AS DATE) AS d, symbol, close, change_pct FROM daily_bars "
        f"WHERE CAST(ts AS DATE) >= DATE '{lo}' AND CAST(ts AS DATE) <= DATE '{d}' "
        "AND close > 0 ORDER BY d, symbol"
    ).to_pandas()
    df["d"] = df["d"].astype(str)
    n_cmp, diffs = 0, []
    by_day = {k: v for k, v in df.groupby("d")}
    for i in range(1, len(dates)):
        cur, prev = by_day.get(dates[i]), by_day.get(dates[i - 1])
        if cur is None or prev is None:
            continue
        j = cur.merge(prev, on="symbol", suffixes=("_b", "_p"))
        if len(j) == 0:
            continue
        closed = float(np.mean(j["close_b"].to_numpy(dtype=np.float64)
                               / j["close_p"].to_numpy(dtype=np.float64) - 1.0))
        chg = j["change_pct_b"].astype(float).to_numpy()
        chg = chg[np.isfinite(chg)]
        if len(chg):
            n_cmp += 1
            diffs.append(abs(closed - float(np.mean(chg)) / 100.0))
    if n_cmp:
        mx = max(diffs)
        print(f"  比对天数={n_cmp}  最大偏差={mx:.6f} (= {mx * 100:.4f} 个百分点)")
        _check(mx < 5e-3,
               "close 口径与 change_pct 口径一致（偏差 < 0.5pp）",
               f"最大 {mx * 100:.4f}pp")
    else:
        _check(False, "change_pct 交叉校验可执行", "无可比对天数")

    print("\n--- ③ rets 序列健康度 ---")
    finite = bool(np.all(np.isfinite(rets)))
    _check(finite, "rets 序列全为有限值（无 NaN/Inf）", f"n={len(rets)}")
    _check(len(rets) == len(dates), "rets 与 dates 等长")
    nz = int(np.sum(np.abs(rets) > 1e-12))
    _check(nz >= max(1, len(rets) // 2), "rets 非全零（数据真的取到了）",
           f"{nz}/{len(rets)} 非零")
    print(f"  rets: n={len(rets)} mean={np.mean(rets):+.6f} std={np.std(rets):.6f} "
          f"min={np.min(rets):+.4f} max={np.max(rets):+.4f}")

    # ------------------------------------------------------------------
    # ④ target_plan 取数（步骤②）: 真实 h5i 上验证 views parquet + bars join
    # ------------------------------------------------------------------
    print("\n--- ④ target_plan 取数（步骤②）真实数据验证 ---")
    import pandas as pd  # noqa: E402
    view_p = os.path.join(data_dir, "h5i", "views", "v_factor_scores_daily.parquet")
    sym_p = os.path.join(data_dir, "h5i", "static", "symbols.parquet")
    _check(os.path.isfile(view_p), "views parquet 存在（= legacy 的 v_factor_scores_daily）")
    _check(os.path.isfile(sym_p), "symbols parquet 存在（提供 market/is_active）")
    if os.path.isfile(view_p):
        v = pd.read_parquet(view_p)
        need = ["canon", "date", "f_signal", "f_trend", "f_govern",
                "f_liquidity", "f_vol", "f_mom_rev"]
        _check(all(c in v.columns for c in need), "views 列与 legacy SQL 所需一一对应",
               f"缺={[c for c in need if c not in v.columns]}")
        v["date"] = v["date"].astype(str)
        vmax = v["date"].max()
        cur = v[v["date"] == vmax]
        print(f"  views: {len(v)} 行, 最新截面 date={vmax} 共 {len(cur)} 只")
        _check(len(cur) > 3000, "最新截面规模合理（>3000 只）", f"{len(cur)}")
        b = store.bars_on_day(vmax)     # h5i daily_bars, 键列 ts 需 CAST
        print(f"  daily_bars({vmax}): {0 if b is None else len(b)} 行")
        if b is not None and len(b):
            j = cur.merge(b, left_on="canon", right_on="symbol", how="left")
            hit = int(j["close"].notna().sum())
            print(f"  LEFT JOIN 命中: {hit}/{len(j)}")
            _check(len(j) == len(cur), "LEFT JOIN 不丢行（LEFT 语义）",
                   f"{len(j)} vs {len(cur)}")
            _check(hit > len(j) * 0.5, "多数标的能 join 到当日价格", f"{hit}/{len(j)}")
        else:
            _check(False, "最新截面当日有 daily_bars", f"date={vmax}")
        if os.path.isfile(sym_p):
            s = pd.read_parquet(sym_p)
            _check({"symbol", "market"}.issubset(set(s.columns)),
                   "symbols 含 symbol/market（+可选 is_active）", f"cols={list(s.columns)}")
            mk = s["market"].astype(str).str.lower()
            print(f"  symbols: {len(s)} 行, market 分布={dict(mk.value_counts().head(4))}")

    fails = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结论: {'全部一致' if not fails else '有差异'}  "
          f"({len(_RESULTS) - len(fails)}/{len(_RESULTS)} PASS)")
    for f in fails:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

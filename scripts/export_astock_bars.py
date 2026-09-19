# -*- coding: utf-8 -*-
"""导出 A_stock_rotation 真实日线, 供 veighna_sim 模拟盘无依赖读取.

为什么需要
  veighna_sim 的 paper 路径默认 `[data_source].provider="pseudo"`, 对任意符号返回
  **哈希伪造价**(见 `cli._pseudo_price`: 10.0 + hash%990/10)。实测 config.toml 下
  目标池 10 只全部拿到伪价(601333 真实 2.91 vs 伪价 31.5) —— 若直接接线, 会产生
  一份"看起来正常"的假价成交台账, 对账毫无意义。

  因此把**与选股同源**的日线导出为 JSON, 让 veighna_sim(无 h5i_db)按日取价。
  同源很重要: 若两侧价格源不同, 价格口径差会混进对账偏差, 重演 P0-2 那类
  "偏差无法归因"的困境。

PIT 口径
  选股决策约束在候选目录 C < D(盘前视角), 而**成交发生在 D 日、用 D 日价格**是
  合理的(D 日收盘时该价格已知)。故此处导出完整日线, 由消费方按"消费日 D 取 D 日
  收盘价"取用, 不做 decision_time 裁剪。

用法(必须用持有 h5i_db 的解释器)
  & "$env:APPDATA\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python\python.exe" \
      scripts\export_astock_bars.py --start 2026-09-01 --end 2026-09-08
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, "src"))

OUT_FP = os.path.join(_BASE, "data", "astock_bars_export.json")

#: 逐字复制 realtime_engine._A_SHARE_PREFIXES
_A_SHARE_PREFIXES = {
    "000", "001", "002", "003",
    "300", "301", "302",
    "600", "601", "603", "605",
    "688", "689",
}


def _is_a_share(canon: str) -> bool:
    if not canon:
        return False
    code = str(canon).split(".")[0]
    return len(code) >= 3 and code[:3] in _A_SHARE_PREFIXES


def _collect_symbols(d_lo: str, d_hi: str) -> list[str]:
    """范围内 plan/selection 出现过的全部 canon(带 .SH/.SZ/.BJ)。"""
    out: set[str] = set()
    for sub, fn in (("drl", "target_plan.json"), ("daily", "selection.json")):
        root = os.path.join(_BASE, "data", sub)
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not (name.isdigit() and len(name) == 8):
                continue
            ds = f"{name[:4]}-{name[4:6]}-{name[6:]}"
            if not (d_lo <= ds <= d_hi):
                continue
            fp = os.path.join(root, name, fn)
            if not os.path.isfile(fp):
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            for it in raw.get("top_n") or []:
                c = str((it or {}).get("canon", ""))
                if _is_a_share(c):
                    out.add(c)
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出真实日线供 veighna_sim 使用")
    ap.add_argument("--start", required=True, help="起始日 YYYY-MM-DD(含缓冲前)")
    ap.add_argument("--end", required=True, help="结束日 YYYY-MM-DD")
    ap.add_argument("--lookback-days", type=int, default=15,
                    help="额外向前导出的日历天数(供前收/涨跌停判断)")
    ap.add_argument("--out", default=OUT_FP)
    args = ap.parse_args(argv)

    d_lo = args.start
    d_hi = args.end
    syms = _collect_symbols(d_lo, d_hi)
    if not syms:
        print(f"[export_astock_bars] {d_lo}~{d_hi} 内未发现任何 pool 符号, 放弃写出",
              file=sys.stderr)
        return 2

    lo = (datetime.strptime(d_lo, "%Y-%m-%d") - timedelta(days=args.lookback_days)
          ).strftime("%Y-%m-%d")

    from h5i_bar_store import H5iBarStore  # type: ignore

    store = H5iBarStore()
    bars: dict[str, dict] = {}
    missing: list[str] = []
    try:
        for s in syms:
            # daily_bars.symbol 存**裸 6 位代码**(无 .SH/.SZ 后缀); 键仍用带后缀 canon
            try:
                df = store.bars(s.split(".")[0], start=lo, end=d_hi)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{s}:ERR {type(exc).__name__}")
                continue
            if df is None or len(df) == 0:
                missing.append(f"{s}:无数据")
                continue
            rec: dict[str, dict] = {}
            prev_close = None
            for _, r in df.iterrows():
                d = str(r["d"])[:10]
                close = r["close"]
                rec[d] = {
                    "open": None if r["open"] is None else round(float(r["open"]), 4),
                    "high": None if r["high"] is None else round(float(r["high"]), 4),
                    "low": None if r["low"] is None else round(float(r["low"]), 4),
                    "close": None if close is None else round(float(close), 4),
                    "volume": None if r.get("volume") is None else float(r["volume"]),
                    "pre_close": prev_close,
                }
                if close is not None:
                    prev_close = round(float(close), 4)
            bars[s] = rec
        src = "h5i:daily_bars"
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": src,
        "range": [d_lo, d_hi],
        "lookback_from": lo,
        "n_symbols": len(bars),
        "n_bars": sum(len(v) for v in bars.values()),
        "missing": missing,
        "bars": bars,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[export_astock_bars] {args.out} symbols={len(bars)}/{len(syms)} "
          f"bars={payload['n_bars']} range={d_lo}~{d_hi} missing={len(missing)}")
    if missing:
        print("  缺失明细(前10):", missing[:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

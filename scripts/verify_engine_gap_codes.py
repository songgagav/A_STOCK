# -*- coding: utf-8 -*-
"""交叉验证「厂商引擎缺失的代码」是**真实标的**还是**幻影代码**（P2-ENGINEGAP）.

背景
----
换源对账时发现: 引擎 8 个交易日的代码并集 = 5490, 离线镜像分片 = 5548,
其中 **58 只镜像有、引擎没有**。逐只查引擎全历史后:
  · 4 只可解释(引擎有历史, 只是窗口内无成交) —— 停牌/退市, 不构成覆盖问题;
  · **54 只引擎完全没有记录**, 且被引擎的代码序列跳过
    (引擎有 603429/603439 而无 603435; 有 688805/688807 而无 688806),
    历史极短(13–77 行), 其中 688825 的 volume 累计 144 亿股, 明显荒谬。

两种可能, 后果完全不同:
  · **真实标的** ⇒ 厂商引擎覆盖不全 ⇒ 需要第三条补数路径, 且**绝不能删**;
  · **幻影代码** ⇒ 镜像那一侧产生了不存在的代码, 连同数据一起进了 h5i
    ⇒ 会污染 universe / 因子 / 权重。
**故在定论前一行都不删**（用户明确要求）。

判据(三源交叉)
--------------
1. `ak.stock_info_a_code_name()`  —— 当前在市 A 股全表(代码+名称)
2. `ak.stock_info_sh_delist()` / `ak.stock_info_sz_delist()` —— 已退市名单
3. 镜像侧该代码的历史(行数/起止/名称) —— 用于人工复核

分类:
  · IN_MARKET  在市  => 真实标的
  · DELISTED   已退市 => 真实标的(曾上市)
  · NEVER      三源皆无 => **疑似幻影**, 需进一步人工确认后才可处置

用法
  python scripts/verify_engine_gap_codes.py                 # 自动重算缺口并验证
  python scripts/verify_engine_gap_codes.py --codes 603435 688806 ...
  python scripts/verify_engine_gap_codes.py --out data/engine_gap_verify.json
退出码: 0 = 全部有定论; 1 = 存在 NEVER(需人工); 2 = 环境错误
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

LAKE = os.environ.get("STOCKDB_ROOT", "").strip() or r"E:\A_stockDB"
PARTS = os.path.join(LAKE, "kline_parts")
PYBAO = os.environ.get("PYBAO_DIR", "").strip() or os.path.join(LAKE, "pybao")
PREFIXES = ["0*", "3*", "6*", "9*"]
# 待补的 8 个交易日(用于算"引擎窗口内有没有"), 与 2026-09-21 那次换源一致。
WINDOW_DAYS = ["20260909", "20260910", "20260911", "20260914",
               "20260915", "20260916", "20260917", "20260918"]


def _engine_codes() -> set:
    if PYBAO not in sys.path:
        sys.path.insert(0, PYBAO)
    from stock_sdk import rd
    out = set()
    for d in WINDOW_DAYS:
        for pfx in PREFIXES:
            try:
                for r in rd.vals("日k", pfx, d):
                    out.add(str(r.get("code")).zfill(6))
            except Exception:  # noqa: BLE001
                pass
    return out


def _mirror_codes() -> set:
    out = set()
    for f in glob.glob(os.path.join(PARTS, "*.parquet")):
        out.add(os.path.basename(f).split("_")[-1].replace(".parquet", "").zfill(6))
    return out


def _mirror_detail(code: str) -> dict:
    """镜像侧该代码的实况(行数/起止) —— 人工复核用, 不参与自动判定。"""
    import pandas as pd
    for mkt in ("sh", "sz", "bj"):
        f = os.path.join(PARTS, f"{mkt}_{code}.parquet")
        if os.path.exists(f):
            try:
                df = pd.read_parquet(f)
                d = pd.to_datetime(df["trade_date"], errors="coerce")
                return {"file": os.path.basename(f), "rows": int(len(df)),
                        "first": str(d.min().date()), "last": str(d.max().date()),
                        "volume_sum": float(pd.to_numeric(df.get("volume"),
                                                          errors="coerce").sum())}
            except Exception as e:  # noqa: BLE001
                return {"file": os.path.basename(f), "error": f"{type(e).__name__}: {e}"}
    return {"file": None}


def _ak_sources() -> dict:
    """取三个 akshare 源; 任一失败都**明确记录**, 不静默当成'不存在'。"""
    import akshare as ak
    src = {}

    def _try(key, fn):
        try:
            df = fn()
            src[key] = {"ok": True, "rows": int(len(df)), "df": df}
        except Exception as e:  # noqa: BLE001
            src[key] = {"ok": False, "rows": 0,
                        "error": f"{type(e).__name__}: {str(e)[:160]}"}
    _try("a_code_name", ak.stock_info_a_code_name)
    _try("sh_delist", ak.stock_info_sh_delist)
    _try("sz_delist", ak.stock_info_sz_delist)
    return src


def _code_set(src: dict, key: str, col_candidates=("code", "证券代码", "公司代码",
                                                   "股票代码", "A股代码")) -> set:
    e = src.get(key) or {}
    if not e.get("ok"):
        return set()
    df = e["df"]
    col = next((c for c in col_candidates if c in df.columns), None)
    if col is None:
        col = df.columns[0]
    return {str(x).strip().zfill(6) for x in df[col].tolist()}


def _name_map(src: dict) -> dict:
    e = src.get("a_code_name") or {}
    if not e.get("ok"):
        return {}
    df = e["df"]
    ccol = next((c for c in ("code", "证券代码", "股票代码") if c in df.columns), df.columns[0])
    ncol = next((c for c in ("name", "证券简称", "股票简称") if c in df.columns), None)
    if ncol is None:
        return {}
    return {str(r[ccol]).strip().zfill(6): str(r[ncol]).strip() for _, r in df.iterrows()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", help="显式代码; 缺省则自动重算引擎/镜像缺口")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.codes:
        gap = sorted({c.zfill(6) for c in args.codes})
        print(f"  使用显式代码 {len(gap)} 只")
    else:
        try:
            eng, mir = _engine_codes(), _mirror_codes()
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] 引擎/镜像取数失败: {type(e).__name__}: {e}")
            return 2
        gap = sorted(mir - eng)
        print(f"  引擎(8 日并集) {len(eng)} 只 | 镜像 {len(mir)} 只 | **镜像有引擎无 = {len(gap)}**")

    try:
        src = _ak_sources()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] akshare 不可用: {type(e).__name__}: {e}")
        return 2

    print("\n  --- akshare 三源状态(失败必须显式可见, 不能被当成'不存在') ---")
    for k, v in src.items():
        print(f"    {k:<12} ok={v['ok']}  rows={v['rows']}"
              + (f"  error={v['error']}" if not v["ok"] else ""))

    in_market = _code_set(src, "a_code_name")
    names = _name_map(src)
    delisted = _code_set(src, "sh_delist") | _code_set(src, "sz_delist")
    print(f"\n  在市集合 {len(in_market)} | 退市集合 {len(delisted)}")

    rows, never = [], []
    for c in gap:
        if c in in_market:
            verdict = "IN_MARKET"
        elif c in delisted:
            verdict = "DELISTED"
        else:
            verdict = "NEVER"
            never.append(c)
        rows.append({"code": c, "verdict": verdict, "name": names.get(c),
                     "mirror": _mirror_detail(c)})

    print("\n" + "=" * 78)
    print(f"  逐只判定（共 {len(rows)}）")
    print("=" * 78)
    for r in rows:
        m = r["mirror"] or {}
        vs = m.get("volume_sum")
        vs_txt = f"{vs:.0f}" if isinstance(vs, (int, float)) else "-"
        print(f"  {r['code']}  {r['verdict']:<10} name={(r['name'] or '(无)'):<12} "
              f"镜像 rows={m.get('rows')} {m.get('first')}..{m.get('last')} vol_sum={vs_txt}")

    from collections import Counter
    cnt = Counter(r["verdict"] for r in rows)
    print(f"\n  汇总: {dict(cnt)}")
    if never:
        print(f"\n  [需人工] 三源皆无的 {len(never)} 只: {never}")
        print("  注意: '三源皆无'**不等于**已判定为幻影 —— akshare 的在市表只含当前在市,")
        print("        退市表可能不全; 仍需逐只人工确认(如查交易所公告)后才可处置。")
    else:
        print("\n  [PASS] 全部代码都能在 akshare 中定位 —— 均为真实标的(在市或已退市)")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                       "source_status": {k: {kk: vv for kk, vv in v.items() if kk != "df"}
                                         for k, v in src.items()},
                       "gap_n": len(gap), "summary": dict(cnt), "rows": rows},
                      f, ensure_ascii=False, indent=2)
        print(f"\n  结果已写: {args.out}")

    return 1 if never else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""已知坏行**排除登记**（登记册 P1-ZEROFILL 的用户决定：标记为排除项，不改写）。

背景与决定
----------
h5i `daily_bars` 存量里有 **7 行零填充占位行**（open=high=low=0，仅 close 有值）:
  2026-09-08: 600825 / 600929 / 688291 / 688432
  2026-09-07: 600825 / 600929 / 688432
它们来自换源前的旧镜像覆盖缺口，是**历史既成事实**。

用户 2026-09-22 决定: **标记为排除项，不改写**。三条修法里另两条（按停牌惯例改写为"昨收复制"、
或直接删除）都要求**绕过 h5i 的单调追加约束去改历史行** —— 那是对已落盘事实的改写，
收益（7 行 / 1600 万行）远小于风险（一旦改错无法回滚，且破坏"写入即不可变"的既有约定）。
故本模块只做标记与查询。

**为什么"标记"必须真的被消费**：登记而不消费等于没登记。故提供:
  · `is_excluded(symbol, date)`     —— 单点查询
  · `filter_frame(df)`              —— 批量过滤(读侧接入用, 返回被丢弃的行数)
  · `explain(symbol, date)`         —— 为什么这行被排除(给人看的理由)
消费方接入是**后续项**：本模块先保证"标记存在、可查、可校验"，并在扫描里把
『已知排除』与『新出现的坏行』分开报 —— 后者才是需要人立刻看的。

登记文件带 `decided_by` / `decided_at` / `reason`：排除一个数据行是有后果的动作，
必须能回答"谁在什么时候因为什么把它排除了"。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def exclusions_path(path: str | None = None) -> str:
    return path or os.path.join(_repo_root(), "data", "quality_exclusions.json")


def load_exclusions(path: str | None = None) -> dict:
    """读登记文件。**读不到一律返回空登记**, 且 `ok=False` 供调用方区别对待。"""
    fp = exclusions_path(path)
    if not os.path.isfile(fp):
        return {"ok": False, "error": f"登记文件不存在: {fp}", "rows": []}
    try:
        with open(fp, encoding="utf-8-sig") as f:
            j = json.load(f)
        if not isinstance(j, dict):
            return {"ok": False, "error": "登记文件不是对象", "rows": []}
        j["ok"] = True
        j.setdefault("rows", [])
        return j
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "rows": []}


def _key(symbol, date) -> tuple:
    d = str(date).replace("-", "")
    return (str(symbol).strip(), d[:8])


def _norm_rows(rows) -> list:
    """登记行 -> 规范化 (symbol8, date8) 列表。字段缺失的行被忽略(不猜)。"""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        s = r.get("symbol")
        d = r.get("date")
        if s is None or d is None:
            continue
        out.append(_key(s, d))
    return out


def excluded_keys(path: str | None = None) -> set:
    return set(_norm_rows(load_exclusions(path).get("rows")))


def is_excluded(symbol, date, path: str | None = None) -> bool:
    return _key(symbol, date) in excluded_keys(path)


def explain(symbol, date, path: str | None = None) -> dict | None:
    """为什么这行被排除（人看的理由 + 谁在何时决定的）。"""
    k = _key(symbol, date)
    for r in load_exclusions(path).get("rows") or []:
        if _norm_rows([r]) and _norm_rows([r])[0] == k:
            return r
    return None


def filter_frame(df, path: str | None = None):
    """批量过滤（读侧接入用）。返回 (过滤后的 df, 被丢弃的行数)。

    只按 `symbol` + `date` 两列匹配 —— 登记的是**具体某天某只**的坏行, 不做泛化,
    以免把同标的其它正常交易日一起误伤。
    """
    keys = excluded_keys(path)
    if not keys or df is None or len(df) == 0:
        return df, 0
    try:
        cols = {c.lower(): c for c in df.columns}
        sc, dc = cols.get("symbol"), cols.get("date")
        if not sc or not dc:
            return df, 0
        keep = []
        for s, d in zip(df[sc], df[dc]):
            keep.append(_key(s, d) not in keys)
        import pandas as pd  # noqa: PLC0415
        out = df[pd.Series(keep, index=df.index)]
        return out, int(len(df) - len(out))
    except Exception:  # noqa: BLE001
        return df, 0


def save_rows(rows: list, reason: str, actor: str = "human",
              path: str | None = None, now=None) -> dict:
    """登记一批排除行（幂等：同 (symbol,date) 不重复登记）。"""
    doc = load_exclusions(path)
    doc = {"version": 1, "rows": doc.get("rows") or [], "updated": None}
    have = set(_norm_rows(doc["rows"]))
    added = 0
    for r in rows or []:
        s, d = (r.get("symbol"), r.get("date")) if isinstance(r, dict) else (None, None)
        if s is None or d is None:
            continue
        k = _key(s, d)
        if k in have:
            continue
        doc["rows"].append({"symbol": k[0], "date": k[1], "reason": reason,
                            "decided_by": actor,
                            "decided_at": (now or datetime.now()).strftime(_TS_FMT)})
        have.add(k)
        added += 1
    doc["updated"] = (now or datetime.now()).strftime(_TS_FMT)
    fp = exclusions_path(path)
    d = os.path.dirname(fp)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, fp)
    return {"added": added, "total": len(doc["rows"])}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="已知坏行排除登记")
    ap.add_argument("--list", action="store_true", help="列出全部排除项")
    ap.add_argument("--check", nargs=2, metavar=("SYMBOL", "DATE"), help="查某行是否被排除")
    args = ap.parse_args(argv)

    if args.check:
        s, d = args.check
        e = explain(s, d)
        print("已排除: " + json.dumps(e, ensure_ascii=False) if e else "未在排除登记中（=> 若扫描报坏行, 属新问题）")
        return 0

    doc = load_exclusions()
    if not doc.get("ok"):
        print("!! " + str(doc.get("error")))
        return 1
    print(f"排除登记: {len(doc['rows'])} 行  (updated={doc.get('updated')})")
    for r in doc["rows"]:
        print(f"  {r['symbol']} {r['date']}  {r.get('reason','')}")
        print(f"      决定: {r.get('decided_by')} @ {r.get('decided_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

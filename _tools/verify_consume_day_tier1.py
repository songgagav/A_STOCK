# -*- coding: utf-8 -*-
"""一次性验收: 有了"新契约"产物后, 第 1 档是否**真的**能命中(端到端).

## 为什么这道验收不可省

`_tools/verify_consume_day_regression2.py` 证明了**旧目录取池不变**(18/18 一致),
但它的必然推论是"第 1 档命中 0 次" —— 因为磁盘上**还没有任何**声明
`consume_day` 的产物(它们全是改动前写的)。也就是说: 兼容性验过了, **新能力
还没验过**。若不补这一道, 很可能出现"改动上线、兼容无损、但新路径从没跑通"
—— 那正是 DISC-2 ② 的形态(没失败, 只是没被验证)。

## 做法

在**临时 QUANT_DATA_DIR** 里搭一个最小 `data/drl/` 磁盘状态:

  1. 只放旧产物 -> 断言第 1 档**不**命中(不能把旧产物当新契约);
  2. 放入一份 `consume_day == 消费日` 的新产物 -> 断言第 1 档**命中**且来源正确;
  3. 放入一份 `consume_day == 别的日子` 的新产物 -> 断言**不**命中;
  4. 放入一份"非交易日写出的、consume_day 为 null"的产物 -> 断言**不**命中
     (这条锁住长周末里维护产物不得被第 1 档取用)。

**只读**(临时目录内写入, 不碰真实 data/)。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

import realtime_engine as re  # noqa: E402

A_ITEM = {"canon": "600016.SH", "price": 10.0}
CONSUME = "2026-09-29"
AUTHOR = "2026-09-28"
PREV_TRADE = datetime.datetime(2026, 9, 28)


def _plan(consume_day, generated_at, day="2026-09-28"):
    return {"day": day, "consume_day": consume_day,
            "generated_at": generated_at, "top_n": [dict(A_ITEM)]}


def _write(drl_root, day8, plan):
    d = os.path.join(drl_root, day8)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "target_plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)


def _load(drl_root):
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "drl"), exist_ok=True)
        for day8, plan in drl_root:
            _write(os.path.join(td, "drl"), day8, plan)
        orig = (re.DATA_DIR, re.DAILY_DIR)
        re.DATA_DIR = td
        re.DAILY_DIR = os.path.join(td, "daily")
        try:
            return re._load_daily_plan_for_consume_day(
                CONSUME.replace("-", ""), CONSUME, PREV_TRADE)
        finally:
            re.DATA_DIR, re.DAILY_DIR = orig


CASES = []


def _check(name, cond, detail=""):
    CASES.append((name, bool(cond), detail))
    print("[%s] %s%s" % ("OK  " if cond else "FAIL", name,
                         ("  <- " + detail) if (detail and not cond) else ""))


def main() -> int:
    print("=" * 78)
    print("第 1 档命中验收: 消费日 %s (撰写日 %s)" % (CONSUME, AUTHOR))
    print("=" * 78)

    # 1) 只有旧产物 -> 不得命中
    src, top_n, _ = _load([("20260928", {
        "day": AUTHOR, "generated_at": "2026-09-28 19:30:00",
        "top_n": [dict(A_ITEM)]})])
    _check("① 仅旧产物(无字段) -> 第 1 档不命中", src is None and top_n is None,
           f"src={src}")

    # 2) 新产物, consume_day == 消费日 -> 必须命中
    src, top_n, _ = _load([("20260928", _plan(CONSUME, "2026-09-28 19:30:00"))])
    _check("② 新产物 consume_day==消费日 -> 命中", src == "20260928" and top_n,
           f"src={src} n={len(top_n or [])}")

    # 3) 新产物, consume_day == 别的日子 -> 不得命中
    src, top_n, _ = _load([("20260928", _plan("2026-09-30",
                                              "2026-09-28 19:30:00"))])
    _check("③ 新产物 consume_day!=消费日 -> 不命中", src is None and top_n is None,
           f"src={src}")

    # 4) 非交易日写出的维护产物(consume_day=null) -> 不得命中
    src, top_n, _ = _load([("20260927", _plan(None, "2026-09-27 15:06:46",
                                              day="2026-09-27"))])
    _check("④ 非交易日维护产物(consume_day=null) -> 不命中",
           src is None and top_n is None, f"src={src}")

    # 5) 同日既有新产物又有旧产物 -> 应取**声明了字段**的那份(新契约优先),
    #    且新旧同在一个目录时按目录倒序取最新
    src, top_n, _ = _load([
        ("20260925", {"day": "2026-09-25", "generated_at": "2026-09-25 19:10:00",
                      "top_n": [dict(A_ITEM)]}),          # 旧产物(周五, 交易日)
        ("20260928", _plan(CONSUME, "2026-09-28 19:30:00")),  # 新产物
    ])
    _check("⑤ 同存新旧 -> 取声明字段的那份", src == "20260928",
           f"src={src}")

    # 6) 多份新产物都声明同一消费日 -> 取目录日**最新**的那份
    src, top_n, _ = _load([
        ("20260924", _plan(CONSUME, "2026-09-24 19:12:46", day="2026-09-24")),
        ("20260928", _plan(CONSUME, "2026-09-28 19:30:00")),
    ])
    _check("⑥ 多份命中 -> 取目录日最新", src == "20260928", f"src={src}")

    print("=" * 78)
    n_ok = sum(1 for _, ok, _ in CASES if ok)
    print("小结: %d/%d 通过" % (n_ok, len(CASES)))
    for name, ok, detail in CASES:
        if not ok:
            print("  FAIL %s: %s" % (name, detail))
    print("=" * 78)
    return 0 if n_ok == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())

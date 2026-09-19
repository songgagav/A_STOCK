# -*- coding: utf-8 -*-
"""DRL 降级链（DRL-4）**真实数据**验证 —— 只读生产 `data/drl/`, 不写生产任何文件.

为什么需要这个脚本（而不是只跑单测）:
  单测用的是 tmp 目录里**我构造**的布局; 而 `data/drl/` 的真实布局是**混放**的 ——
  实盘逐日版本 与 回测/验证遗留目录 混在一起。这个"混放"是单测里看不到、
  也是我在写 DRL-4 时**差点做错**的地方: 朴素按日期倒序扫描会命中遗留目录。
  实测真实布局（2026-09-19）: 32 个 8 位目录 = 12 个有实盘标记 + 20 个无标记,
  其中无标记的 7 个在朴素判据下"可用"（含 1 个缺标记的实盘日 20260825）。

本脚本做三件事（全部只读生产版本目录）:
  ① 盘点真实版本目录: 有标记 vs 无标记, 各有多少、哪些"朴素判据下可用"
  ② 反事实: 若沿用朴素扫描, 在"2026 年版本全坏"时会选到哪一天（= 会静默用错模型）
  ③ 现实现: 同一场景下 `resolve()` 必须 **L3 暂停** 而不是用遗留模型

安全保证:
  · `config.DATA_DIR` = 生产（只用于**读**版本目录）
  · `DRL_MODEL_POINTER` / `DRL_DEGRADE_LEDGER` / `DRL_VALIDATION_LEDGER` 全部指向 tmp
  · 结束前断言生产 `data/drl/current_model.json` 与 `data/drl_degrade_events.jsonl` **未被创建/未变**

用法: python scripts/preflight_drl_degrade_realdata.py [--day YYYY-MM-DD] [--data-dir DIR]
      生产 `data/` **未纳入 git**（worktree / 干净检出里没有）, 故通常需显式指定:
        QUANT_DATA_DIR=<repo>/data python scripts/preflight_drl_degrade_realdata.py
        或: python scripts/preflight_drl_degrade_realdata.py --data-dir <repo>/data
退出码: 0 = 全部断言通过; 1 = 有断言失败
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import config  # noqa: E402
import drl_degrade as D  # noqa: E402

_RESULTS: "list[tuple[bool, str]]" = []


def _check(ok: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _md5(path: str) -> "str | None":
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _naive_latest(before_day: str, exclude: str = "") -> "str | None":
    """**修复前**的朴素逻辑: 只按日期倒序 + 结构判据（不含实盘甄别）。"""
    root = D._drl_root()
    if not os.path.isdir(root):
        return None
    d8 = before_day.replace("-", "")
    ex = (exclude or "").replace("-", "")
    names = [n for n in os.listdir(root)
             if n.isdigit() and len(n) == 8 and n < d8 and n != ex]
    for n in sorted(names, reverse=True):
        if D.version_usable(n, require_live=False):
            return n
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="评估日 YYYY-MM-DD（默认: 生产最新实盘日+1）")
    ap.add_argument("--data-dir", default=None,
                    help="生产 data/ 路径（等价于 QUANT_DATA_DIR；生产 data/ 未纳入 git）")
    args = ap.parse_args()

    if args.data_dir:
        config.DATA_DIR = os.path.abspath(args.data_dir)

    tmp = tempfile.mkdtemp(prefix="drl_degrade_realdata_")
    os.environ["DRL_MODEL_POINTER"] = os.path.join(tmp, "current_model.json")
    os.environ["DRL_DEGRADE_LEDGER"] = os.path.join(tmp, "drl_degrade_events.jsonl")
    os.environ["DRL_VALIDATION_LEDGER"] = os.path.join(tmp, "drl_validation_metrics.jsonl")

    prod_ptr = os.path.join(config.DATA_DIR, "drl", D.POINTER_NAME)
    prod_led = os.path.join(config.DATA_DIR, D.EVENT_LEDGER_NAME)
    before_ptr, before_led = _md5(prod_ptr), _md5(prod_led)

    root = D._drl_root()
    print("=" * 78)
    print("DRL 降级链 DRL-4 —— 真实数据验证（只读生产版本目录）")
    print("=" * 78)
    print(f"DATA_DIR        : {config.DATA_DIR}")
    print(f"版本根目录      : {root}")
    print(f"指针/账本(临时) : {tmp}")
    print(f"实盘标记文件    : {D.LIVE_MARKER_NAME}")
    print(f"回退窗口(天)    : {D.FALLBACK_LOOKBACK_DAYS}")

    if not os.path.isdir(root):
        print(f"\n[FAIL] 版本根目录不存在: {root}")
        print("       生产 data/ 未纳入 git；worktree / 干净检出里没有它。请显式指定:")
        print("         QUANT_DATA_DIR=<repo>/data python scripts/preflight_drl_degrade_realdata.py")
        print("         或 --data-dir <repo>/data")
        return 1

    days = sorted(n for n in os.listdir(root)
                  if os.path.isdir(os.path.join(root, n)) and n.isdigit() and len(n) == 8)
    live = [d for d in days if D.is_live_version(d)]
    legacy = [d for d in days if d not in live]
    naive_usable_legacy = [d for d in legacy if D.version_usable(d, require_live=False)]
    live_usable = [d for d in live if D.version_usable(d)]

    print("\n--- ① 真实版本目录盘点 ---")
    print(f"  8 位数字目录总数     : {len(days)}")
    print(f"  实盘版本 (有标记)    : {len(live)}   {live[0] if live else '-'} .. {live[-1] if live else '-'}")
    print(f"  遗留目录 (无标记)    : {len(legacy)}   {legacy[0] if legacy else '-'} .. {legacy[-1] if legacy else '-'}")
    print(f"  遗留中'朴素可用'的   : {len(naive_usable_legacy)}  {naive_usable_legacy}")
    print(f"  实盘且结构可用的     : {len(live_usable)}  {live_usable}")

    _check(legacy and not any(D.is_live_version(d) for d in legacy),
           "实盘判据不与任何遗留目录重叠", f"遗留 {len(legacy)} 个, 命中 0 个")
    _check(all(D.is_live_version(d) for d in live),
           "实盘判据覆盖全部实盘版本", f"{len(live)}/{len(live)}")
    _check(not any(D.version_usable(d) for d in naive_usable_legacy),
           "遗留目录在现判据下一律不可用", f"朴素可用者 {len(naive_usable_legacy)} 个已全被挡")
    _check(len(naive_usable_legacy) > 0,
           "存在'朴素判据可用'的遗留目录 (证明该防线不是空谈)",
           f"{len(naive_usable_legacy)} 个: {naive_usable_legacy}")

    day = args.day
    if not day:
        if live:
            last = dt.datetime.strptime(max(live), "%Y%m%d").date()
            day = (last + dt.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            day = dt.date.today().strftime("%Y-%m-%d")
    day8 = day.replace("-", "")
    print(f"\n评估日: {day}  (指针缺失的冷启动状态)")

    print("\n--- ② 反事实: 沿用朴素扫描会选到哪一天 ---")
    naive = _naive_latest(day8, exclude=day8)
    print(f"  朴素扫描结果        : {naive}")
    if naive and not D.is_live_version(naive):
        age = (dt.datetime.strptime(day8, "%Y%m%d").date()
               - dt.datetime.strptime(naive, "%Y%m%d").date()).days
        print(f"  ⚠ 该结果**不是实盘版本**, 且距今 {age} 天 -> 若采用 = 静默用错模型")

    print("\n--- ③ 现实现: 同一场景下的分级 ---")
    real = D.resolve(day, train_ok=False, fail_reason="realdata 验证")
    print(f"  level={real['level']} ({real.get('level_name')})  "
          f"source_day={real.get('source_day')}  halt={real.get('halt')}")

    print("\n--- ④ 关键防线: 模拟'2026 年全部版本都不可用'（如模型文件被清空/损坏）---")
    # 忠实模拟: 2026 年的版本文件全部坏掉, 只剩 2015-2021 的遗留目录结构性完好。
    _orig_usable = D.version_usable

    def _fake_usable(d, require_live=True):
        if str(d).replace("-", "").startswith("2026"):
            return False
        return _orig_usable(d, require_live=require_live)

    D.version_usable = _fake_usable
    try:
        naive2 = _naive_latest(day8, exclude=day8)
        D.FALLBACK_LOOKBACK_DAYS = 999999          # 连窗口这道保险也去掉
        decided = D.resolve(day, train_ok=False, fail_reason="2026 版本全不可用(模拟)")
        scan = D.scan_versions(day8, exclude_day=day8)
    finally:
        D.version_usable = _orig_usable
        D.FALLBACK_LOOKBACK_DAYS = 45

    print(f"  朴素扫描会选        : {naive2}")
    print(f"  现实现分级          : level={decided['level']} halt={decided['halt']} "
          f"source_day={decided['source_day']}")
    print(f"  扫描: {scan['scanned']} 个目录, 非实盘 {len(scan['skipped_not_live'])} 个, "
          f"窗口外 {len(scan['skipped_out_of_window'])} 个")
    _check(naive2 is not None and not D.is_live_version(naive2),
           "反事实: 朴素扫描确实会落到**非实盘**的遗留目录",
           f"{naive2}({'有' if naive2 and naive2.startswith('2026') else '无'}标记)")
    _check(decided["level"] == D.LEVEL_HALT and decided["halt"] is True,
           "近端版本全不可用 -> 必须 L3 暂停交易, 绝不用遗留模型",
           f"level={decided['level']}")
    _check(decided["source_day"] is None, "L3 不得给出任何'生效来源日'")
    _check(scan["skipped_not_live"], "被跳过的非实盘目录已计数留痕",
           f"{len(scan['skipped_not_live'])} 个")

    print("\n--- ⑤ 生产文件未被写入 ---")
    after_ptr, after_led = _md5(prod_ptr), _md5(prod_led)
    _check(after_ptr == before_ptr, "生产指针文件未被创建/未变",
           f"{'(不存在)' if after_ptr is None else after_ptr[:12]}")
    _check(after_led == before_led, "生产降级账本未被创建/未变",
           f"{'(不存在)' if after_led is None else after_led[:12]}")

    ledger = os.environ["DRL_DEGRADE_LEDGER"]
    n_ev = sum(1 for ln in open(ledger, encoding="utf-8") if ln.strip()) \
        if os.path.isfile(ledger) else 0
    print(f"\n  临时账本事件数      : {n_ev}  (仅记录到 tmp, 未污染生产)")

    fails = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结论: {'全部通过' if not fails else '有失败项'}  "
          f"({len(_RESULTS) - len(fails)}/{len(_RESULTS)} PASS)")
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

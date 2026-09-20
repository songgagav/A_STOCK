# -*- coding: utf-8 -*-
"""DRL 环境门禁 + 显式 L3 + 可见性 的**双解释器**验证（用户决策 D，2026-09-20）.

为什么必须在**两个**解释器里各跑一次
  生产的实际故障形态就是"换解释器就换一种缺失": `.venv314` 缺 h5i_db、3.10 缺 torch。
  只在其中一个里验证等于没验证 —— 用户要的正是"**任何**解释器下都响亮地置 L3"。

验证四项（对应"立即"清单）
  ① 显式置 L3 且**不生成新信号** —— 即使存在可用旧模型也不回退（决策 D 的核心）
  ② 落盘降级账本 —— 每次触发一条, 且**多次触发都留痕**
  ③ 可见性 —— `current_level()` / `last_event()` / `event_count()` 可读（复盘/面板用）,
     且 metrics_server 的 astock_drl_* 指标真的被设值（告警用）
  ④ 只写沙箱, 不碰生产

用法（建议两个解释器各跑一次）:
  <py> scripts/preflight_drl_env_gate.py --data-dir <沙箱> --prod-data <生产 data/>
退出码: 0 = 全部断言通过; 1 = 有失败
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

_RESULTS: "list[tuple[bool, str]]" = []


def _check(ok: bool, label: str, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def _md5(p):
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True, help="沙箱输出目录")
    ap.add_argument("--prod-data", default=None, help="生产 data/（仅校验 md5 未变）")
    ap.add_argument("--day", default="2026-09-20")
    args = ap.parse_args()

    sandbox = os.path.abspath(args.data_dir)
    day_dir = args.day.replace("-", "")
    os.makedirs(sandbox, exist_ok=True)
    os.environ["QUANT_DATA_DIR"] = sandbox

    before = {}
    if args.prod_data:
        for n in ("state.json", "live_state.json"):
            before[n] = _md5(os.path.join(args.prod_data, n))
        before["<prod plan>"] = _md5(os.path.join(
            args.prod_data, "drl", day_dir, "target_plan.json"))
        before["<prod ledger>"] = _md5(os.path.join(args.prod_data, "drl_degrade_events.jsonl"))

    import config
    import dataguard
    import drl_degrade as D

    config.DATA_DIR = sandbox
    dataguard.reset_warnings()

    print("=" * 78)
    print("DRL 环境门禁 / 显式 L3 / 可见性 —— 双解释器验证")
    print("=" * 78)
    print(f"解释器        : {sys.version.split()[0]}  ({sys.executable})")
    print(f"沙箱 DATA_DIR : {sandbox}")

    print("\n--- ① 运行环境探测（不 import 重依赖）---")
    probe = D.probe_runtime()
    print(f"  deps   : {probe['deps']}")
    print(f"  missing: {probe['missing']}")
    _check("python" in probe and "note" in probe, "probe_runtime 返回完整结构")
    _check(set(probe["missing"]) == {k for k, v in probe["deps"].items() if not v},
           "missing 与 deps 自洽")
    # 两个解释器各自缺的东西不同, 故只断言"至少缺一项"（本机就是这种状态）
    _check(probe["ok"] is False,
           "本机该解释器环境**不齐备**（正是显式 L3 的触发条件）",
           f"缺 {'/'.join(probe['missing']) or '(无)'}")

    print("\n--- ①b 结构上不得回退旧模型（决策 D 核心）---")
    # 造一个"可用旧版本", 验证 force_halt 仍然不带旧权重
    vdir = os.path.join(sandbox, "drl", "20260905")
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, D.LIVE_MARKER_NAME), "w", encoding="utf-8") as f:
        f.write("{}")
    with open(os.path.join(vdir, "model.zip"), "wb") as f:
        f.write(b"PK\x03\x04x")
    with open(os.path.join(vdir, "train_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"ok": True, "final_weights": {k: 1 / 6 for k in
                  ("signal", "trend", "govern", "liquidity", "vol", "mom_rev")}}, f)
    _check(D.version_usable("20260905") is True, "前提: 沙箱内存在可用旧版本")

    print("\n--- ② 显式置 L3 + 落盘账本 ---")
    why = (f"DRL 运行环境不可用: 缺 {'/'.join(probe['missing'])}; 决策 D")
    dec = D.force_halt(args.day, why, extra={"probe": probe})
    _check(dec.get("halt") is True and dec.get("level") == D.LEVEL_HALT,
           "force_halt 返回 halt=True / level=3", f"level={dec.get('level')}")
    _check(dec.get("effective_weights") is None and dec.get("source_day") is None,
           "**不回退旧模型**（effective_weights/source_day 均为空）")
    lp = D.event_ledger_path()
    _check(os.path.isfile(lp), "降级账本已落盘", os.path.basename(lp))
    with open(lp, encoding="utf-8") as f:
        evs = [json.loads(x) for x in f if x.strip()]
    _check(len(evs) == 1, "账本记录 1 条 L3 事件")
    _check(evs and evs[0]["level"] == D.LEVEL_HALT and evs[0]["blocked_plan"] is True,
           "账本字段正确（level=3 / blocked_plan=True）")
    _check(evs and probe["missing"][0] in evs[0]["trigger"],
           "账本 trigger 记录了缺失依赖", f"{probe['missing'][0]}")

    print("\n--- ②b 多次触发都留痕（不是只记第一次）---")
    D.force_halt("2026-09-21", "缺依赖(第二天)")
    _check(D.event_count() == 2, "累计事件数 = 2", f"{D.event_count()}")

    print("\n--- ③ 可见性: 复盘/面板读取器 ---")
    cur = D.current_level()
    print(f"  current_level: level={cur['level']} {cur['level_name']} "
          f"severity={cur['severity']} blocked={cur['blocked_plan']}")
    _check(cur["level"] == D.LEVEL_HALT and cur["blocked_plan"] is True,
           "current_level 反映 L3/阻断")
    _check(cur["pointer_source"] == "halt_forced", "指针来源标记为 halt_forced",
           f"{cur['pointer_source']}")
    last = D.last_event()
    _check(last is not None and last["day"] == "20260921", "last_event 返回最近一条",
           f"{last and last.get('day')}")
    _check(isinstance(D.event_count(), int) and D.event_count() == 2, "event_count 可读")
    _check(dataguard.warned_count(D.LEVEL_WARN_KEY[3]) == 2,
           "每次 L3 都发 CRITICAL 告警（计数=2）")

    print("\n--- ③b Prometheus 指标真的被设值（告警源）---")
    try:
        import metrics_server as MS
        MS._refresh_drl_degrade()
        vals = {}
        for g in (MS._g_drl_level, MS._g_drl_blocked, MS._g_drl_events,
                  MS._g_drl_env_ok, MS._g_drl_read_ok):
            name = g._name
            vals[name] = g._value.get()
        print(f"  {vals}")
        _check(vals.get("astock_drl_degrade_level") == D.LEVEL_HALT,
               "astock_drl_degrade_level = 3", f"{vals.get('astock_drl_degrade_level')}")
        _check(vals.get("astock_drl_plan_blocked") == 1,
               "astock_drl_plan_blocked = 1")
        _check(vals.get("astock_drl_degrade_read_ok") == 1,
               "astock_drl_degrade_read_ok = 1",
               f"{vals.get('astock_drl_degrade_read_ok')}")
        _check(vals.get("astock_drl_env_ok") == 0,
               "astock_drl_env_ok = 0（缺依赖, 供 DrlEnvMissing 告警）")
        _check(vals.get("astock_drl_degrade_events") == 2,
               "astock_drl_degrade_events = 2")
    except ImportError as e:
        _check(False, "metrics_server 可导入", str(e))

    print("\n--- ①c 不生成新信号: 沙箱内不得出现当日 target_plan.json ---")
    plan = os.path.join(sandbox, "drl", day_dir, "target_plan.json")
    _check(not os.path.isfile(plan),
           "L3 当日未生成 target_plan.json（不生成新信号）", plan)

    print("\n--- ④ 只写沙箱 ---")
    if args.prod_data:
        for n, h in before.items():
            p = (os.path.join(args.prod_data, "drl", day_dir, "target_plan.json")
                 if n == "<prod plan>" else
                 os.path.join(args.prod_data, "drl_degrade_events.jsonl")
                 if n == "<prod ledger>" else os.path.join(args.prod_data, n))
            after = _md5(p)
            _check(after == h, f"生产 {n} 跑前跑后 md5 一致",
                   f"{'(不存在)' if after is None else after[:12]}")

    fails = [lbl for ok, lbl in _RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"结论: {'全部通过' if not fails else '有失败项'}  "
          f"({len(_RESULTS) - len(fails)}/{len(_RESULTS)} PASS)  py={sys.version.split()[0]}")
    for f in fails:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

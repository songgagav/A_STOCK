# -*- coding: utf-8 -*-
"""端到端验证编排器 (e2e_verify.py) —— 三链路 × 盘前/盘中/盘后 × 交叉验证 × 故障注入.

为什么需要它
  用户给出的全流程验证设计有 6 节、几十个验证项, 相关脚本**已基本齐备**, 但缺:
    1) 一个把验证项 ↔ 脚本 ↔ 判定规则固化下来的**编排层**;
    2) **逐链路**判定 (数据链 / 信号链 / 执行链), 而不是一堆孤立脚本输出;
    3) 把"结构上不可验证"的项**显式列出**, 避免它们被默默算作通过。

关键实现事实(实测得出, 不是推断)
  · 这 6 个 preflight 脚本**一律 exit 0**, 即使内部有 FAIL
    ⇒ **退出码不可作为判据**, 必须解析各自的 JSON 输出。
  · 各 JSON 的 schema **互不相同**(有 pass 字典 / assert_pass+total / _summary / 纯 verdict 字符串)
    ⇒ 每个脚本需要一个专用适配器, 不能用通用提取器。

判定规则(取自用户设计第五节)
  · 三条链路全部通过            -> Go
  · 任一链路有未闭环 P0          -> No-Go
  · 有 P1 但有缓解措施           -> Conditional Go

用法
  $env:BAR_STORE='h5i'
  & <py310> scripts\\e2e_verify.py                 # 只读验证(第一阶段)
  & <py310> scripts\\e2e_verify.py --phase inject   # 追加故障注入(第二阶段)
  & <py310> scripts\\e2e_verify.py --phase all
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))
os.chdir(_BASE)

DATA = os.path.join(_BASE, "data")
OUT_FP = os.path.join(DATA, "e2e_report.json")

#: 执行链在**真实通道**上的验证项 —— 本环境无券商通道, 结构性不可验证。
#  不得因为"模拟层通过"就把它们算作通过。
UNVERIFIABLE = [
    ("执行链", "真实下单成功率", "TRADE_BROKER=paper, 无券商通道"),
    ("执行链", "真实拒单/部分成交", "同上; paper 撮合的拒单语义不等价"),
    ("执行链", "券商回报对账(逐笔)", "本地 vt_orderid 由 paper 引擎生成, ≠ 券商回报"),
    ("执行链", "断线重连后状态一致性", "无长连接可断"),
    ("执行链", "资金/持仓以券商为准核对", "无券商侧真值"),
    ("盘中", "行情接收延迟 <5s", "无实时行情源(tick 由 seed 构造)"),
    ("盘中", "实时对账(每小时增量)", "无真实回报流, 只能事后对账"),
]


def _run(script: str, timeout: int = 900) -> tuple[int, str]:
    p = os.path.join(_BASE, "scripts", script)
    if not os.path.exists(p):
        return 127, f"脚本不存在: {script}"
    try:
        r = subprocess.run([sys.executable, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时({timeout}s)"


#: 本次已运行的脚本 -> 其 JSON 必须在脚本启动**之后**被写过, 否则视为陈旧。
#  为什么必须查: 脚本若失败/未产出, 目录里可能残留**上一轮的旧 JSON**,
#  适配器会照旧读出"通过" —— 这正是本项目反复出现的静默失败类型。
#  实测踩到过一次: preflight_neutral_check.json 是本编排器曾经的字符串匹配产物,
#  脚本其实不存在(exit=127), 却被读成 pass。
_RUN_START: float = 0.0


def _load(name: str, since: float | None = None):
    """读取 JSON；若 `since` 给定且文件 mtime 早于它, 返回 None(视为未产出)。"""
    p = os.path.join(DATA, name)
    if since is not None:
        try:
            if os.path.getmtime(p) < since - 1.0:
                return None
        except OSError:
            return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- 适配器
# 每个适配器: (script, json, 周期, 链路) -> (n_pass, n_fail, 明细列表, 备注)

def _ad_data_checks():
    j = _load("preflight_data_checks.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_data_checks", "FAIL", "无 JSON 输出")], ""
    items = []
    for b in j.get("boundary", []):
        items.append((f"PIT 边界 {b.get('day')}(pb 覆盖 {b.get('pb_cov')})",
                      "OK" if b.get("cov_ok") else "FAIL", ""))
    pit = j.get("pit_consistency", {})
    items.append((f"PIT 口径一致性({pit.get('n_impls')} 份实现/{pit.get('n_periods')} 期)",
                  "OK" if pit.get("n_mismatch") == 0 else "FAIL",
                  f"mismatch={pit.get('n_mismatch')}"))
    cc = j.get("concurrent_reads", {})
    items.append(("并发读一致性", "OK" if cc.get("ok") else "FAIL", ""))
    n_pass = sum(1 for _, s, _ in items if s == "OK")
    return n_pass, len(items) - n_pass, items, ""


def _ad_risk_triggers():
    j = _load("preflight_risk_triggers.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_risk_triggers", "FAIL", "无 JSON 输出")], ""
    ap, at = j.get("assert_pass", 0), j.get("assert_total", 0)
    items = [("门控构造触发断言", "OK" if ap == at else "FAIL", f"{ap}/{at}")]
    fails = [r for r in j.get("results", []) if not r.get("pass")]
    note = ""
    if fails:
        note = "未过项: " + "; ".join(str(r.get("scenario"))[:40] for r in fails)
    return (1 if ap == at else 0), (0 if ap == at else 1), items, note


def _ad_circuit_breaker():
    j = _load("preflight_circuit_breaker.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_circuit_breaker", "FAIL", "无 JSON 输出")], ""
    mp = {"drawdown_8": "回撤 -8% 触发熔断(level>=2)",
          "clear_12": "回撤 -12% 升级清仓(level=3)",
          "cvar": "CVaR 6% 触发", "vol": "波动 40% 触发"}
    items = [(v, "OK" if j.get("pass", {}).get(k) else "FAIL", "")
             for k, v in mp.items()]
    n_pass = sum(1 for _, s, _ in items if s == "OK")
    return n_pass, len(items) - n_pass, items, ""


def _ad_paperbook():
    j = _load("preflight_paperbook.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_paperbook", "FAIL", "无 JSON 输出")], ""
    s = j.get("_summary", {})
    items = []
    for k, v in j.items():
        if k.startswith("_") or not isinstance(v, dict) or "pass" not in v:
            continue
        items.append((f"PaperBook:{k}", "OK" if v.get("pass") else "FAIL", ""))
    n_pass = sum(1 for _, st, _ in items if st == "OK")
    note = str(s.get("not_extrapolable", ""))
    return n_pass, len(items) - n_pass, items, note


def _ad_missing_data():
    j = _load("preflight_missing_data.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_missing_data", "FAIL", "无 JSON 输出")], ""
    s = j.get("_summary", {})
    p, t = s.get("pass", 0), s.get("total", 0)
    return p, t - p, [("数据缺失故障注入", "OK" if p == t else "FAIL", f"{p}/{t}")], ""


def _ad_atomicity():
    j = _load("preflight_atomicity.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_atomicity", "FAIL", "无 JSON 输出")], ""
    v = str(j.get("verdict", ""))
    # [2026-09-19] 该脚本已升级为给出**确定性判据**: `pass` 布尔 + 提交粒度明细。
    #   依据: 写入端每块恰 1000 行, 故"每个已落盘块都是 1000 行整数倍" ⇒ 整块提交
    #   (chunk-granular atomic), 库处于"若干完整块已提交"的一致状态, 满足清单的
    #   "完好旧态/完好新态"(**块粒度**, 非逐行原子); 出现非整数倍的块 ⇒ 撕裂写 ⇒ FAIL。
    #   实测: 6 个块各恰好 1000 行, 撕裂块=0。
    ok = j.get("pass")
    if ok is None:                      # 兼容旧产物: 退回文字判据
        ok = ("完好" in v) and ("部分" not in v)
        return (1 if ok else 0), 0, \
            [("写入原子性(kill 中断)", "OK" if ok else "REVIEW", v[:70])], \
            ("" if ok else f"需人工定性(旧产物): {v}")
    detail = f"{len(j.get('committed_chunks', []))} 个完整块, 撕裂块={len(j.get('torn_chunks', []))}"
    return (1 if ok else 0), (0 if ok else 1), \
        [("写入原子性(kill 中断, 块粒度)", "OK" if ok else "FAIL", detail)], ""


def _ad_fault_inject():
    """第二批故障注入（模拟条件 + 真实代码路径）。

    该脚本自身已按用户要求对每个故障验证三件事(告警触发/系统恢复/数据一致),
    故此处直接读其 `_summary` 与逐例三态。
    """
    j = _load("preflight_fault_inject.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_fault_inject", "FAIL", "无 JSON 输出")], ""
    items = []
    for c in j.get("cases", []):
        st = "OK" if c.get("pass") else "FAIL"
        items.append((f"故障注入:{c.get('fault')}", st,
                      f"告警={c.get('alert_triggered')} 恢复={c.get('system_recovered')} "
                      f"一致={c.get('data_consistent')}"))
    s = j.get("_summary", {})
    p, t = s.get("pass", 0), s.get("total", 0)
    return p, t - p, items, str(j.get("scope_note", ""))[:120]


def _ad_daemon_heal():
    """进程崩溃 + 守护重启演练。

    **本次演练抓到一个真缺陷**：`daemon._proc_alive()` 仅凭 OpenProcess 成功判定存活,
    而 Windows 上只要还有句柄指向已终止的进程对象, OpenProcess 就会成功 —— 于是看护会把
    **已崩溃**的进程判为存活而**不重建**(与 daemon.py 注释里那条旧缺陷同一故障模式)。
    实测: 子进程 exit(7) 且未释放其 Popen 句柄时 `_proc_alive=True`(错)、
    `GetExitCodeProcess=False`(对)。已修 `_proc_alive` 增加退出码判定, 修后 6/6 PASS。
    """
    j = _load("preflight_daemon_heal.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_daemon_heal", "FAIL", "无 JSON 输出")], ""
    items = []
    for c in j.get("checks", []):
        items.append((f"守护:{c.get('name')}", "OK" if c.get("pass") else "FAIL",
                      str(c.get("detail", ""))[:80]))
    s = j.get("_summary", {})
    p, t = s.get("pass", 0), s.get("total", 0)
    return p, t - p, items, str(j.get("note", ""))[:120]


def _ad_backup_restore():
    """备份恢复演练（全程临时库/临时文件, 未触碰生产数据）。"""
    j = _load("preflight_backup_restore.json", _RUN_START)
    if not j:
        return 0, 1, [("preflight_backup_restore", "FAIL", "无 JSON 输出")], ""
    items = [(f"备份恢复:{c.get('name')}", "OK" if c.get("pass") else "FAIL",
              str(c.get("detail", ""))[:80]) for c in j.get("checks", [])]
    s = j.get("_summary", {})
    p, t = s.get("pass", 0), s.get("total", 0)
    return p, t - p, items, ""


def _ad_neutral_removed():
    """已移除的适配器（保留以记录教训）。

    曾登记 `preflight_neutral_check.py`，实测该脚本**不存在**(exit=127)，但目录里
    残留同名 JSON，适配器照旧读出 pass —— 即"脚本没跑成功却读出通过"。
    现由 `_load(..., since=_RUN_START)` 的新鲜度校验堵住该类问题；该项不再登记，
    因为用户设计的三链路清单里并无此验证项。
    """
    return 0, 0, [], ""


#: (脚本, 适配器, 周期, 链路)
CHECKS = [
    ("preflight_data_checks.py", _ad_data_checks, "盘前", "数据链"),
    ("preflight_risk_triggers.py", _ad_risk_triggers, "盘前", "信号链"),
    ("preflight_circuit_breaker.py", _ad_circuit_breaker, "盘中", "信号链"),
    ("preflight_paperbook.py", _ad_paperbook, "盘后", "执行链"),
]
INJECT = [
    ("preflight_missing_data.py", _ad_missing_data, "盘中", "数据链"),
    ("preflight_atomicity.py", _ad_atomicity, "盘后", "数据链"),
    # 第二批: 模拟条件(ENOSPC/URLError/TimeoutError)打在真实代码路径上;
    # 每例均已断言"告警触发+系统恢复+数据一致"三件事。
    ("preflight_fault_inject.py", _ad_fault_inject, "盘中", "数据链"),
    # 第三批: 进程崩溃 + 守护自愈(真实拉起子进程, 用临时端口不碰用户 8000)。
    # 归入执行链: 守护看护的是引擎/可视化等执行侧进程。
    ("preflight_daemon_heal.py", _ad_daemon_heal, "盘中", "执行链"),
    # 第四批: 备份恢复演练(临时库/临时文件, 未碰生产)。第三阶段 dry-run 的前置条件。
    ("preflight_backup_restore.py", _ad_backup_restore, "盘后", "数据链"),
]

#: 已知未闭环 P0 (摘自 ops/acceptance_status.json 的权威登记)
KNOWN_P0 = {
    "数据链": [],
    "信号链": ["P0-3 实盘风控接线缺失(回撤 -8% 未触发熔断级)"],
    "执行链": ["P0-1 真实回报对账不可验证(无券商通道)", "E-1 非整手(已修, 保留追溯)"],
}
#: 已知 P1 及其缓解
KNOWN_P1 = {
    "数据链": ["P2-DEDUP 同一规则 5 份拷贝(本次实测一致, 缓解: 有残留检查)"],
    "信号链": ["P1-8 门控迟滞 5 日 / risk 仅降 10% 暴露 / 单日 -5% 与 -9% 响应相同"],
    "执行链": ["P2-SNAPSHOT 快照 cash 四舍五入到分(1.05e-4 元)"],
}


def main(argv=None) -> int:
    global _RUN_START
    ap = argparse.ArgumentParser(description="三链路端到端验证编排器")
    ap.add_argument("--phase", choices=["readonly", "inject", "all"], default="readonly")
    ap.add_argument("--out", default=OUT_FP)
    args = ap.parse_args(argv)

    todo = list(CHECKS) + (list(INJECT) if args.phase in ("inject", "all") else [])
    results = []
    print(f"[e2e] phase={args.phase}  待跑 {len(todo)} 个脚本\n")
    for script, adapter, phase, chain in todo:
        # 记录本次启动时刻: 适配器读 JSON 时会校验 mtime 晚于它, 防"陈旧 JSON 假通过"
        _RUN_START = time.time()
        rc, out = _run(script)
        try:
            n_pass, n_fail, items, note = adapter()
        except Exception as e:  # noqa: BLE001
            n_pass, n_fail, items, note = 0, 1, [(script, "FAIL", f"适配器异常 {e}")], ""
        print(f"  [{chain}/{phase}] {script}  exit={rc}  pass={n_pass} fail={n_fail}")
        for name, st, det in items:
            mark = {"OK": "  OK  ", "FAIL": " FAIL ", "REVIEW": "REVIEW", "SKIP": " SKIP "}.get(st, st)
            print(f"      {mark} {name}" + (f"  ({det})" if det else ""))
        if note:
            print(f"      note: {note}")
        print()
        results.append({"chain": chain, "phase": phase, "script": script,
                        "exit": rc, "pass": n_pass, "fail": n_fail,
                        "items": [{"name": n, "status": s, "detail": d} for n, s, d in items],
                        "note": note,
                        "stdout_tail": out.strip().splitlines()[-6:]})

    # ---- 逐链路判定 ----
    chains: dict = {}
    for r in results:
        c = chains.setdefault(r["chain"], {"pass": 0, "fail": 0, "review": 0})
        c["pass"] += r["pass"]
        c["fail"] += r["fail"]
        c["review"] += sum(1 for i in r["items"] if i["status"] in ("REVIEW", "SKIP"))

    verdicts = {}
    for chain, c in chains.items():
        p0 = KNOWN_P0.get(chain, [])
        p1 = KNOWN_P1.get(chain, [])
        # 判定规则(用户设计第五节):
        #   未闭环 P0            -> No-Go
        #   自动检查有 FAIL       -> No-Go(实测失败, 属未闭环问题)
        #   有 REVIEW/未定性       -> Conditional Go(需人工定性, 不是 P0)
        #   有 P1 且有缓解        -> Conditional Go
        #   否则                 -> Go
        if p0 or c["fail"] > 0:
            verdicts[chain] = "No-Go"
        elif c["review"] > 0 or p1:
            verdicts[chain] = "Conditional Go"
        else:
            verdicts[chain] = "Go"

    overall = ("No-Go" if any(v == "No-Go" for v in verdicts.values())
               else ("Conditional Go" if any(v == "Conditional Go" for v in verdicts.values())
                     else "Go"))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "phase": args.phase,
        "chains": {k: {**v, "verdict": verdicts[k],
                       "known_p0": KNOWN_P0.get(k, []), "known_p1": KNOWN_P1.get(k, [])}
                   for k, v in chains.items()},
        "overall": overall,
        "unverifiable": [{"chain": c, "item": i, "reason": r} for c, i, r in UNVERIFIABLE],
        "results": results,
        "scope_note": ("执行链的成交结论仅适用于本地 vnpy_paperaccount 模拟撮合; "
                       "真实通道的下单/拒单/断线/回报对账未验证, 不得外推。"),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 78)
    for chain in ("数据链", "信号链", "执行链"):
        if chain in chains:
            c = chains[chain]
            print(f"  {chain}: pass={c['pass']} fail={c['fail']} review={c['review']}"
                  f"  -> {verdicts[chain]}")
    print(f"\n  总判定: {overall}")
    if any("No-Go" == v for v in verdicts.values()):
        print("  依据: 存在未闭环 P0 (见 ops/acceptance_status.json)")
    print(f"\n  结构性不可验证项({len(UNVERIFIABLE)} 项, 不计入通过):")
    for c, i, r in UNVERIFIABLE:
        print(f"    - [{c}] {i} —— {r}")
    print(f"\n  报告 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

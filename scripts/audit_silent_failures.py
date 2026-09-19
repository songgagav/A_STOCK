# -*- coding: utf-8 -*-
"""静默失败审计 (audit_silent_failures.py).

背景
  2026-09 连续抓到多个"静默降级": 异常被吞 / 空结果被当成正常值, 结果看起来正常其实是错的
  （见 src/dataguard.py docstring、§⑫ 的 f_ml 断链、§⑬ 的引擎回退）。
  逐个靠人眼翻效率低且必漏, 故用 AST 扫描: 找出**catch 之后既不留日志、也不抛、也不留痕**
  的处理器, 再按"是否在决策链路"分级。

判定"有声"的记号(任一命中即不算静默):
  raise / print / sys.stderr / logging.<level> / _LOG.<level> / logger.<level>
  / warn / warn_once / guard_ / dataguard / reason= (把原因写进返回值)
  / 赋值给 *_error / *_reason / err

用法
    python scripts/audit_silent_failures.py            # 全 src/ 扫描 + 按分级汇总
    python scripts/audit_silent_failures.py --all      # 连非决策链路一起列明细
输出
    data/silent_failure_audit.json
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_BASE, "src")
OUT = os.path.join(_BASE, "data", "silent_failure_audit.json")

#: 决策链路(其静默失败会改变实盘选股/配权/风控结果) —— 高优先级
DECISION_PATH = [
    "selector.py", "factor_fusion.py", "factor_gate.py", "factor_library.py",
    "target_weighting.py", "risk_first.py", "realtime_engine.py", "db.py",
    "vnpy_backtest.py", "ml_fusion_bridge.py", "fml_accumulate.py",
    "factor_dynamic_weights.py", "factor_mad.py", "fusion_daily_validate.py",
]
#: 数据链路(影响回测/数据完整性, 次高)
DATA_PATH = [
    "valuation_backfill.py", "rebuild_financials.py", "h5i_bar_store.py",
    "local_pull.py", "update_db.py", "pool_snapshot.py",
]

_NOISE_MARKERS = ("raise", "print", "warn", "guard_", "dataguard", "reason",
                  "error", "err", "stderr")


def _call_name(node: ast.AST) -> str:
    """把 Call 节点渲染成 'obj.attr' 形式的名字."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):        # logging.getLogger(...).warning(...)
        return _call_name(cur.func) + "()"
    return ".".join(reversed(parts))


def _is_noisy(handler: ast.ExceptHandler) -> tuple[bool, list[str]]:
    marks: list[str] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            marks.append("raise")
        elif isinstance(node, ast.Call):
            nm = _call_name(node.func)
            low = nm.lower()
            if any(k in low for k in ("log", "warn", "print", "guard")):
                marks.append(nm)
            if "debug" in low or "info" in low:      # debug/info 在盘后日志里常被过滤
                marks.append(f"low-level:{nm}")
        elif isinstance(node, ast.Name):
            if any(k in node.id.lower() for k in ("err", "reason", "warning")):
                marks.append(f"name:{node.id}")
        elif isinstance(node, ast.Attribute):
            if any(k in node.attr.lower() for k in ("err", "reason")):
                marks.append(f"attr:{node.attr}")
    # 只有 debug/info 级别日志的, 视为"弱留痕"(盘后 INFO 常常没落盘)
    strong = [m for m in marks if m != "raise" and not m.startswith("low-level:")]
    return bool(strong or "raise" in marks), marks


def audit(path: str, mod: str) -> list[dict]:
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"module": mod, "line": 0, "kind": "parse_error", "detail": str(e)}]
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            for h in node.handlers:
                noisy, marks = _is_noisy(h)
                if noisy:
                    continue
                body_kinds = sorted({type(s).__name__ for s in h.body})
                out.append({
                    "module": mod, "line": h.lineno, "func": fn.name,
                    "exc": (_call_name(h.type) if h.type is not None else "bare"),
                    "body": body_kinds, "marks": marks,
                    "silent_kind": ("pass" if body_kinds == ["Pass"] else
                                    "return_empty" if any(
                                        isinstance(s, ast.Return) for s in h.body)
                                    else "other"),
                })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连非决策链路一起列明细")
    a = ap.parse_args()

    findings = []
    for f in sorted(os.listdir(SRC)):
        if f.endswith(".py"):
            findings += audit(os.path.join(SRC, f), f)
    # 子包
    for sub in ("factor_mine",):
        d = os.path.join(SRC, sub)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".py"):
                    findings += audit(os.path.join(d, f), f"{sub}/{f}")

    def bucket(m):
        return ("decision" if m in DECISION_PATH else
                "data" if m in DATA_PATH else "other")

    for x in findings:
        x["bucket"] = bucket(x["module"])
    order = {"decision": 0, "data": 1, "other": 2}
    findings.sort(key=lambda x: (order[x["bucket"]], x["module"], x["line"]))
    counts = {b: sum(1 for x in findings if x["bucket"] == b)
              for b in ("decision", "data", "other")}
    print(f"静默 except 处理器: 决策链路 {counts['decision']} / 数据链路 {counts['data']}"
          f" / 其它 {counts['other']}  (合计 {len(findings)})\n")
    for b, title in (("decision", "【决策链路 — 会改变实盘选股/配权/风控】"),
                     ("data", "【数据链路】"), ("other", "【其它】")):
        if b == "other" and not a.all:
            continue
        xs = [x for x in findings if x["bucket"] == b]
        if not xs:
            continue
        print(f"{title} {len(xs)} 处")
        shown = xs if b != "other" else xs[:40]
        for x in shown:
            print(f"  {x['module']}:{x['line']}  {x['func']}()  except {x['exc']}"
                  f"  -> {x['body']}")
        if len(xs) > len(shown):
            print(f"  … 其余 {len(xs) - len(shown)} 处见 JSON")
        print()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"counts": counts, "findings": findings}, f,
                  ensure_ascii=False, indent=2)
    print("已保存:", OUT)


if __name__ == "__main__":
    main()

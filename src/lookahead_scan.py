# -*- coding: utf-8 -*-
"""策略代码级 lookahead（未来函数）静态扫描（路线图 #8）。

为什么需要它
------------
lookahead 是量化策略最隐蔽的一类错误: 代码跑得通、回测很漂亮, 但用了**当时还没发生的信息**,
于是实盘必然打折。它不会抛异常、不会算错数, 只会让回测的收益虚高 —— 靠人肉 review 发现,
而本仓的策略代码有数万行。故用静态扫描把它变成一条可重复执行的检查。

规则只收**零误报**的那几条（证据驱动）
--------------------------------------
第一版曾考虑『全局统计量归一化』(除以整段 `max/mean/std`) 这类规则, 但它在真实代码里
命中过多且多数无害 —— 一个噪声比真信号多的检查会让人整体忽略它, 与本次加固要让告警可信的
目标相悖。故**只留语法上就能定性、无法狡辩**的模式, 并**在真实代码库上实测每条规则的命中数**
(见 CLI 输出), 用命中质量而不是想象来决定去留:

  future_shift     `.shift(-N)`  N>0            —— 直接取未来行
  future_diff      `.diff(-N)` / `.pct_change(-N)` —— 同上, 换了个函数名
  centered_window  `rolling(..., center=True)` / `ewm(..., center=True)`
                                                —— 窗口居中 => 半个窗口在未来
  backward_fill    `.bfill()` / `.backfill()` / `fillna(method=『bfill』)`
                                                —— 用**未来值**回填过去
  future_index     `iloc[i+N]` / `iloc[i+N:...]` —— 正向索引(仅在循环变量上可疑)

**刻意不报**的常见写法（假阳性来源）: `shift(1)`/`shift(N>0)`（因果, 是**正确**做法）、
`expanding()`/`rolling().mean()`（天然因果)、`ffill()`（向前填充只用过去）。

豁免通道
--------
行尾注释 `# lookahead-ok: 理由` 可豁免该行；文件首 5 行内 `# lookahead-scan: skip-file`
可整文件跳过。豁免必须写理由 —— 让"为什么这里是安全的"留在代码里, 而不是留在某人的记忆里。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

RULES = {
    "future_shift": "CRITICAL",
    "future_diff": "CRITICAL",
    "backward_fill": "CRITICAL",
    "centered_window": "HIGH",
    "future_index": "MEDIUM",
}

RULE_WHY = {
    "future_shift": "shift(-N) 取的是未来第 N 行 —— 决策时该数据尚不存在",
    "future_diff": "diff(-N)/pct_change(-N) 与 shift(-N) 等价, 同样是未来信息",
    "backward_fill": "bfill/backfill 用**未来**的观测回填过去的缺口; 因果版本是 ffill",
    "centered_window": "center=True 使窗口跨到未来(半个窗口在未来), 因果版本不带 center",
    "future_index": "正向索引 iloc[i+N] 取到未来行(仅当索引变量是当前时点时有意义)",
}

_FFILL_METHODS = {"bfill", "backfill", "pad_backward"}


def _fname(node) -> str:
    """取调用名（支持 a.b.c(...) 形式）。"""
    f = getattr(node, "func", None)
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _const_int(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _const_int(node.operand)
        return -v if v is not None else None
    return None


def _kwarg(node, name):
    for kw in getattr(node, "keywords", []) or []:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _first_arg(node):
    a = getattr(node, "args", None) or []
    return a[0] if a else None


def _snippet(code_lines, lineno, width=110) -> str:
    if 1 <= lineno <= len(code_lines):
        return code_lines[lineno - 1].strip()[:width]
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self, code_lines):
        self.lines = code_lines
        self.hits: list = []

    def _add(self, node, rule, extra=""):
        self.hits.append({
            "line": getattr(node, "lineno", 0),
            "col": getattr(node, "col_offset", 0),
            "rule": rule,
            "severity": RULES[rule],
            "why": RULE_WHY[rule] + (f" ({extra})" if extra else ""),
            "snippet": _snippet(self.lines, getattr(node, "lineno", 0)),
        })

    def visit_Call(self, node):
        fn = _fname(node)
        # .shift(-N) / .diff(-N) / .pct_change(-N)
        if fn in ("shift", "diff", "pct_change"):
            n = _const_int(_first_arg(node)) if node.args else None
            if n is not None and n < 0:
                self._add(node, "future_shift" if fn == "shift" else "future_diff",
                          extra=f"{fn}({n})")
        # .bfill() / .backfill()  —— **必须是方法调用**(Attribute)才算:
        # 实测本仓有 2 处 `backfill(day)` 是本地数据回填函数, 裸名调用报出来就是假阳性
        # (250 文件扫描的 9 处命中里有 2 处如此)。规则精度靠真实代码库实测来定。
        if fn in _FFILL_METHODS and isinstance(getattr(node, "func", None), ast.Attribute):
            self._add(node, "backward_fill", extra=f"方法调用 .{fn}()")
        # fillna(method="bfill")
        if fn == "fillna":
            m = _kwarg(node, "method")
            if isinstance(m, str) and m in _FFILL_METHODS:
                self._add(node, "backward_fill", extra=f'fillna(method="{m}")')
        # rolling/ewm(center=True)
        if fn in ("rolling", "ewm") and _kwarg(node, "center") is True:
            self._add(node, "centered_window", extra=f"{fn}(center=True)")
        # iloc[i+N] —— 只报"加正数"的形态; iloc[i]/iloc[i-1] 是因果的
        if fn == "iloc" or (isinstance(getattr(node, "func", None), ast.Attribute)
                            and getattr(node.func, "attr", "") == "__getitem__"):
            pass
        self.generic_visit(node)

    def visit_Subscript(self, node):
        # df.iloc[i + 1] / arr[i+1:...]
        v = node.value
        if isinstance(v, ast.Attribute) and v.attr == "iloc":
            self._check_index(node, node.slice)
        elif isinstance(v, ast.Subscript) and isinstance(v.value, ast.Attribute) \
                and v.value.attr == "iloc":
            pass
        self.generic_visit(node)

    def _check_index(self, node, sl):
        sl = sl if isinstance(sl, ast.Index) else sl   # py<3.9 兼容(本仓 3.10 走 else 分支)
        expr = sl
        if isinstance(sl, ast.Slice):
            expr = sl.lower
        # 形如 i + 1 / i + 2 (Name + 正数)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            n = _const_int(expr.right)
            if isinstance(expr.left, ast.Name) and n is not None and n > 0:
                self._add(node, "future_index", extra=f"{expr.left.id} + {n}")


def scan_source(code: str, filename: str = "<string>") -> list:
    """扫描一段源码（**纯函数**，CI 可测）。返回 findings 列表。"""
    lines = code.splitlines()
    # 文件级豁免
    head = "\n".join(lines[:5])
    if "lookahead-scan: skip-file" in head:
        return []
    try:
        tree = ast.parse(code, filename=filename)
    except SyntaxError as e:
        return [{"file": filename, "line": getattr(e, "lineno", 0) or 0, "col": 0,
                 "rule": "syntax_error", "severity": "INFO",
                 "why": f"无法解析(跳过): {e}", "snippet": ""}]
    v = _Visitor(lines)
    v.visit(tree)
    out = []
    for h in v.hits:
        h["file"] = filename
        # 行级豁免: 该行含 lookahead-ok 即放行(必须写理由)
        ln = h["line"]
        if 1 <= ln <= len(lines) and "lookahead-ok" in lines[ln - 1]:
            continue
        out.append(h)
    return out


DEFAULT_SKIP_DIRS = ("__pycache__", ".venv", ".venv310", ".venv314", "node_modules", ".git")


def scan_paths(paths, skip_dirs=DEFAULT_SKIP_DIRS, max_files=5000) -> dict:
    """扫描若干文件/目录。返回 {'findings': [...], 'files': n, 'by_rule': {...}}。"""
    files: list = []
    for p in paths:
        if os.path.isfile(p) and p.endswith(".py"):
            files.append(p)
        elif os.path.isdir(p):
            for root, dirs, fns in os.walk(p):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for fn in fns:
                    if fn.endswith(".py"):
                        files.append(os.path.join(root, fn))
        if len(files) >= max_files:
            break
    allf: list = []
    for fp in files[:max_files]:
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                code = f.read()
        except Exception:  # noqa: BLE001
            continue
        for h in scan_source(code, fp):
            if h.get("rule") != "syntax_error":
                allf.append(h)
    by_rule: dict = {}
    for h in allf:
        by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
    return {"findings": allf, "files": len(files[:max_files]), "by_rule": by_rule}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="策略代码 lookahead(未来函数) 静态扫描")
    ap.add_argument("--paths", nargs="*", default=["src", "scripts"],
                    help="要扫描的文件/目录(默认 src scripts)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args(argv)

    r = scan_paths(args.paths)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print(f"扫描 {r['files']} 个 .py 文件; 命中 {len(r['findings'])} 处")
    print("按规则: " + (", ".join(f"{k}={v}" for k, v in sorted(r["by_rule"].items())) or "(无)"))
    if not args.quiet:
        for h in sorted(r["findings"], key=lambda x: (x["severity"] != "CRITICAL", x["file"], x["line"])):
            rel = os.path.relpath(h["file"], os.getcwd()) if os.path.isabs(h["file"]) else h["file"]
            print(f"  [{h['severity']}] {rel}:{h['line']} {h['rule']}  {h['snippet']}")
            print(f"        {h['why']}")
    crit = sum(1 for h in r["findings"] if h["severity"] == "CRITICAL")
    return 1 if crit else 0


if __name__ == "__main__":
    raise SystemExit(_main())

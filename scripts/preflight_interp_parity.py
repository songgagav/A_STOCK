# -*- coding: utf-8 -*-
"""解释器过渡验证：同一交易日、同一份输入，用**两个解释器**各跑一次 dry-run 并逐项对比.

用途（用户 2026-09-20 批准的"观察性过渡"第一天）
    从 `.venv314`（3.14）切到 `.venv310`（3.10）不只是 DRL 路径变了 —— **所有** Python
    代码的运行环境都变了（numpy/pandas/torch 版本、浮点行为、异常栈格式）。故切换前必须
    证明"同一输入下两边的**决策与信号**一致，差异都能解释"。

设计要点
    · 复用 `scripts/preflight_dryrun_day.py`（它已具备：沙箱 DATA_DIR + 种入 daily/drl 输入
      + 生产 state.json/live_state.json 的 md5 前后比对）。用 `DRYRUN_DATA_DIR` 给每次运行
      独立沙箱, 故两个解释器互不干扰。
    · dry-run 实际跑的是 `realtime_engine.load_targets()`（**决策/档位**）与
      `RealtimeEngine.run_tick()`（**信号/撮合/状态回写**）—— 正是要对比的两件事。
    · 对比**结构化产物**而不是 stdout 文本: 文本会因库版本改变 warning/栈格式, 直接 diff
      会产生大量噪声。文本只做"归一化后的补充对比"。

差异三分类（这是"差异可解释"的落地方式）
    1. VOLATILE   —— 天然可变字段（时间戳/耗时/临时路径/运行 id）: 忽略, 但**计数并列出**
    2. FLOAT_TOL  —— 两侧都是浮点且相对差 ≤ 容差: 视为等价（跨版本浮点格式化/末位差异）, 计数
    3. SEMANTIC   —— 其余一切: **必须为 0**, 否则判 FAIL 并逐条打印

用法
    python scripts/preflight_interp_parity.py \
        --a-py .venv310/Scripts/python.exe --b-py .venv314/Scripts/python.exe \
        --date 2026-09-08
退出码: 0 = 无 SEMANTIC 差异且生产未被触碰; 1 = 有
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DRYRUN = os.path.join(_HERE, "preflight_dryrun_day.py")

#: 天然可变字段（忽略但记账）
VOLATILE_KEYS = {
    "generated_at", "updated_at", "at", "tmp_data_dir", "total_seconds",
    "seconds", "tmp_artifacts", "pid", "run_id", "last_seen", "mtime",
    "checked_at", "started_at", "finished_at", "duration", "elapsed",
    "prod_state_md5_before", "prod_state_md5_after", "prod_state_untouched",
}
#: 浮点相对容差: ≤ 此值视为"跨版本浮点末位差异", 可解释
FLOAT_REL_TOL = 1e-9
#: 文本归一化：时间戳 / 耗时 / 临时路径 / 十六进制
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?")
_SEC = re.compile(r"\d+(?:\.\d+)?\s*(?:s\b|sec\b|seconds)")
_TMP = re.compile(r"(?:[A-Za-z]:\\|[A-Za-z]:/)[^\s\"']*(?:astock_dryrun_|_interp_parity_)[^\s\"']*")
_HEX = re.compile(r"\b[0-9a-f]{16,}\b")
#: 值形态判定用：整串就是时间戳
_TS_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$")


def _md5(p: str) -> "str | None":
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _norm_log(text: str, sandbox: str) -> "list[str]":
    """日志归一化: 去掉时间戳/耗时/沙箱路径/长十六进制, 再去空行与行尾空白。"""
    t = _TS.sub("<TS>", text or "")
    t = _SEC.sub("<SEC>", t)
    t = _TMP.sub("<TMP>", t)
    if sandbox:
        t = t.replace(sandbox, "<SANDBOX>").replace(sandbox.replace("\\", "/"), "<SANDBOX>")
    t = _HEX.sub("<HEX>", t)
    return [ln.rstrip() for ln in t.splitlines() if ln.strip()]


def _diff(a, b, path="$", out=None):
    """递归结构化对比, 把差异追加进 out 并带分类。"""
    if out is None:
        out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}"
            if k not in a:
                out.append(("SEMANTIC", p, "<缺>", b[k]))
            elif k not in b:
                out.append(("SEMANTIC", p, a[k], "<缺>"))
            elif k in VOLATILE_KEYS:
                if a[k] != b[k]:
                    out.append(("VOLATILE", p, a[k], b[k]))
            else:
                _diff(a[k], b[k], p, out)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(("SEMANTIC", f"{path}.len", len(a), len(b)))
        for i in range(min(len(a), len(b))):
            _diff(a[i], b[i], f"{path}[{i}]", out)
        return out
    if a == b:
        return out
    # 时间戳形态: 按**值的形态**判, 而不是靠把键名一个个列进 VOLATILE_KEYS。
    # 教训: 初版只按键名判, 于是 `live_state.updated` / `data_ts` 这类"键名没列到"
    # 的墙钟时间戳被误报成 SEMANTIC（实测 A/B 相差 6 秒 = 两次运行的真实时刻差）。
    if isinstance(a, str) and isinstance(b, str) \
            and _TS_FULL.match(a) and _TS_FULL.match(b):
        out.append(("TIMESTAMP", path, a, b))
        return out
    # 浮点容差
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        fa, fb = float(a), float(b)
        denom = max(abs(fa), abs(fb), 1e-12)
        if abs(fa - fb) / denom <= FLOAT_REL_TOL:
            out.append(("FLOAT_TOL", path, a, b))
            return out
    out.append(("SEMANTIC", path, a, b))
    return out


def _run_one(py: str, date: str, tag: str) -> dict:
    """在独立沙箱里用指定解释器跑一次 dry-run, 收集结构化产物。"""
    sandbox = tempfile.mkdtemp(prefix=f"_interp_parity_{tag}_")
    env = dict(os.environ)
    env["DRYRUN_DATA_DIR"] = sandbox
    env["QUANT_DATA_DIR"] = sandbox
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("H5I_MARKET_DB", None)      # 让 h5i 用仓库默认路径（生产库, 只读）
    cmd = [py, _DRYRUN, "--date", date]
    t0 = dt.datetime.now()
    pr = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, cwd=_ROOT, timeout=1800)
    dur = (dt.datetime.now() - t0).total_seconds()

    def _read(rel):
        p = os.path.join(sandbox, rel)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return {"<unparsable>": p}

    def _read_jsonl(rel):
        p = os.path.join(sandbox, rel)
        if not os.path.isfile(p):
            return []
        out = []
        with open(p, encoding="utf-8-sig") as f:
            for ln in f:
                if ln.strip():
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
        return out

    return {
        "tag": tag, "py": py, "sandbox": sandbox, "rc": pr.returncode,
        "seconds": round(dur, 2),
        "stdout": pr.stdout or "", "stderr": pr.stderr or "",
        "report": _read("dryrun_report.json"),
        "state": _read("state.json"),
        "live_state": _read("live_state.json"),
        "targets_trace": _read_jsonl("targets_source.jsonl"),
    }


def _degrade_reasons(res: dict) -> dict:
    """抽出**降级/异常原因** —— 这是结构化产物对比**看不到**的关键信息。

    为什么必须单独抽: 实测两侧都可能落到同一个 `used=fml_fallback`（故结构化对比 0 差异），
    但**原因截然不同** —— 一侧是真实的数据覆盖不足, 另一侧是"读不到主数据源(h5i_db)"。
    "输出相同、原因不同"在过渡期是最危险的一类差异: 看输出会以为没问题。
    """
    txt = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    reasons = []
    for ln in txt.splitlines():
        if "降级" in ln and "used=" in ln:
            reasons.append(ln.strip())
    missing = sorted(set(re.findall(r"ModuleNotFoundError: No module named '([^']+)'", txt)))
    imports = sorted(set(re.findall(r"ImportError: ([^\n]{0,60})", txt)))
    return {"degrade_lines": reasons, "missing_modules": missing, "import_errors": imports}


def _decision_of(res: dict) -> dict:
    """抽出"决策"字段（档位/来源日/标的数）—— 这是必须逐位一致的部分。"""
    rep = res.get("report") or {}
    steps = rep.get("steps") or []
    pool = next((s for s in steps if s.get("stage") == "盘前/池构造"), {}) or {}
    tr = res.get("targets_trace") or []
    last = tr[-1] if tr else {}
    return {
        "rung": pool.get("rung"),
        "sel_day": pool.get("sel_day"),
        "n_targets": pool.get("n_targets"),
        "trace_rung": last.get("rung"),
        "trace_n": len(last.get("targets") or []) if isinstance(last.get("targets"), list) else None,
        "error": rep.get("error"),
        "freeze_gate_exists": (rep.get("signal_freeze_gate_0925") or {}).get("exists"),
        "prod_untouched": rep.get("prod_state_untouched"),
    }


def _reason_key(line: str) -> str:
    """把一条降级日志压成"原因类别", 用于判断两侧降级是否**同类**。

    例: `... reason=fusion 覆盖不足: n_sc=7 ...` 与 `... reason=fusion 异常: ModuleNotFoundError ...`
    应判为**不同类** —— 前者是数据问题, 后者是环境问题。
    """
    m = re.search(r"reason=([^\n]*)", line)
    body = m.group(1) if m else line
    for cat in ("覆盖不足", "样本少", "异常", "缺失", "超时"):
        if cat in body:
            return cat
    return body.strip()[:40]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a-py", required=True, help="解释器 A（建议 .venv310）")
    ap.add_argument("--b-py", required=True, help="解释器 B（建议 .venv314）")
    ap.add_argument("--date", required=True, help="同一交易日 YYYY-MM-DD")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--keep", action="store_true", help="保留沙箱以便人工查看")
    args = ap.parse_args()

    a_py = os.path.abspath(args.a_py)
    b_py = os.path.abspath(args.b_py)
    for p in (a_py, b_py):
        if not os.path.isfile(p):
            print(f"[FAIL] 解释器不存在: {p}")
            return 1

    prod = os.path.join(_ROOT, "data")
    watch = ["state.json", "live_state.json", "preflight_dryrun.json"]
    before = {n: _md5(os.path.join(prod, n)) for n in watch}

    print("=" * 78)
    print("解释器过渡验证：同输入 · 双解释器 dry-run 对比")
    print("=" * 78)
    print(f"解释器 A : {a_py}")
    print(f"解释器 B : {b_py}")
    print(f"交易日   : {args.date}")

    resA = _run_one(a_py, args.date, "A")
    resB = _run_one(b_py, args.date, "B")
    for r in (resA, resB):
        print(f"\n[解释器 {r['tag']}] rc={r['rc']} 耗时={r['seconds']}s sandbox={r['sandbox']}")

    # ---------------- 决策对比 ----------------
    dA, dB = _decision_of(resA), _decision_of(resB)
    print("\n--- ① 决策（档位 / 来源日 / 标的数）---")
    for k in dA:
        mark = "OK " if dA[k] == dB[k] else "DIFF"
        print(f"  [{mark}] {k:20} A={dA[k]!r:28} B={dB[k]!r}")
    dec_same = all(dA[k] == dB[k] for k in dA)

    # ---------------- 结构化产物对比 ----------------
    print("\n--- ② 结构化产物（逐字段）---")
    all_diffs = []
    for name in ("report", "state", "live_state", "targets_trace"):
        d = _diff(resA.get(name), resB.get(name), path=f"$.{name}")
        # report 里干跑自己的 md5/耗时属 VOLATILE, 已在 _diff 内归类
        all_diffs.extend((name,) + tuple(x) for x in d)
    sem = [x for x in all_diffs if x[1] == "SEMANTIC"]
    flt = [x for x in all_diffs if x[1] == "FLOAT_TOL"]
    vol = [x for x in all_diffs if x[1] == "VOLATILE"]
    tsp = [x for x in all_diffs if x[1] == "TIMESTAMP"]
    print(f"  SEMANTIC={len(sem)}  FLOAT_TOL={len(flt)}  VOLATILE={len(vol)}  "
          f"TIMESTAMP={len(tsp)}")
    for grp, label in ((sem, "SEMANTIC（必须为 0）"), (flt, "FLOAT_TOL（可解释）"),
                       (tsp, "TIMESTAMP（墙钟, 可解释）"), (vol, "VOLATILE（可忽略）")):
        for src, cls, path, a, b in grp[:12]:
            print(f"    [{cls}] {src}{path[1:]}  A={a!r}  B={b!r}")
        if len(grp) > 12:
            print(f"    ...（{label} 共 {len(grp)} 条, 只列前 12）")

    # ---------------- 日志对比（归一化）----------------
    print("\n--- ③ 日志（归一化后）---")
    lA = _norm_log(resA["stdout"] + resA["stderr"], resA["sandbox"])
    lB = _norm_log(resB["stdout"] + resB["stderr"], resB["sandbox"])
    sA, sB = set(lA), set(lB)
    only_a, only_b = sorted(sA - sB), sorted(sB - sA)
    print(f"  归一化行数: A={len(lA)} B={len(lB)}  仅在A={len(only_a)}  仅在B={len(only_b)}")
    for ln in only_a[:8]:
        print(f"    [仅A] {ln[:150]}")
    for ln in only_b[:8]:
        print(f"    [仅B] {ln[:150]}")

    # ---------------- 降级/异常原因对比（结构化对比看不到的部分）----------------
    # 用户要求: `degrade_reason_mismatch` 作为**长期监控项, 每次 dry-run 都输出**。
    # 故本段**无条件**打印（没有降级也要明确说"两侧均无降级"），而不是只在有降级时才出现
    # —— "这次没打印"和"这次没降级"必须能被区分开, 否则监控项本身会静默消失。
    print("\n--- ③b 降级/异常**原因**对比（结构化产物看不到; 长期监控项）---")
    gA, gB = _degrade_reasons(resA), _degrade_reasons(resB)
    for tag, g in (("A", gA), ("B", gB)):
        print(f"  [{tag}] 缺模块={g['missing_modules'] or '无'}  "
              f"降级行数={len(g['degrade_lines'])}")
        for ln in g["degrade_lines"][:4]:
            print(f"      {ln[:160]}")
    reason_mismatch = False
    if gA["degrade_lines"] or gB["degrade_lines"]:
        keyA = sorted(_reason_key(x) for x in gA["degrade_lines"])
        keyB = sorted(_reason_key(x) for x in gB["degrade_lines"])
        reason_mismatch = keyA != keyB
        print(f"  degrade_reason_mismatch = {reason_mismatch}"
              f"  ({'两侧降级**原因不同**' if reason_mismatch else '两侧降级原因同类'};"
              f" A={keyA or '无'} B={keyB or '无'})")
    else:
        print("  degrade_reason_mismatch = False  (两侧均无降级, 本项无差异可比)")
    if (gA["missing_modules"] or gB["missing_modules"]):
        print(f"  ⚠ 有解释器缺模块: A={gA['missing_modules'] or '无'} "
              f"B={gB['missing_modules'] or '无'}")
        print("     ⇒ 该侧运行在**降级模式**: 输出可能仍相同, 但那是因为它绕过了主数据源。")

    # ---------------- 生产文件 ----------------
    print("\n--- ④ 生产文件（受保护的两个 + dry-run 摘要）---")
    after = {n: _md5(os.path.join(prod, n)) for n in watch}
    for n in watch:
        ok = before[n] == after[n]
        tag = "PROTECTED" if n in ("state.json", "live_state.json") else "by-design 会写"
        print(f"  [{'OK ' if ok else 'CHANGED'}] {n:24} {tag}  "
              f"{'(不存在)' if after[n] is None else after[n][:12]}")
    protected_ok = all(before[n] == after[n] for n in ("state.json", "live_state.json"))

    # ---------------- 结论 ----------------
    print("\n" + "=" * 78)
    verdict_ok = dec_same and not sem and protected_ok
    print(f"决策一致        : {dec_same}")
    print(f"SEMANTIC 差异   : {len(sem)}  (要求 0)")
    print(f"degrade_reason_mismatch : {reason_mismatch}"
          f"{'   <<< 输出相同但降级原因不同, 需人工确认' if reason_mismatch else ''}")
    print(f"受保护生产文件  : {'未变' if protected_ok else '**被改写**'}")
    print(f"结论: {'通过 —— 差异均可解释' if verdict_ok else '不通过 —— 见上方 SEMANTIC/决策差异'}")
    if reason_mismatch:
        print("WARN: 两侧降级**原因不同** —— 输出恰好相同不等于行为相同, "
              "切换后实际走的数据路径会改变(见 ③b)。")
    print("=" * 78)

    if args.json_out:
        payload = {
            "date": args.date, "a_py": a_py, "b_py": b_py,
            "verdict_ok": verdict_ok, "decision_same": dec_same,
            "decision": {"A": dA, "B": dB},
            "n_semantic": len(sem), "n_float_tol": len(flt), "n_volatile": len(vol),
            "n_timestamp": len(tsp),
            "semantic": [{"src": s, "path": p, "A": repr(a), "B": repr(b)}
                         for s, _c, p, a, b in sem],
            "log_only_a": only_a[:50], "log_only_b": only_b[:50],
            "degrade_reason_mismatch": reason_mismatch,
            "degrade": {"A": gA, "B": gB},
            "protected_untouched": protected_ok,
            "seconds": {"A": resA["seconds"], "B": resB["seconds"]},
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"报告 -> {args.json_out}")

    if not args.keep:
        for r in (resA, resB):
            shutil.rmtree(r["sandbox"], ignore_errors=True)
    else:
        print(f"沙箱保留: {resA['sandbox']} / {resB['sandbox']}")

    return 0 if verdict_ok else 1


if __name__ == "__main__":
    sys.exit(main())

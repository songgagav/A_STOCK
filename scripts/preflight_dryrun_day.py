# -*- coding: utf-8 -*-
"""完整交易日 dry-run（盘前池消费 → 盘中撮合 → 状态回写），全程写临时目录.

安全设计（本脚本的第一职责是"证明自己没碰生产"）
  · 在 **import config 之前** 设 `QUANT_DATA_DIR=<临时目录>` ⇒ 状态/回执/快照全部落到临时目录;
  · 行情仍读真实 h5i（`_H5I_PATH` 不由 DATA_DIR 派生 —— 已实测两组读取结果一致）;
  · 运行前后对生产 `data/state.json` 与 `data/live_state.json` 做 **md5 比对**,
    任一变则整次 dry-run 判 FAIL（这是最关键的一条断言）;
  · 结束后核对临时目录里究竟产生了哪些产物。

观察项（用户指定）：**信号生成完成时间**，用于验证 09:25 冻结缺口
  · 记录 load_targets 落点档位（同源/跨日回退/现场选股）与耗时;
  · 记录信号/评分阶段耗时;
  · 报告是否存在 09:25 之类的硬截止（全仓搜索结论为"无"）。

用法
  & <py310> scripts\\preflight_dryrun_day.py
输出
  <QUANT_DATA_DIR>/dryrun_report.json 与 data/preflight_dryrun.json（只写结论摘要, 不含持仓明细）
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 1) 先设环境
TMP_DATA = os.environ.get("DRYRUN_DATA_DIR") or tempfile.mkdtemp(prefix="astock_dryrun_")
os.makedirs(TMP_DATA, exist_ok=True)
os.environ["QUANT_DATA_DIR"] = TMP_DATA          # **必须在 import config 之前**
os.environ.setdefault("BAR_STORE", "h5i")

PROD_STATE = os.path.join(_BASE, "data", "state.json")
PROD_LIVE = os.path.join(_BASE, "data", "live_state.json")
SUMMARY_OUT = os.path.join(_BASE, "data", "preflight_dryrun.json")


def _md5(p):
    try:
        return hashlib.md5(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


def _tree(root):
    out = []
    for r, _d, fs in os.walk(root):
        for f in fs:
            fp = os.path.join(r, f)
            out.append((os.path.relpath(fp, root), os.path.getsize(fp)))
    return sorted(out)


def main() -> int:
    before = {"state.json": _md5(PROD_STATE), "live_state.json": _md5(PROD_LIVE)}
    print(f"[dry-run] 临时数据目录: {TMP_DATA}")
    print(f"[dry-run] 生产 state.json md5(前) = {before['state.json']}")
    print(f"[dry-run] 生产 live_state md5(前) = {before['live_state.json']}\n")

    sys.path.insert(0, os.path.join(_BASE, "src"))
    os.chdir(_BASE)

    import config
    print(f"[dry-run] config.DATA_DIR = {config.DATA_DIR}")
    assert os.path.abspath(config.DATA_DIR) == os.path.abspath(TMP_DATA), \
        "QUANT_DATA_DIR 未生效, 中止(否则会写生产状态)"

    steps: list[dict] = []
    err = None
    t_all0 = time.time()
    try:
        import realtime_engine as RE

        # ---- 盘前：池构造（耗时 + 落点档位）----
        day = datetime.now().strftime("%Y-%m-%d")
        t0 = time.time()
        targets, sel_info, sel_day = RE.load_targets(day)
        dt_pool = time.time() - t0
        rung = None
        trace_fp = os.path.join(TMP_DATA, "targets_source.jsonl")
        if os.path.exists(trace_fp):
            try:
                last = [json.loads(x) for x in open(trace_fp, encoding="utf-8") if x.strip()][-1]
                rung = last
            except Exception:  # noqa: BLE001
                pass
        steps.append({"stage": "盘前/池构造", "seconds": round(dt_pool, 3),
                      "n_targets": len(targets or []), "sel_day": sel_day,
                      "rung": (rung or {}).get("rung")})
        print(f"  盘前池构造: {len(targets or [])} 只, 来源日={sel_day}, "
              f"档位={(rung or {}).get('rung')}, 耗时 {dt_pool:.2f}s")

        # ---- 盘中：一次 tick（撮合 + 风控 + 状态回写）----
        t0 = time.time()
        eng = RE.RealtimeEngine(interval=0.0, intraday_only=True)
        eng.run_tick()
        dt_tick = time.time() - t0
        steps.append({"stage": "盘中/单次tick", "seconds": round(dt_tick, 3)})
        print(f"  盘中单次 tick: 耗时 {dt_tick:.2f}s")
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        steps.append({"stage": "异常", "error": err})
        print("  异常:", err)
        traceback.print_exc(limit=3)

    dt_total = time.time() - t_all0
    after = {"state.json": _md5(PROD_STATE), "live_state.json": _md5(PROD_LIVE)}
    print(f"\n[dry-run] 生产 state.json md5(后) = {after['state.json']}")
    print(f"[dry-run] 生产 live_state md5(后) = {after['live_state.json']}")

    prod_untouched = (before == after)
    print(f"[dry-run] **生产状态未被触碰**: {prod_untouched}"
          f"{'' if prod_untouched else '  <<< FAIL: 生产文件被改写!'}")

    arts = _tree(TMP_DATA)
    print(f"\n[dry-run] 临时目录产物 {len(arts)} 个:")
    for rel, sz in arts[:20]:
        print(f"      {rel}  ({sz} B)")

    # ---- 观察项：09:25 冻结缺口 ----
    import subprocess
    src_dir = os.path.join(_BASE, "src")
    grep = subprocess.run(
        ["findstr", "/S", "/I", "/C:09:25", "/C:freeze_signal", "/C:signal_freeze",
         "/C:hard_deadline", os.path.join(src_dir, "*.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    gate_hits = [l for l in (grep.stdout or "").splitlines() if l.strip()]
    has_gate = bool(gate_hits)
    print(f"\n[dry-run] 09:25 冻结闸门搜索: 命中 {len(gate_hits)} 行 -> "
          f"{'存在' if has_gate else '**不存在（缺口确认）**'}")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tmp_data_dir": TMP_DATA,
        "prod_state_md5_before": before, "prod_state_md5_after": after,
        "prod_state_untouched": prod_untouched,
        "total_seconds": round(dt_total, 2),
        "steps": steps,
        "tmp_artifacts": [{"path": p, "bytes": s} for p, s in arts],
        "signal_freeze_gate_0925": {"exists": has_gate, "hits": gate_hits[:5]},
        "error": err,
    }
    # 临时目录内放完整报告
    try:
        with open(os.path.join(TMP_DATA, "dryrun_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(os.path.dirname(SUMMARY_OUT), exist_ok=True)
    # 仓库内只留摘要(含 md5 与耗时), 不含持仓明细
    with open(SUMMARY_OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[dry-run] 摘要 -> {SUMMARY_OUT}")
    print(f"[dry-run] 完整报告 -> {os.path.join(TMP_DATA, 'dryrun_report.json')}")
    return 0 if prod_untouched else 1


if __name__ == "__main__":
    raise SystemExit(main())

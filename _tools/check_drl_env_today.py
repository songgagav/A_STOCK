"""一次性诊断: 今天 run_daily 里 DRL 环境的**实际**结论是什么 (2026-09-25)。

为什么要写这个: 我此前把 `DrlEnvMissing` 记为「已知结构性误报」, 而今天
`astock_drl_env_ok` = 1 且 `drl_train.ok` = true ⇒ 训练**真的跑了**。
在改任何文档之前, 必须先把"到底哪个解释器在跑、环境是否真的齐备"查实,
否则会把一个**已修复**的项继续标成"已知误报"(反向的静默错误)。

输出写文件 + 显式 utf-8, 避免控制台 GBK 编码把结论字符吞掉。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_drl_env_report.txt")
lines: list[str] = []


def p(s: str = "") -> None:
    lines.append(s)


# --- 1. run_daily 记下的 drl_degrade 步骤 -------------------------------------
fp = os.path.join("data", "daily", "20260925", "daily_summary.json")
d = json.load(open(fp, encoding="utf-8"))
st = d["steps"]
p("=== run_daily step: drl_degrade ===")
p(json.dumps(st.get("drl_degrade"), ensure_ascii=False, indent=2))
p()
p("=== run_daily step: drl_train (keys only) ===")
dt = st.get("drl_train") or {}
p("ok = %r" % dt.get("ok"))
p("keys = %s" % ", ".join(sorted(dt.keys())))
p()

# --- 2. 三个解释器的依赖齐备性 -----------------------------------------------
p("=== interpreter probe (find_spec, 与 probe_runtime 同法) ===")
REQ = ("h5i_db", "torch", "gymnasium", "stable_baselines3")
p("this process: %s (py %s)" % (sys.executable, sys.version.split()[0]))
import importlib.util as iu  # noqa: E402

have = []
for m in REQ:
    try:
        ok = iu.find_spec(m) is not None
    except Exception:  # noqa: BLE001
        ok = False
    have.append("%s=%s" % (m, ok))
p("  " + "  ".join(have))

# --- 3. drl_degrade 账本里有没有"环境缺失"类事件 ------------------------------
p()
p("=== drl_degrade_events.jsonl kinds ===")
lfp = os.path.join("data", "drl_degrade_events.jsonl")
if os.path.exists(lfp):
    from collections import Counter

    kinds: Counter = Counter()
    for ln in open(lfp, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        kinds[str(r.get("kind"))] += 1
    for k, v in kinds.most_common():
        p("  %-28s %d" % (k, v))
else:
    p("  (no ledger)")

# --- 4. 今天的 DRL 产物 ------------------------------------------------------
p()
p("=== today's drl artifacts ===")
dd = os.path.join("data", "drl", "20260925")
if os.path.isdir(dd):
    for n in sorted(os.listdir(dd)):
        f = os.path.join(dd, n)
        p("  %-34s %8d bytes" % (n, os.path.getsize(f)))
else:
    p("  (missing dir)")

# --- 5. 关键判据: 训练产物里用的解释器 ---------------------------------------
p()
p("=== heartbeat content (谁跑的) ===")
for n in ("drl_train_heartbeat.json", "pre_drl_brief_heartbeat.json"):
    f = os.path.join(dd, n)
    if os.path.exists(f):
        p("-- %s" % n)
        p(json.dumps(json.load(open(f, encoding="utf-8")), ensure_ascii=False, indent=2))

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written:", OUT)

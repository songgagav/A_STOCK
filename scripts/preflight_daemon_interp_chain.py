# -*- coding: utf-8 -*-
"""模拟 daemon 的『孙进程』解释器链, 验证 run_daily 会拿到具备依赖的解释器.

背景: `.venv310\\Scripts\\python.exe` 是 virtualenv 的**桩**, 它会 spawn 一个
base Python310 的子进程(实测 daemon 因此有两个 pid)。而 `daemon.py` 用
`PY = sys.executable` 启动 `run_daily` —— 若该链上任何一环丢掉了 venv 上下文,
子进程就会**没有 h5i_db/torch**, 即 P0-DRLDEP 那类失败(且是静默降级)。
故这里**实测**而不是推理: 从 venv 解释器出发再 spawn 一层, 看它是否仍具备四项依赖。
"""
from __future__ import annotations

import json
import subprocess
import sys

NEED = ("h5i_db", "torch", "gymnasium", "stable_baselines3")

CHILD = (
    "import sys, importlib.util as u, json\n"
    "print(json.dumps({'exe': sys.executable, 'prefix': sys.prefix,\n"
    "                  'deps': {m: bool(u.find_spec(m)) for m in %r}}))\n" % (NEED,)
)

print("  本进程 sys.executable =", sys.executable)
print("  本进程 sys.prefix     =", sys.prefix)

r = subprocess.run([sys.executable, "-c", CHILD], capture_output=True, text=True)
print("  孙进程 returncode     =", r.returncode)
if r.stdout.strip():
    info = json.loads(r.stdout.strip())
    print("  孙进程 sys.executable =", info["exe"])
    print("  孙进程 sys.prefix     =", info["prefix"])
    for m, ok in info["deps"].items():
        print("  孙进程 %-20s %s" % (m, ok))
    missing = [m for m, ok in info["deps"].items() if not ok]
    print()
    if missing:
        print("  [FAIL] 孙进程缺依赖:", missing, "-> daemon 启动 run_daily 会静默降级")
        sys.exit(1)
    print("  [PASS] 孙进程四项依赖齐备 —— daemon 的 PY 链可用")
else:
    print("  孙进程 stderr         =", (r.stderr or "").strip()[:500])
    sys.exit(2)

# -*- coding: utf-8 -*-
"""一次性: 把测试里「源码短语 + 魔数窗口」换成 `code_block_bounds`(按结构定界)。

覆盖 10 处(分布在 8 个测试文件)。这些**不是**文档守卫, 而是"看某段源码之后
有没有某件事"的守卫 —— 窗口长度同样是拍的, 失效方向同样**不报错**:
太短则静默看不到, 太长则被下一段代码满足。
"""
from __future__ import annotations

import io
import re

TARGETS = [
    "tests/test_drl_degrade.py",
    "tests/test_round2_wiring.py",
    "tests/test_daemon_heal_and_deadman.py",
    "tests/test_datasource_gate.py",
    "tests/test_push_state.py",
    "tests/test_stockdb_patrol.py",
    "tests/test_engine_lag_alerting.py",
    "tests/test_drl_metrics.py",
    "tests/test_drl_degrade_env_gate.py",
    "tests/test_factor_weight_env_bounds.py",
]

# 形态 1: `X in src[i:i + N]`  ->  `X in src[i:code_block_bounds(src, i)]`
pat1 = re.compile(r'src\[i:i \+ \d+\]')
# 形态 2: `src[i_none:i_none + N]`
pat2 = re.compile(r'src\[(?P<v>i_\w+):(?P=v) \+ \d+\]')
# 形态 3: `hblk = src[j:j + N]` / `seg = src[k:k + N]`
pat3 = re.compile(r'src\[(?P<v>[a-z_]+):(?P=v) \+ \d+\]')

total = 0
for fp in TARGETS:
    src = io.open(fp, encoding="utf-8").read()
    orig = src
    src, n1 = pat1.subn("src[i:code_block_bounds(src, i)]", src)
    src, n2 = pat2.subn(lambda m: f"src[{m.group('v')}:code_block_bounds(src, {m.group('v')})]", src)
    src, n3 = pat3.subn(lambda m: f"src[{m.group('v')}:code_block_bounds(src, {m.group('v')})]", src)
    n = n1 + n2 + n3
    if src != orig:
        # 确保 import 了 code_block_bounds
        if "code_block_bounds" not in src.split("class ")[0]:
            m = re.search(r'^(import pytest\n)', src, re.M)
            if m:
                src = src[:m.end()] + (
                    "\nfrom doc_section import code_block_bounds  # noqa: E402  "
                    "按**结构**定界, 取代 src[i:i+N] 的魔数窗口\n"
                ) + src[m.end():]
            else:
                # 退而求其次: 在最后一个顶层 import 之后插
                m2 = list(re.finditer(r'^(?:import|from) .*\n', src, re.M))[-1]
                src = src[:m2.end()] + (
                    "from doc_section import code_block_bounds  # noqa: E402\n"
                ) + src[m2.end():]
        io.open(fp, "w", encoding="utf-8", newline="\n").write(src)
    total += n
    print(f"{fp:48} converted={n}")

print("TOTAL:", total)

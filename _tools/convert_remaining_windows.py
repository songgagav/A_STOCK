# -*- coding: utf-8 -*-
"""一次性: 把 `test_discipline_doc.py` 里剩余的「正文短语 + 魔数窗口」收敛。

## 策略(与前三版返工的区别)

只做**能机械判定为安全**的两种改写, 其余留给人工:

| 形态 | 处置 |
|---|---|
| `i = src.find("### H")` + `src[i:i+N]` | → `_sec(src, "### H")` —— 锚点是标题 |
| `i = src.find("正文X")` + `src[i:i+N]`, **且后面紧跟 `j = src.find("### H", i)`** | → 用 `section_bounds` 取该节, 再 `line_of` 在其中定位 X |
| 其余(纯正文短语 + 窗口) | **不动** —— 判据不充分, 强改会重演"14 条失败" |

最后一项是刻意的: 本轮**不追求"全仓零窗口"**, 追求"**改对**"。
"""
from __future__ import annotations

import io
import re

FP = "tests/test_discipline_doc.py"
src = io.open(FP, encoding="utf-8").read()
orig = src

# ---- 形态 A: 标题锚点 + 魔数窗口 -> _sec ----
patA = re.compile(
    r'(?P<ind>[ \t]*)i = src\.find\((?P<h>"#[^"]*")(?:, (?P<base>[^)\n]+))?\)\n'
    r'(?:[ \t]*assert [^\n]*\n)*?'
    r'[ \t]*(?P<v>block|blk|sec) = src\[i:i \+ \d+\]\n'
)


def replA(m):
    ind, h, base, v = m.group("ind"), m.group("h"), m.group("base"), m.group("v")
    args = f"{h}, {base}" if base else h
    return f'{ind}{v} = _sec(src, {args})\n'


src, nA = patA.subn(replA, src)

# ---- 形态 B: 正文短语 + 窗口, 且**同一函数内**出现带标题的第二个 find ----
# 这种就是"在某一节里找短语"的写法: 改成 section_bounds + line_of。
patB = re.compile(
    r'(?P<ind>[ \t]*)i = src\.find\((?P<x>"[^"#][^"]*")\)\n'
    r'[ \t]*(?P<v>block|blk|sec) = src\[i:i \+ \d+\]\n'
    r'(?P<mid>(?:(?!\n[ \t]*(?:def |class )).)*?)'      # 到下一个 def/class 之前
    r'[ \t]*j = src\.find\((?P<h>"#[^"]*"), i\)\n',
    re.S,
)


def replB(m):
    ind, x, v, mid, h = (m.group("ind"), m.group("x"), m.group("v"),
                         m.group("mid"), m.group("h"))
    # 重写: 先取该节, 再在节内定位短语
    return (f'{ind}_a, _b = section_bounds(src, {h})\n'
            f'{ind}i = line_of(src, {x}, _a, _b)\n'
            f'{ind}{v} = src[i:_b]\n'
            f'{mid}'
            f'{ind}j = _b\n')


src, nB = patB.subn(replB, src)

if src != orig:
    io.open(FP, "w", encoding="utf-8", newline="\n").write(src)

print("formA (heading + magic window):", nA)
print("formB (phrase in section):     ", nB)
print("changed:", src != orig)

left = re.findall(r'src\[i:i \+ \d+\]', src)
left_blk = re.findall(r'src\[(?:i|k|idx):(?:i|k|idx) \+ \d+\]', src)
print("remaining magic windows:", len(left_blk))

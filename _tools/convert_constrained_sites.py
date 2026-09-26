# -*- coding: utf-8 -*-
"""一次性: 收敛剩余几处"用标题偏移 + `find(正文短语, 起点)`"的写法。

这些**不是**被劫持的高危形态(它们已经把起点约束在标题之后), 但仍有两个缺陷:
  · 起点来自 `src.find("## 环境不可用项")` —— 标题改名 ⇒ 返回 -1 ⇒
    起点变 0 或负偏移, 切片静默变形(④b);
  · 右边界是**魔数窗口**(`i + 3200`) —— 小节写长后断言会**看不到**后面的内容。

改为 `section_bounds` 后: 起点行首锚定、右边界由文档结构决定、找不到时抛错。
"""
from __future__ import annotations

import io
import re

FP = "tests/test_discipline_doc.py"
src = io.open(FP, encoding="utf-8").read()
orig = src

# 形态: i = src.find("X", src.find("## H"))  /  block = src[i:i + N]
pat = re.compile(
    r'(?P<ind>[ \t]*)i = src\.find\((?P<x>"[^"]*"), src\.find\((?P<h>"## [^"]*")\)\)\n'
    r'[ \t]*(?P<v>block|blk) = src\[i:i \+ \d+\]\n'
)


def repl(m):
    ind, x, h, v = m.group("ind"), m.group("x"), m.group("h"), m.group("v")
    return (f'{ind}_a, _b = section_bounds(src, {h})\n'
            f'{ind}i = line_of(src, {x}, _a, _b)\n'
            f'{ind}{v} = src[i:_b]\n')


src, n = pat.subn(repl, src)
if src != orig:
    io.open(FP, "w", encoding="utf-8", newline="\n").write(src)
print("converted (constrained phrase in section):", n)
print("changed:", src != orig)

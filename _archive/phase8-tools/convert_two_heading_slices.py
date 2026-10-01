# -*- coding: utf-8 -*-
"""一次性: 把「标题锚定 + 第二个 find 当右边界」的取证写法, 换成 `section_bounds`。

## 为什么必须换 (DISC-2 形态 ④b「边界腐烂」)

原写法:

```python
i = src.find("### 失效形态索引")
j = src.find("### 详细判据", i)
idx = src[i:j]
```

**右边界是"下一个标题的首次出现"** —— 于是:

· 标题一改名 ⇒ `j = -1` ⇒ `src[i:-1]` 取到**几乎整篇**, Python **不报错**;
· 我写文档时**引用**了 `### 详细判据` 这个字面量(作例子),
  于是 `find` 命中那个引用 ⇒ 右边界提前 ⇒ 区间被**截短**。

`section_bounds` 两个问题都解决: 它按**行首标题**找, 且自动止于
**同级或更高级的下一个标题**(不需要第二个 `find`), 找不到时**抛错**。

## [三次返工的教训, 记在这里]

前两版脚本都**只按代码形状**批量替换:
· 第一版把 `src.find("正文里的一句话")` 也换掉 ⇒ 锚点不是标题 ⇒ 14 条守卫失败;
· 第二版"回退"同样粗暴, 把正确的也退掉 ⇒ 白做。
⇒ **批量改写必须先问「这个字面量是标题还是正文」**, 不能只看形状。
本版只处理 `src[i:j]`(j 来自第二个 find)这一种**明确是"取两标题之间"**的形态。
"""
from __future__ import annotations

import io
import re

FP = "tests/test_discipline_doc.py"
src = io.open(FP, encoding="utf-8").read()
orig = src

# i = src.find("### A")          或  i = src.find("### A", base)
# j = src.find("### B", i)       或  j = src.find(SOME_CONST, i)
# X = src[i:j]
pat = re.compile(
    r'(?P<ind>[ \t]*)i = src\.find\((?P<h>"[^"]*")(?:, (?P<base>[^)\n]+))?\)\n'
    r'[ \t]*j = src\.find\((?P<j>[^)\n]+), i\)\n'
    r'[ \t]*(?P<v>\w+) = src\[i:j\]\n'
)


def repl(m):
    ind = m.group("ind")
    h = m.group("h")
    if not h.startswith('"#'):
        return m.group(0)          # 锚点不是标题 ⇒ 不动
    base = m.group("base")
    v = m.group("v")
    args = f"{h}, {base}" if base else h
    return (f'{ind}a, b = section_bounds(src, {args})\n'
            f'{ind}{v} = src[a:b]\n')


src, n = pat.subn(repl, src)
if src != orig:
    io.open(FP, "w", encoding="utf-8", newline="\n").write(src)
print("converted (two-heading slices):", n)
print("changed:", src != orig)

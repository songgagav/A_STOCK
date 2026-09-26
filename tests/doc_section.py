# -*- coding: utf-8 -*-
"""按**小节标题行首锚定**取文档区间 —— 根治「用某句关键话的首次出现定位一节」。

## 为什么需要它 (2026-09-26, 本会话第 5 次「守卫自指涉」后立的工具)

多个文档守卫要"在某一节里取证", 而它们原先的写法是:

```python
i = src.find("宁可 None, 不猜")      # 用某句关键话定位 DISC-1 那一节
block = src[i:i + 2600]              # 再取一个**任意长度**的窗口
```

这个写法有**两个**独立的缺陷, 都会让守卫静默失效:

1. **锚点会被"引用"劫持** —— 写文档时引用某节的关键句是极自然的冲动。
   一旦引用出现在**更靠前**的位置, `find` 就命中引用, 守卫从此读的是
   **别处的文字**, 却以为在读那一节。实测: 我在新增小节里引用了 DISC-1 的
   原则原句 ⇒ 两条 DISC-1 守卫**同时失败**, 而 DISC-1 一个字都没改,
   且报错理由指向**错误的方向**(「像是 DISC-1 的内容缺了」)。
2. **窗口长度是拍的** —— `i + 2600` 这类魔数: 该节一旦写长, 断言
   会**默默看不到**后面的内容(不是失败, 是**看不到**)。

## 本工具的判据

- `section_bounds(src, heading)` 返回该标题**行首锚定**的 `[起, 止)` 区间;
- 起止都用**行首匹配**, 故**不会被句子中间或引用里的同名字样命中**;
- 区间**止于同级或更高级的下一个标题**, 故长度由文档结构决定, 不是魔数。

## 用法

```python
from doc_section import section_bounds, line_of

a, b = section_bounds(src, "### 同族纪律: 留痕字段")
seg = src[a:b]                       # 该节全文, 不多不少
i = line_of(src, "事后追溯", a, b)   # 在该节内定位一句话(可选)
```

`heading` 可给**完整标题行**或**行首前缀**(如 `"### ⑤ "` / `"#### ③b"`)。
"""
from __future__ import annotations

__all__ = ["section_bounds", "line_of", "headings_in", "find_unique",
           "line_index", "scan_lines"]


def line_index(src: str, off: int) -> int:
    """把字符偏移换算成**行号**(0-based)。"""
    return src.count("\n", 0, off)


def scan_lines(src: str, needle: str) -> list:
    """按**行**找含 `needle` 的行, 返回 `[(行号, 行文本), ...]`。

    ## 为什么需要它 (2026-09-26 三次教训的收敛)

    比 `src.count(needle)` / `src.find(needle)` 更适合"**检查某种行存在**"这类断言:

    · `count` 数的是**子串**, 于是"引用该串的说明文字"也被计入 ⇒
      我写文档时引用了 `### 详细判据` 作例子, `count` 变 2,
      把一条"标题唯一"的守卫**误报**了;
    · `find` 只给**首次**出现位置, 于是"更靠前的引用"会劫持它 ⇒
      两条 DISC-1 守卫以**误导性理由**失败。

    按行扫描两者都避免 —— 且能顺带断言"命中了几行", 这是比"子串出现几次"
    更贴近"文档结构"的判据。
    """
    out = []
    for n, ln in enumerate(src.splitlines()):
        if needle in ln:
            out.append((n, ln))
    return out


def _iter_heading_lines(src: str):
    """产出 `(字符偏移, 行文本)` —— 只含**行首**的 Markdown 标题行。

    排除三类"看起来像标题但不是":
      · **围栏代码块**(``` 之间)里的 `#` —— 那是 shell 注释。实测踩过:
        `# 写: .NET 直写...` 被当成 L1 标题 ⇒ **提前结束 `## DISC-1:` 区间**,
        于是 DISC-1 的守卫在错误的区间里取证;
      · **引用行**(`>` 开头) —— 引用里的小节标题不是真小节;
      · 行内出现的 `###`(表格格子里、句子中间)。
    """
    off = 0
    in_fence = False
    for raw in src.splitlines(keepends=True):
        line = raw.rstrip("\n").rstrip("\r")
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("#") and not stripped.startswith(">"):
            # 必须是 `#` 开头且紧跟 `#`/空白, 排除 `#tag` 这种
            j = 0
            while j < len(line) and line[j] == "#":
                j += 1
            if j <= 6 and (j == len(line) or line[j] == " "):
                yield off, line
        off += len(raw)


def headings_in(src: str) -> list:
    """返回**全部**行首标题的 `(偏移, 级别, 行文本)`。"""
    out = []
    for off, line in _iter_heading_lines(src):
        lvl = len(line) - len(line.lstrip("#"))
        out.append((off, lvl, line))
    return out


def section_bounds(src: str, heading: str, start: int = 0) -> tuple:
    """返回标题 `heading` 所辖小节的 `(start, end)` 字符区间。

    **止于同级或更高级的下一个标题** ⇒ 区间长度由文档结构决定。

    未找到时抛 `AssertionError`(**不返回 -1**) —— 这正是本工具要根治的第二件事:
    `str.find` 用 `-1` 表示"没找到", 而 `-1` 在切片里是**合法的负索引**,
    于是"找不到"被静默翻译成"取到文末"(见 DISC-2 形态 ④b 边界腐烂)。
    """
    hits = [(off, lvl, line) for off, lvl, line in headings_in(src)
            if off >= start and line.startswith(heading)]
    assert hits, (
        f"找不到行首标题 {heading!r} —— 注意判据是**行首前缀匹配**: "
        f"句子中间或引用里的同名字样**不算**。(start={start})")
    # 精确优先: 若 heading 本身就是完整标题行, 取行文本完全相等的那一个
    exact = [h for h in hits if h[2] == heading]
    off0, lvl0, line0 = (exact or hits)[0]
    end = len(src)
    for off, lvl, line in headings_in(src):
        if off > off0 and lvl <= lvl0:
            end = off
            break
    return off0, end


def find_unique(src: str, needle: str) -> int:
    """返回 `needle` 的唯一定位; 出现 0 次或多次都抛错。

    用于"锚点句必须唯一"的场合 —— 与 `section_bounds` 互补:
    `section_bounds` 根治"按句子定位", 本函数用于确实要靠句子定位、
    但必须先证明它唯一的场合。
    """
    n = src.count(needle)
    assert n == 1, (
        f"锚点 {needle!r} 在文档里出现 {n} 次(要求恰好 1 次)—— "
        f"多处出现时, 用 find() 定位会命中**更靠前的那个**, 静默读错地方")
    return src.find(needle)


def line_of(src: str, needle: str, lo: int = 0, hi: int = None) -> int:
    """在 `[lo, hi)` 内定位 `needle`, 返回其绝对偏移; 找不到抛错。

    比 `src.find(needle)` 多一层**范围约束** —— 若它跑到了别的节里, 直接失败,
    而不是让后续断言在错误的区域里取证。
    """
    if hi is None:
        hi = len(src)
    i = src.find(needle, lo, hi)
    assert i >= 0, (
        f"在区间 [{lo}, {hi}) 内找不到 {needle!r} —— "
        f"它可能被移到了别的小节(这类失败此前表现为「断言内容缺失」, 方向错误)")
    return i

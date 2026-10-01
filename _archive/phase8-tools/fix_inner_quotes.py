# -*- coding: utf-8 -*-
"""一次性: 修 `_tools/register_rootcause.py` 里**嵌在字符串内部**的半角引号对。

## 判据(与已删除的两个失败工具的区别)

前两次失败: 我按"引号前后有中文"或"成对且 >2 个"判, 结果把**结构引号**
(`"id": "X"` 的引号)也换了 —— 因为结构引号也可能紧邻中文。

**本次判据**: 只处理**行内引号总数 >= 4** 的行, 且
**跳过行首那一个与行尾那一个**(它们才是结构定界符), 中间的一对一对换 `「」`。

但这对 `"key": "value"`(4 个引号, 但分属两对结构) 仍会误伤。
故**再加一条**: 仅当该行的**第二、三个引号之间含中文**(即它们是"内层引用"的候选)时,
才替换**第二与第三个**。这样:
· `"所有 target_plan 都被"校验未过"跳过"),` → 第 2/3 个引号之间是 `校验未过`(中文) ✅ 替换;
· `"id": "FINDING-...",`                   → 第 2/3 个之间是 `: `(非中文) ❌ 跳过。

这是**可判定**的, 不是猜。
"""
from __future__ import annotations

import io
import sys


def cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" for ch in s)


def fix_line(ln: str) -> tuple[str, int]:
    """反复处理: 每轮只改**最靠左**的一对"中间含中文"的相邻引号。

    **配对规则(为什么不是简单循环)**: 一行的引号序列形如

        Q0 ... Sn Q1  Cn  Q2  Se ... Qk

    这是 `"...Sn\"Cn\"Se..."` 的**扁平化**写法 —— 嵌套的 `\\"` 与结构引号
    在源码里是同一个字符, 无法用括号匹配还原。可靠的经验规则是:

      * `Q1`(第 2 个引号)与 `Q2`(第 3 个引号)之间是**内层引用内容**;
      * 最外层结构引号是 `Q0` 与 `Qk`(k = 引号总数 - 1);
      * 于是**只改 Q1/Q2 这一对**, 绝不碰 Q0/Qk。

    但一行里可能有不止一对外层引号(实例: 一行同时含
    `..."当日正式计划"** ... "回测≠实盘"...`)。所以按"处理完一对就
    重新扫描"的方式**从左到右推进**: 每一轮只在**剩余引号序列**上
    取 Q1/Q2, 且要求它们之间含中文。

    **为什么必须反复**(而不是只处理第一对): 留下的第二对仍会截断
    字符串, 编译照样失败 —— 而且报错行看上去"已经改过了", 更难查。

    **不能做的事**: 不能在没有中文夹心时也替换。例如
    `print("skip (已存在):", e["id"])` 的 Q1/Q2 之间是
    `, e[` —— 没有中文, 说明这是**并列的两个参数**, 不是嵌套引用。
    早期版本(从左到右只要含中文就换)会把结构引号吃掉, 险些写坏文件。
    """
    changed = 0
    while True:
        pos = [i for i, ch in enumerate(ln) if ch == '"']
        if len(pos) < 3:
            break
        a, b = pos[1], pos[2]
        if not cjk(ln[a + 1:b]):
            break
        chars = list(ln)
        chars[a] = "「"
        chars[b] = "」"
        ln = "".join(chars)
        changed += 1
    return ln, changed


def main() -> None:
    apply = "--apply" in sys.argv
    fp = [a for a in sys.argv[1:] if not a.startswith("--")][0]
    src = io.open(fp, encoding="utf-8").read()
    out, n = [], 0
    for ln in src.split("\n"):
        f, k = fix_line(ln)
        if k:
            n += k
            print("  -", ln.strip()[:100])
            print("  +", f.strip()[:100])
        out.append(f)
    print(f"{fp}: {n} 行")
    if apply and n:
        io.open(fp, "w", encoding="utf-8", newline="\n").write("\n".join(out))
        print("  已写盘")


if __name__ == "__main__":
    main()

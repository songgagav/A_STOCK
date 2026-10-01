# -*- coding: utf-8 -*-
"""`fix_cjk_quotes`: 自动把**中文串内嵌的半角引号**改成 `「」` —— DISC-4 的修复端。

## 为什么需要"修复端"(而不只是"校验端")

DISC-4 的原始教训是: **守卫能拦住错误, 但拦不住低效的修复循环。**
`ast.parse` 每次都拦住了中文半角引号, 但我仍然"手改一处、再跑、再报下一处",
因为**报错行通常不是肇事行**。

`safe_write.py` 解决的是**生成**场景(先校验再落盘, 一次收敛)。
但**编辑既有文件**时没有这层 —— 我本会话在编辑 `tests/` 与 `src/` 时又栽了两次。
故补上修复端: 给定文件, 自动定位并替换, 反复直到 `ast.parse` 通过。

## 判据(为什么"紧贴 CJK"是安全信号)

形如 `..."中文"中文"...` 的位置上, 那对引号**不可能**是合法的字符串界定符 ——
若它是界定符, 其后紧跟的中文会变成裸标识符而报错。故替换安全。

## 用法

```
python _tools/fix_cjk_quotes.py <file.py> [--dry-run]
```

也可作为库: `fix_file(path) -> (n_fixed, ok)`。
"""
from __future__ import annotations

import ast
import re

#: 形如: 非空白/非分隔符 + "CJK…" + 非空白/非分隔符
#: 只在**已判定语法错误**后才应用, 且只作用于报错行。
PAIR = re.compile(r'(?<=[^\s(,=\[{' + "'" + r'"])"([\u4e00-\u9fff][^"\n]*?)"(?=[^\s),:\]};；。])')


def _fix_line(line: str) -> tuple[str, int]:
    n = 0

    def _sub(m):
        nonlocal n
        n += 1
        return "「" + m.group(1) + "」"

    return PAIR.sub(_sub, line), n


def fix_text(src: str, *, filename: str = "<text>", max_rounds: int = 80):
    """反复修直到 `ast.parse` 通过。返回 `(new_src, n_fixed, ok)`。

    **只动 `ast.parse` 报错的那一行** —— 因此不会误改正常的字符串界定符。
    """
    try:
        ast.parse(src, filename=filename)
        return src, 0, True
    except SyntaxError:
        pass
    lines = src.split("\n")
    total = 0
    for _ in range(max_rounds):
        try:
            ast.parse("\n".join(lines), filename=filename)
            return "\n".join(lines), total, True
        except SyntaxError as e:
            ln = e.lineno or 1
            if not (1 <= ln <= len(lines)):
                return "\n".join(lines), total, False
            new, n = _fix_line(lines[ln - 1])
            if not n:
                return "\n".join(lines), total, False   # 该行修不动, 交给人
            lines[ln - 1] = new
            total += n
    return "\n".join(lines), total, False


def fix_file(path: str, *, dry_run: bool = False) -> tuple[int, bool]:
    src = open(path, encoding="utf-8").read()
    new, n, ok = fix_text(src, filename=path)
    if n and not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    return n, ok


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="把中文串内嵌的半角引号改成 「」 直到 ast.parse 通过(DISC-4 修复端)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    bad = 0
    for fp in args.files:
        try:
            n, ok = fix_file(fp, dry_run=args.dry_run)
        except OSError as e:
            print(f"  FAIL {fp}: {e}")
            bad += 1
            continue
        if ok:
            print(f"  {'(dry) ' if args.dry_run else ''}OK   {fp}  修了 {n} 处")
        else:
            print(f"  FAIL {fp}  修了 {n} 处但**仍未通过** —— 需人工看")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_main())

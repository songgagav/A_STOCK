# -*- coding: utf-8 -*-
"""`write_python_safely`: **先校验, 再落盘** —— DISC-4 的执行机制。

## 为什么需要它(DISC-4 的由来)

本会话我**连续五次**栽在同一处: 在中文串里手写半角引号 `"…"`, 把 Python 字符串
字面量**提前截断**。`ast.parse` 每次都在运行前拦住了它 —— 但拦住之后我仍然是
**手改一处、再跑、再报下一处**, 因为"报错行"通常**不是肇事行**
(截断后解析器要往后找闭合, 报错位置常落在不相干的下一行)。

**元教训: 守卫能拦住错误, 但拦不住低效的修复循环。**

正确做法是把校验**移进生成过程**: 内容拼好 -> `ast.parse` -> **通过才落盘**。
这样一次收敛, 而不是 N 轮往返。

## 用法

```python
import sys, os
sys.path.insert(0, os.path.join(REPO, "_tools"))
from safe_write import write_python

write_python(path, source)          # 语法不过就抛, 且**不落盘**
write_python(path, source, allow_overwrite=False)   # 已存在则拒
```

也提供 CLI:

```
python _tools/safe_write.py <file.py>     # 只校验已存在的文件
```

## 为什么不放进 `src/`

它不是运行时能力, 是**开发期工具**; 放 `src/` 会混进生产导入面。
本仓约定: `_tools/` = 诊断与开发辅助。
"""
from __future__ import annotations

import ast
import os
import sys


class PythonSyntaxInvalid(ValueError):
    """内容不是合法 Python。**落盘不会发生**。"""


def validate_python(source: str, *, filename: str = "<generated>") -> None:
    """校验语法; 不通过抛 `PythonSyntaxInvalid`(带**定位提示**)。"""
    try:
        ast.parse(source, filename=filename)
    except SyntaxError as e:
        lines = source.split("\n")
        ln = e.lineno or 1
        lo = max(1, ln - 3)
        hi = min(len(lines), ln + 2)
        ctx = "\n".join(
            f"{'>>' if i == ln else '  '} {i:>4}: {lines[i - 1][:110]}"
            for i in range(lo, hi + 1))
        raise PythonSyntaxInvalid(
            f"{filename} 第 {ln} 行语法错误: {e.msg}\n"
            f"**注意: 报错行常不是肇事行** —— 多半是前面某个字符串字面量被"
            f"**中文里的半角引号**提前截断(改用 「」 或转义)。上下文:\n{ctx}"
        ) from e


def write_python(path: str, source: str, *, allow_overwrite: bool = True,
                 encoding: str = "utf-8") -> str:
    """**先 `ast.parse`, 通过才落盘**。返回写入的路径。

    语法不通过时**不写任何东西**(不像"先写再修"—— 那会留下一个坏文件,
    而坏文件可能被别的进程导入/执行)。
    """
    if not allow_overwrite and os.path.exists(path):
        raise FileExistsError(f"{path} 已存在(allow_overwrite=False)")
    validate_python(source, filename=os.path.basename(path))
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding=encoding) as f:
        f.write(source)
    return path


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DISC-4 工具: 生成 .py 前先校验语法")
    ap.add_argument("files", nargs="*", help="待校验的 .py 文件")
    args = ap.parse_args(argv)

    bad = 0
    for fp in args.files:
        try:
            validate_python(open(fp, encoding="utf-8").read(), filename=fp)
            print(f"  OK   {fp}")
        except PythonSyntaxInvalid as e:
            bad += 1
            print(f"  FAIL {fp}\n{e}")
        except OSError as e:
            bad += 1
            print(f"  FAIL {fp}: {e}")
    if not args.files:
        print("DISC-4 工具。用法: safe_write.py <file.py>...")
        print("(检查『已提交但未推送』请用 _tools/push_state.py —— 那是 DISC-2)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(_main())

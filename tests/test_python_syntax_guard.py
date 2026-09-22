# -*- coding: utf-8 -*-
"""全仓 Python 文件**语法编译守卫** (2026-09-22)。

## 这条守卫为什么存在(来自一次真实且反复的教训)

本次会话里, 我**连续四次**在"生成含大段中文的脚本"时写出语法错误, 每次都是运行到
那一步才失败:

    SyntaxError: invalid syntax. Perhaps you forgot a comma?
        "…而「服务活着但数据没进来」——**两者都不管**…"

根因是同一个: **中文语境里手写了半角引号**(`"…"`), 它把 Python 字符串字面量**提前截断**,
于是后面整段变成语法错误。而报错位置常在**不相干的下一行**(字符串被截断后,
解析器要往后找闭合), 排查时容易被带到错的方向。

用户的指示(采纳): 「在生成含大段中文的 Python 脚本时, 默认先跑 `ast.parse`,
而不是等它失败。这应该写入开发规范。这与 METHOD-1 同源:
**先做能立刻验证的事, 再等它在更晚的阶段失败**。」

## 为什么做成测试而不是只写进规范

"规范"要靠人记得执行 —— 而这次连续四次都说明**记不住**。
一条在测试套里跑、覆盖**全仓每个 .py**的编译检查, 才是"立刻验证"的载体:
它把失败点从"跑到那个脚本时"提前到"提交时"。

**注意**: 它查的是**全部** `src/ scripts/ tests/ ops/ _tools/` 下的 .py,
不只新增文件 —— 因为这类错误也可能被写进既有文件的一次编辑里。

## 已知的刻意豁免

`.ps1` 不在范围内(那是 PowerShell, 有它自己的守卫
`tests/test_interp_parity_helpers.py::TestPowerShellScriptEncoding`)。
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 扫这些目录。含 `_tools/` —— 一次性脚本也曾因同类问题失败过,
#: 而一次性脚本的失败代价是"当场卡住", 同样值得提前拦。
_ROOTS = ("src", "scripts", "tests", "ops", "_tools")
_SKIP_DIRS = {"__pycache__", ".venv314", ".venv310", ".pytest_tmp", "node_modules"}

#: 临时脚本(`_tmp_*.py`)的豁免数量上限。
#:
#: 为什么豁免它们: `scripts/` 里的 `_tmp_*` 是**一次性排查脚本**, 本仓既有约定
#: 就是"用完即清"(历史上有多次 `chore: 移除一次性…脚本` 提交)。它们是**最差**
#: 的文件, 而把守卫指向最差的文件会制造噪声 —— 噪声会让守卫本身被忽略,
#: 那正是本仓反复出现的"永久假阳性"教训。
#:
#: 但**不是无条件豁免**: 这里断言"这类文件的数量不得超过上限"。
#: 首版守卫实测在 `scripts/` 抓到 **25 个** 2026-09-17/18 的遗留(全部未跟踪),
#: 其中一个 `_tmp_fork_safe.py` 是**真语法错误**(docstring 用 `*/` 收尾) ——
#: 说明"临时文件堆积"本身是真实问题。故: 允许少量在途, 但**堆积会被抓到**。
_TMP_GLOB_PREFIX = "_tmp_"
_TMP_MAX = 5


def _is_tmp(fn: str) -> bool:
    return fn.startswith(_TMP_GLOB_PREFIX) and fn.endswith(".py")


def _iter_py(include_tmp: bool = False):
    for root in _ROOTS:
        base = os.path.join(_REPO, root)
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in _SKIP_DIRS]
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                if not include_tmp and _is_tmp(fn):
                    continue
                yield os.path.relpath(os.path.join(dp, fn), _REPO)


def _tmp_files() -> list[str]:
    return [rel for rel in _iter_py(include_tmp=True)
            if _is_tmp(os.path.basename(rel))]


class TestEveryPythonFileCompiles:
    def test_no_syntax_errors_anywhere(self):
        """**核心**: 全仓每个 .py 都必须能解析。

        失败 = 有人(很可能是我)写出了截断的字符串字面量, 最常见的原因是
        **中文里用了半角引号**。修法是把它改成全角 `「」` 或转义, 而**不是**
        调整报错的那一行 —— 报错行往往不是肇事行。
        """
        broken = []
        for rel in _iter_py():
            fp = os.path.join(_REPO, rel)
            try:
                src = open(fp, encoding="utf-8").read()
            except UnicodeDecodeError as e:
                broken.append((rel, f"非 UTF-8 编码: {e}"))
                continue
            try:
                ast.parse(src, filename=fp)
            except SyntaxError as e:
                broken.append((rel, f"第 {e.lineno} 行: {e.msg}"))
        assert not broken, (
            "以下文件有语法错误(多半是**中文里的半角引号截断了字符串字面量**; "
            "注意报错行常不是肇事行, 往前找未闭合的字符串):\n  "
            + "\n  ".join(f"{r}: {m}" for r, m in broken))

    def test_guard_actually_scans_files(self):
        """守卫自身不得空转 —— 否则"没报错"可能只是"没扫到"。"""
        files = list(_iter_py())
        assert len(files) > 100, f"只扫到 {len(files)} 个文件, 路径可能配错了"

    def test_tmp_scripts_do_not_pile_up(self):
        """临时脚本可以少量在途, 但**不得堆积**。

        首版守卫实测在 `scripts/` 抓到 **25 个** 2026-09-17/18 的遗留
        (全部未跟踪), 其中 `_tmp_fork_safe.py` 是**真语法错误** ——
        说明"临时文件堆积"不是洁癖问题, 它同时意味着**没人清理过期的排查残留**。
        """
        tmp = _tmp_files()
        assert len(tmp) <= _TMP_MAX, (
            f"临时脚本堆积到 {len(tmp)} 个 (上限 {_TMP_MAX}): {tmp[:8]}… "
            f"—— 请把过期的一次性脚本清掉(本仓约定: 登记结果落盘后脚本即弃)")

    def test_guard_detects_a_known_bad_snippet(self):
        """**自证**: 喂一段"中文半角引号截断"的样例, 守卫必须能抓到。

        没有这条, 守卫可能因为"判据写错"而永远通过 ——
        那正是本仓反复出现的那类"看起来在检查, 实际什么都没查"。
        """
        bad = '''# -*- coding: utf-8 -*-
"""说明: 本模块不管"服务活着但数据没进来" —— 那正是缺口。"""
'''
        # 上面这段"看起来"是合法的: 引号在注释/文档串里……
        # 真正会炸的是把它放进代码行:
        bad2 = 'x = "而「服务活着」与"数据没进来"是两件事"\n'
        with pytest.raises(SyntaxError):
            ast.parse(bad2)
        # 而合规写法必须解析通过
        ast.parse('x = "而「服务活着」与「数据没进来」是两件事"\n')
        ast.parse(bad)

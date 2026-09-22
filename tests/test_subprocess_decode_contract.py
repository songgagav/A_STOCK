# -*- coding: utf-8 -*-
"""子进程输出的**解码契约**守卫 (2026-09-22 批次)。

## 为什么需要这个文件

中文 Windows 上 `locale.getpreferredencoding()` 是 **cp936**, 所以
`subprocess.run(..., text=True)` 不带 `encoding=` 时按 GBK 解码 —— 而本仓的子进程
Python 脚本输出的是 **UTF-8** 中文。后果有两种, 且**两种都不会让调用方收到异常**:

1. **静默乱码**: GBK 绝大多数字节序列都能"解成功", 只是解出看似正常却完全错误的
   字串。实测: `汉字测试` → `姹夊瓧娴嬭瘯`。
2. **静默 None**: 少数字节序列 GBK 解不开(实测 `0xae`), 错发生在 subprocess 的
   **读取线程**里 —— `subprocess.run` **不抛**, 而是静默返回 `stdout=None`。
   调用方再写 `(x.stdout or "")` 就把"解码失败"伪装成"子进程什么都没说"。

## 本仓实际踩到的三处(同一个 bug, 三种表现)

- `src/datasource_gate.py` 的引擎探针: `stdout=None` ⇒ 探针 JSON 永远解析不到、
  `engine_probe` 恒为 None ⇒ **「引擎不可达/引擎落后」这一整类失效模式从来没被检查过**,
  而报告上只是"没有这一项"。
- `tests/test_cvar_config.py::test_cli_help`: `drl_train.py --help` 的描述含中文 ⇒
  `stdout=None` ⇒ 断言报成 `argument of type 'NoneType' is not a container`。
  **看起来像测试写坏了, 实际是被测程序的 help 根本没读到**, 于是它一直红着没人管。
- `tests/test_exec_algo.py`: 子进程 import 失败的 traceback 含中文 ⇒ `stdout=None`
  ⇒ `proc.stdout.split()` 报 `AttributeError`, **把真正的 import 失败盖掉**。

三处都不是"运气不好", 是同一个默认值陷阱。逐个补是打地鼠, 所以这里立一条不变量。

## 不变量

`text=True` 的调用**必须**同时显式给 `encoding=`。豁免项在 `_ALLOW` 里逐个写明理由 ——
允许豁免, 但**不允许无理由地存在**。
"""
from __future__ import annotations

import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CALL = re.compile(r"subprocess\.(run|check_output|Popen|call|check_call)\s*\(")

#: 扫哪些目录。`_tools/` 与 `scripts/` 也扫: 它们跑在生产排查路径上,
#: 一份乱码的取证日志比没有日志更糟(看起来有证据, 实际不可信)。
_ROOTS = ("src", "scripts", "tests", "ops", "_tools")

#: (相对路径, 行号) -> 豁免理由。**行号变化会主动让守卫失败**, 这是刻意的:
#: 豁免必须被人重新看过一眼, 不能靠"反正它在名单里"长期挂着。
_ALLOW: dict[tuple[str, int], str] = {
    ("scripts/preflight_daemon_interp_chain.py", 27):
        "该脚本**就是**在测平台默认解码行为(解释器 parity 探针), "
        "指定 encoding 会让它测不到东西 —— 这是唯一一处**故意**用默认解码的地方",
}


def _code_only(src: str) -> str:
    """抹掉字符串字面量与注释, 只留代码 —— 否则**说明这个 bug 的文字本身**会被当成 bug。

    实测踩到: 给这个陷阱写注释/文档时提到 `subprocess.run(..., text=True)`,
    守卫立刻把注释也报成违规。守卫扫的是**行为**, 不是提到行为的文字。
    用 `tokenize` 而非正则: 正则处理不了三引号、转义引号与 f-string。
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                # 用等量空白占位, 保住**行号**与列位置, 便于按行报告
                for ch in tok.string:
                    out.append("\n" if ch == "\n" else " ")
            else:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src  # 解不了就退回原文, 宁可多报也不漏报
    return "".join(out)


def _iter_py():
    for root in _ROOTS:
        base = os.path.join(_REPO, root)
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d != "__pycache__"]
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, _REPO).replace("\\", "/")
                if "_tmp" in rel or rel.startswith("_tools/_tmp"):
                    continue
                yield rel, fp


def _offenders() -> list[tuple[str, int, str]]:
    out = []
    for rel, fp in _iter_py():
        try:
            raw = open(fp, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        src = _code_only(raw)
        code_lines = src.split("\n")
        raw_lines = raw.split("\n")
        for i, ln in enumerate(code_lines):
            if not _CALL.search(ln):
                continue
            # 调用块: 从本行到括号配平为止(最多 12 行, 防止跑飞)
            blk, depth = [], 0
            for j in range(i, min(i + 12, len(code_lines))):
                blk.append(code_lines[j])
                depth += code_lines[j].count("(") - code_lines[j].count(")")
                if j > i and depth <= 0:
                    break
            blob = "\n".join(blk)
            if "text=True" in blob and "encoding=" not in blob:
                if (rel, i + 1) in _ALLOW:
                    continue
                shown = raw_lines[i].strip()[:90] if i < len(raw_lines) else ln.strip()[:90]
                out.append((rel, i + 1, shown))
    return out


class TestSubprocessDecodeContract:
    def test_text_true_requires_explicit_encoding(self):
        """**核心不变量**: `text=True` 必须配 `encoding=`。

        失败时怎么办: 不是把行号塞进 `_ALLOW`, 而是**给那个调用补上
        `encoding="utf-8", errors="replace"`** —— 除非你能在这份文件的 `_ALLOW` 里
        写出一条站得住的理由。
        """
        bad = _offenders()
        assert not bad, (
            "以下 subprocess 调用用 text=True 但没给 encoding= —— "
            "中文 Windows 下会按 cp936 解码, 结果是**静默乱码**或**静默 stdout=None**"
            "(实测 `汉字测试` -> `姹夊瓧娴嬭瘯`; 含 0xae 时 stdout=None)。"
            "修法是补 `encoding=\"utf-8\", errors=\"replace\"`:\n  "
            + "\n  ".join("{}:{}  {}".format(*b) for b in bad))

    def test_allow_list_entries_still_exist(self):
        """豁免项必须指向真实存在的行 —— 否则名单会腐烂成"反正它在名单里"。"""
        stale = []
        for (rel, ln), why in _ALLOW.items():
            if not rel.endswith(".py"):
                continue
            fp = os.path.join(_REPO, rel)
            if not os.path.isfile(fp):
                stale.append((rel, ln, "文件不存在"))
                continue
            lines = open(fp, encoding="utf-8").read().split("\n")
            if ln < 1 or ln > len(lines) or not _CALL.search(lines[ln - 1]):
                stale.append((rel, ln, "该行不再是 subprocess 调用"))
        assert not stale, f"豁免名单已腐烂, 请重新核对: {stale}"

    def test_allow_entries_carry_a_reason(self):
        for k, why in _ALLOW.items():
            assert why and len(why) >= 8, f"{k} 的豁免理由太短, 等于没写"

    def test_the_gbk_trap_is_real_on_this_machine(self):
        """把"中文 Windows 默认 cp936"这个**前提**本身钉住。

        若哪天平台默认编码变了, 这条会先红, 提示上面整段推理的前提已变 ——
        而不是让一条基于旧前提的守卫继续假装有效。
        """
        import locale
        enc = locale.getpreferredencoding(False).lower()
        assert enc in ("cp936", "gbk", "utf-8", "utf8"), enc
        if enc in ("cp936", "gbk"):
            raw = "汉字测试".encode("utf-8")
            assert raw.decode("gbk", errors="replace") != "汉字测试"

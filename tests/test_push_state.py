# -*- coding: utf-8 -*-
"""`push_state` 的语义守卫 (2026-09-22) —— DISC-2 的「已提交 vs 已推送」。

## 用户提出的澄清要求

> 「如果它是『检查是否有未推送的提交』: 这是有用的……
>  如果它是『检查是否有未提交的更改』: 它与 `git status` 重复。
>  建议: 确认它的具体作用, 并考虑是否应纳入 DISC-2。」

**本文件就是那个"确认"** —— 用**实验**而不是断言: 在临时仓里造出三种状态,
断言它对每种状态报什么。这样"它查什么"就变成可执行的事实, 而不是我的说法。

## 结论(已由本文件锁定)

| 状态 | `check_push_state` | `git status` |
|---|---|---|
| 已推送 | `ahead=0` | clean |
| **有未提交更改** | **`ahead=0`(不报)** | 看得到 |
| **已提交未推送** | **`ahead=N` + 提交列表** | **看不到** |

⇒ 它查「已提交未推送」, **不查工作区** ⇒ **与 `git status` 互补, 不重复**。
⇒ 归属 **DISC-2**(状态不可区分), 不是 DISC-4。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    p = os.path.join(_REPO, "_tools", "push_state.py")
    assert os.path.isfile(p), f"缺实现: {p}"
    spec = importlib.util.spec_from_file_location("push_state", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PS = _load()


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@pytest.fixture
def sandbox(tmp_path):
    """本地 origin(裸仓) + 已同步的工作副本。"""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("1", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "c1")
    _git(work, "branch", "-q", "-M", "main")
    _git(work, "push", "-q", "-u", "origin", "main")
    return str(work)


class TestItChecksUnpushedNotUncommitted:
    """**核心**: 证明它查的是"未推送", 不是"未提交"。"""

    def test_synced_reports_zero(self, sandbox):
        st = PS.check_push_state(sandbox)
        assert st["ahead"] == 0 and st["behind"] == 0 and st["error"] is None

    def test_dirty_worktree_is_invisible_to_it(self, sandbox):
        """**关键反证**: 有未提交改动时它**不报** —— 那正是 `git status` 的职责。

        若这条失败(比如它开始报工作区), 说明它与 `git status` **重复**了 ——
        用户明确问过这个点, 故用测试把它钉住。
        """
        (sandbox_path := sandbox)
        with open(os.path.join(sandbox_path, "a.txt"), "w", encoding="utf-8") as f:
            f.write("2")
        with open(os.path.join(sandbox_path, "new_untracked.txt"), "w",
                  encoding="utf-8") as f:
            f.write("x")
        dirty = _git(sandbox_path, "status", "--porcelain").stdout.strip().splitlines()
        assert len(dirty) >= 2, f"沙箱未造出脏状态: {dirty}"
        st = PS.check_push_state(sandbox_path)
        assert st["ahead"] == 0, \
            "它对未提交的改动报了 ahead —— 那就与 git status 重复了"
        # 而 git status 确实看得到(证明两者互补)
        assert any("a.txt" in d for d in dirty)

    def test_committed_but_unpushed_is_reported(self, sandbox):
        """**它是为这个而存在的**: 已提交但没推 —— `git status` 看不到。"""
        # **必须先改文件**: 只有 `git add` 而没有内容变化 ⇒ 空提交,
        # `ahead` 会停在 0 而测试假红(首版就这么写的)。
        with open(os.path.join(sandbox, "a.txt"), "w", encoding="utf-8") as f:
            f.write("unpushed-change")
        _git(sandbox, "add", "-A")
        _git(sandbox, "commit", "-q", "-m", "c2-unpushed")
        # git status 仍然干净(这就是那个"静默状态")
        assert _git(sandbox, "status", "--porcelain").stdout.strip() == ""
        st = PS.check_push_state(sandbox)
        assert st["ahead"] == 1, st
        assert any("c2-unpushed" in c for c in st["ahead_commits"]), st["ahead_commits"]

    def test_after_push_it_returns_to_zero(self, sandbox):
        with open(os.path.join(sandbox, "a.txt"), "w", encoding="utf-8") as f:
            f.write("2")
        _git(sandbox, "add", "-A")
        _git(sandbox, "commit", "-q", "-m", "c2")
        assert PS.check_push_state(sandbox)["ahead"] == 1
        _git(sandbox, "push", "-q", "origin", "main")
        assert PS.check_push_state(sandbox)["ahead"] == 0


class TestItFailsLoudlyNotSilently:
    """无法判定时必须说"无法判定", **不得**被读成"已推"。"""

    def test_unknown_branch_is_an_error_not_a_pass(self, sandbox):
        st = PS.check_push_state(sandbox, branch="nosuchbranch")
        assert st["ahead"] is None and st["error"], st
        # 且告警文本要说明"无法判定不等于已推"
        w = PS.format_warning(st)
        assert "无法判定" in w and "不等于已推" in w, w

    def test_not_a_repo_is_an_error(self, tmp_path_factory):
        """非仓库目录应**报错**, 而不是静默 `ahead=0`。

        **必须在仓外造目录**: `tmp_path` 落在本仓内, 而 git 会**向上查找**仓库根 ——
        于是它会找到真仓并正常返回(首版就这么假红过)。
        用 `tmp_path_factory` 的基目录同样在仓内, 故显式造一个仓外的临时目录。
        """
        import tempfile
        with tempfile.TemporaryDirectory(prefix="notarepo_") as td:
            # 确认它**不在**任何 git 仓内(否则测试本身没意义)
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=td,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            assert r.returncode != 0, f"临时目录竟在 git 仓内: {r.stdout.strip()}"
            st = PS.check_push_state(td)
            assert st["error"], "非仓库目录应报错而不是静默 ahead=0"

    def test_format_warning_is_empty_when_synced(self, sandbox):
        assert PS.format_warning(PS.check_push_state(sandbox)) == ""

    def test_format_warning_lists_commits(self, sandbox):
        with open(os.path.join(sandbox, "a.txt"), "w", encoding="utf-8") as f:
            f.write("3")
        _git(sandbox, "add", "-A")
        _git(sandbox, "commit", "-q", "-m", "cX")
        w = PS.format_warning(PS.check_push_state(sandbox))
        assert "该推了" in w and "cX" in w, w


class TestResponsibilitySplit:
    """职责边界: 两个工具各管一件事, 不得互相混入。"""

    def test_safe_write_no_longer_carries_push_state(self):
        """`push_state` 已移出 `safe_write` —— 一个模块一个职责。

        原先它放在 `safe_write.py`(DISC-4 的代码生成校验)里, 会让该模块职责糊掉:
        "代码生成校验"的工具里塞一个 git 检查, 下一个人不会去那里找它。
        """
        sw = open(os.path.join(_REPO, "_tools", "safe_write.py"),
                  encoding="utf-8").read()
        assert "def check_push_state" not in sw, "push_state 又回到 safe_write 里了"
        assert "push_state.py" in sw, "safe_write 应**指向**新位置, 否则找不到"

    def test_push_state_module_declares_its_discipline(self):
        """归属必须写在模块里 —— 否则后人不知道它属于哪条纪律。"""
        src = open(os.path.join(_REPO, "_tools", "push_state.py"),
                   encoding="utf-8").read()
        assert "DISC-2" in src, "未声明归属 DISC-2"
        assert "不查工作区" in src or "不查工作区" in src, "未说明它不查工作区"

    def test_discipline_doc_lists_both_and_assigns_correctly(self):
        doc = open(os.path.join(_REPO, "docs", "disciplines.md"),
                   encoding="utf-8").read()
        assert "skip" in doc and "不等于" in doc, \
            "DISC-2 应含「skip 不等于 通过」这一实例"
        assert "ahead" in doc, "DISC-2 应含「已提交 vs 已推送」这一实例"
        assert "push_state.py" in doc, "纪律文件应点名 push_state 的实现位置"
        # 归属: 已提交/已推送 属 DISC-2; DISC-4 那节应说明它不在此处
        d4 = doc[doc.find("## DISC-4"):]
        assert "push_state" in d4, "DISC-4 应指明 push_state 不在本纪律(职责边界)"

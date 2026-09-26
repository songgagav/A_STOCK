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

from doc_section import code_block_bounds  # noqa: E402  按**结构**定界, 取代 src[i:i+N] 的魔数窗口

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


class TestPreconditionsMustBeAsserted:
    """DISC-2 形态③: **测试自身的状态必须先被断言**。

    ## 这条守的是我自己犯过的错

    我写"已提交但未推送"的用例时忘了改文件内容 ⇒ `git add` 后无内容变化 ⇒
    **空提交 ⇒ 根本没产生提交** ⇒ `ahead` 停在 0 而断言 `== 1` 失败。
    它看起来像"被测代码不报未推送", 实际是**我压根没造出那个状态**。

    这与「假信心测试」同源(都让测试看起来正常), 但形态不同:
      · 假信心测试 = 断言太弱, **永远通过**;
      · 本形态     = 测试跑了、断言也"对"了, 但**在验证一个错误的场景**。

    **通用对策: 在断言"结果"之前, 先断言"场景已成立"。**
    """

    def test_unpushed_scenario_is_actually_established(self, sandbox):
        """造"已提交未推送"时, 先证明**真的产生了新提交**。"""
        before = _git(sandbox, "rev-parse", "HEAD").stdout.strip()
        with open(os.path.join(sandbox, "a.txt"), "w", encoding="utf-8") as f:
            f.write("changed")            # **必须先改内容**, 否则是空提交
        _git(sandbox, "add", "-A")
        _git(sandbox, "commit", "-q", "-m", "c-pre")
        after = _git(sandbox, "rev-parse", "HEAD").stdout.strip()
        # 先断言场景成立, 再断言被测行为
        assert after != before, "前置条件未成立: 没有产生新提交(疑空提交)"
        assert _git(sandbox, "status", "--porcelain").stdout.strip() == "", \
            "前置条件未成立: 工作区不干净, 说明提交没成功"
        assert PS.check_push_state(sandbox)["ahead"] == 1

    def test_dirty_scenario_is_actually_established(self, sandbox):
        """造"工作区脏"时, 先证明 git 真的看得见改动。"""
        with open(os.path.join(sandbox, "a.txt"), "w", encoding="utf-8") as f:
            f.write("dirty")
        porcelain = _git(sandbox, "status", "--porcelain").stdout.strip()
        assert porcelain, "前置条件未成立: 没有造出脏工作区"
        assert any("a.txt" in l for l in porcelain.splitlines())
        # 场景成立后, 才断言"它不报"
        assert PS.check_push_state(sandbox)["ahead"] == 0

    def test_non_repo_scenario_is_actually_outside_any_repo(self):
        """造"非仓库目录"时, 先证明该目录**真的不在任何 git 仓内**。

        首版用 pytest 的 `tmp_path` —— 它落在本仓内, 而 git 会**向上查找**仓库根,
        于是找到真仓并正常返回, 用例假红(看起来像"被测代码不报错")。
        """
        import tempfile
        with tempfile.TemporaryDirectory(prefix="notarepo2_") as td:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=td,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            assert r.returncode != 0, \
                f"前置条件未成立: 该目录在 git 仓内({r.stdout.strip()})"
            assert PS.check_push_state(td)["error"]


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

    def test_discipline_doc_documents_all_three_test_failure_modes(self):
        """DISC-2 必须列出三种"测试看起来正常"的形态(用户要求并列)。

        ① 假信心测试(断言太弱) ② skip 被读成通过 ③ **前置状态没造出来**。
        第③种最隐蔽: 测试跑了、断言也"对"了, 却在**验证一个错误的场景**。
        """
        doc = open(os.path.join(_REPO, "docs", "disciplines.md"),
                   encoding="utf-8").read()
        for token in ("假信心测试", "skip", "前置状态没造出来"):
            assert token in doc, f"DISC-2 缺形态: {token}"
        assert "先断言" in doc and "场景已成立" in doc, \
            "DISC-2 应给出形态③的通用对策(先断言场景已成立)"


class TestRunDailyLogsWithoutAlerting:
    """`push_state` 接入 daily 回执: **只记日志, 不告警**(用户决策)。"""

    def test_helper_returns_the_number_and_says_it_is_dev_discipline(self):
        import importlib.util
        # run_daily 依赖较重, 用源码检查 + 独立函数测试
        p = os.path.join(_REPO, "src", "run_daily.py")
        src = open(p, encoding="utf-8").read()
        assert "_push_state_meta" in src and "_push_state_streak" in src
        assert "只记日志" in src or "只记录" in src, "未声明「不告警」"
        assert "开发纪律" in src, "未声明它是开发纪律而非运行时纪律"

    def test_it_never_raises_into_the_pipeline(self):
        """开发纪律的检查**绝不能**影响运行时管道。

        源码层面锁: 调用处包在 try/except 里, 且异常只写进 report 不抛出。
        """
        src = open(os.path.join(_REPO, "src", "run_daily.py"),
                   encoding="utf-8").read()
        i = src.find("report[\"push_state\"] = _ps")
        assert i > 0, "未找到接入点"
        blk = src[max(0, i - 700):i + 700]
        assert "try:" in blk and "except Exception" in blk, \
            "push_state 接入未包 try/except —— 一个 git 故障会打断收盘管道"
        # helper 自身也必须吞异常
        j = src.find("def _push_state_meta")
        hblk = src[j:code_block_bounds(src, j)]
        assert "except Exception" in hblk, "helper 未吞异常"

    def test_streak_needs_consecutive_days_not_one(self):
        """用户要求: **连续 N 天**才算"该推了", 单日 ahead>0 不该升级措辞。

        理由同 `datasource_gate`: 偶发一天是正常(大重构), 持续才是真漏。
        """
        src = open(os.path.join(_REPO, "src", "run_daily.py"),
                   encoding="utf-8").read()
        assert "PUSH_STALE_DAYS" in src
        assert "连续" in src, "未体现「连续 N 天」"

    def test_no_alert_rule_was_added_for_push_state(self):
        """**刻意不接告警** —— 它是开发纪律, 加了会变成噪声。

        实测依据: `TableStale24h` 因长期无人处理而 firing 330 次 ⇒
        长期无人处理的告警等于没有告警。故这里断言告警规则里**没有** push 相关项。
        """
        rules = open(os.path.join(_REPO, "ops", "alert_rules.yml"),
                     encoding="utf-8").read()
        low = rules.lower()
        for bad in ("push_state", "pushstate", "unpushed"):
            assert bad not in low, \
                f"告警规则里出现了 {bad} —— 用户明确要求只记日志不告警"

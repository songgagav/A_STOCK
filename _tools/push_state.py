# -*- coding: utf-8 -*-
"""`check_push_state`: **已提交但未推送** vs **已推送** —— DISC-2 的一个实例。

## 它的确切作用(经实验确认, 不是推测)

| 状态 | 本模块 | `git status` |
|---|---|---|
| 已推送 | `ahead=0` | clean |
| **有未提交更改** | **`ahead=0`(不报)** | 看得到 |
| **已提交未推送** | **`ahead=N` + 提交列表** | **看不到** |

⇒ 它查的是「**已提交但未推送**」, **不是**「未提交的更改」。
工作区脏**不在它视野内** —— 那是 `git status` 的职责。**两者互补, 不重复。**

## 为什么它属于 DISC-2(检查项静默消失)

DISC-2 说的是: 报告上「没有这一项」与「这一项没问题」必须可区分。
推送到远端这件事正是同一形态 —— 而且本会话**真的踩到了**:

    我 commit 了 **35 次**, 而 `git status` **一直是干净的**。
    于是「看起来一切已保存」, 实际**一次都没推**。
    直到用户问「我发现你很久没 commit 了」才发现。

`git status` **只显示未提交的文件, 不告诉你 `ahead 35`** ——
这就是那个「静默状态」: 没有报错、没有异常, 只是一个**没人问的问题**。

## 为什么单独一个模块(原先放在 safe_write.py 里)

它是**版本控制**层面的检查, 与 DISC-4(生成代码前先 `ast.parse`)不是一回事。
放在一起会让 `safe_write` 的职责糊掉 —— "代码生成校验"的工具里塞一个 git 检查,
下一个人不会去那里找它。**一个模块一个职责。**
"""
from __future__ import annotations

import re
import subprocess


def check_push_state(repo: str, *, remote: str = "origin",
                     branch: str = "main", timeout: int = 60) -> dict:
    """报告本地相对远端**领先/落后**多少提交。

    返回 `{repo, remote, branch, ahead, behind, ahead_commits, error}`。
    `ahead > 0` ⇒ **有已提交但未推送的工作**(该推了)。

    **不查工作区**: 未提交的改动请用 `git status`(本函数对它是盲的, 见模块 docstring)。
    """
    def _run(args):
        return subprocess.run(args, cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    out = {"repo": repo, "remote": remote, "branch": branch,
           "ahead": None, "behind": None, "ahead_commits": [], "error": None}
    r = _run(["git", "rev-list", "--left-right", "--count",
              f"{remote}/{branch}...{branch}"])
    if r.returncode != 0:
        out["error"] = ((r.stderr or r.stdout or "").strip()[:200]
                        or "rev-list 失败(远端分支可能不存在)")
        return out
    m = re.match(r"\s*(\d+)\s+(\d+)", r.stdout or "")
    if not m:
        out["error"] = f"无法解析 rev-list 输出: {(r.stdout or '')[:120]!r}"
        return out
    out["behind"], out["ahead"] = int(m.group(1)), int(m.group(2))
    if out["ahead"]:
        r2 = _run(["git", "log", "--oneline", "-n", str(min(out["ahead"], 20)),
                   f"{remote}/{branch}..{branch}"])
        out["ahead_commits"] = [l for l in (r2.stdout or "").splitlines() if l.strip()]
    return out


def format_warning(st: dict) -> str:
    """把状态压成一句人话(没有待推内容时返回空串)。"""
    if st.get("error"):
        return f"[push-state] **无法判定**(不等于已推): {st['error']}"
    n = st.get("ahead") or 0
    if not n:
        return ""
    return (f"⚠️ 本地领先 {st['remote']}/{st['branch']} **{n} 个提交** —— 该推了。\n"
            + "\n".join("  " + c for c in st.get("ahead_commits") or []))


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description="检查『已提交但未推送』(DISC-2 实例); 不查工作区, 那用 git status")
    ap.add_argument("repo", nargs="?", default=".",
                    help="仓库路径(默认当前目录)")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    st = check_push_state(args.repo, remote=args.remote, branch=args.branch)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        warn = format_warning(st)
        if warn:
            print(warn)
        elif st.get("error"):
            print(f"[push-state] 无法判定: {st['error']}")
        else:
            print(f"[push-state] 已同步(ahead=0, behind={st.get('behind')})")
    return 1 if (st.get("ahead") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(_main())

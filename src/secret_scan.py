# -*- coding: utf-8 -*-
"""明文密钥扫描（SEC-2 的防复发控制）。

背景（2026-09-22 取证结论）
---------------------------
SEC-2 原描述为『research_trader/.env 明文凭据』，容易被读成"凭据进了 git"。取证结果**相反**:
以脚本从 .env 读出每个键的**值**再回查两个仓库(`A_stock_rotation` / `_merge_workspace`)的
当前跟踪树与**全部历史**(`git grep` + `git log --all -S`)，12 个键中所有真实凭据
(OPENAI_API_KEY / tushare_token / simnow_* / UserID / AuthCode / AppID / GRAPHRAG_API_KEY)
**当前树 0 文件、历史 0 提交** —— 即**从未进入 git**，故**不需要改写 git 历史**（省掉一次
高风险且影响所有克隆的操作）。真正的风险是**磁盘明文**与**将来某次误提交**。

故本模块只做一件事: **让"误提交"在提交前就失败**。它不追历史(历史已验清白), 只看当下。

规则精度优先（本会话的既定纪律）
--------------------------------
一个把 `os.environ["OPENAI_API_KEY"]`(正确写法) 也报出来的扫描器会被立刻忽略, 等于没有。
故每条规则都配**负向控制**测试, 并显式放行: 环境变量读取、空值、占位符
(your-/xxx/changeme/example/placeholder/`${VAR}`/`<...>`/test/dummy)。

规则:
  env_tracked       被 git 跟踪的 `.env` / `.env.*`(**`.env.example` 除外**) => CRITICAL
                    —— 本仓 .gitignore 已含 `.env`/`.env.*`/`!.env.example`, 故这条同时是
                    "有人 -f 强加了"的探测器
  known_prefix      已知密钥前缀(sk- / ghp_ / AKIA / xox?-) 或 PEM 私钥块 => CRITICAL
  hardcoded_secret  形如 `SOMETHING_KEY = "12 位以上字面量"`(名字含 key/secret/token/passwd
                    /authcode/appid) => HIGH
  url_with_creds    形如 scheme://user:password@host 的内嵌凭据 => HIGH  # secret-scan: ok: 规则文档示例, 非真实凭据

豁免: 行尾 `# secret-scan: ok: 理由`（必须写理由, 与 lookahead 扫描器同一约定）。
"""
from __future__ import annotations

import os
import re
import subprocess

RULES = {"env_tracked": "CRITICAL", "known_prefix": "CRITICAL",
         "hardcoded_secret": "HIGH", "url_with_creds": "HIGH"}

#: 允许被跟踪的模板文件
ENV_ALLOW = (".env.example", ".env.sample", ".env.template")

#: 已知密钥前缀 / 私钥块
_PREFIX_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "OpenAI 风格 sk- 密钥"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key ID"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥块"),
]

#: `名字含敏感词 = "字面量"`（Python / shell / js / yaml 均覆盖）
_SECRET_NAME = (r"(?:api[_\-]?key|apikey|secret|passwd|password|token|authcode|"
                r"appid|access[_\-]?key|private[_\-]?key)")
_ASSIGN = re.compile(
    r"""(?ix)
    (?P<name>[A-Za-z0-9_\-]*%s[A-Za-z0-9_\-]*)            # 变量名含敏感词(可在开头)
    \s*[:=]\s*
    (?P<q>["'])(?P<val>.+?)(?P=q)                          # 字面量字符串
    """ % _SECRET_NAME)

_URL_CREDS = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@")

#: 占位符/非密钥白名单（大小写不敏感, 命中即放行）
_PLACEHOLDER = re.compile(
    r"""(?ix)^(
      |your[\-_].*|.*[\-_]here|xxx+|\*+|<.*>|\$\{.*\}|\{\{.*\}\}
      |change[\-_]?me|placeholder|example.*|sample.*|dummy.*|test.*|todo|none|null|nil
      |\.\.\.+|redacted|masked
    )$""")


def _is_placeholder(v: str) -> bool:
    v = (v or "").strip()
    if len(v) < 6:
        return True                       # 太短不构成凭据
    if _PLACEHOLDER.match(v):
        return True
    # 纯重复字符 / 纯字母数字但无随机性特征(如 abcdefgh) 不视为凭据 —— 保守放行
    if len(set(v)) <= 3:
        return True
    return False


def scan_text(text: str, filename: str = "<text>") -> list:
    """扫描一段文本（**纯函数**, CI 可测）。返回 findings。"""
    out: list = []
    lines = text.splitlines()

    # 文件级豁免: 扫描器**自身的测试夹具**必须包含伪造的密钥形态, 否则无法做正向控制。
    # 与 lookahead 扫描器同一约定; 代价是该文件内的真泄露不会被报 —— 故仅限夹具文件使用。
    if any("secret-scan: skip-file" in ln for ln in lines[:5]):
        return out

    base = os.path.basename(filename)
    if base.startswith(".env") and base not in ENV_ALLOW:
        out.append({"file": filename, "line": 1, "rule": "env_tracked", "severity": "CRITICAL",
                    "snippet": base,
                    "why": f"被 git 跟踪的 {base} —— 真实凭据文件不得入库(模板请用 .env.example)"})

    for i, ln in enumerate(lines, start=1):
        if "secret-scan: ok" in ln:
            continue
        for pat, desc in _PREFIX_PATTERNS:
            if pat.search(ln):
                out.append({"file": filename, "line": i, "rule": "known_prefix",
                            "severity": "CRITICAL", "snippet": ln.strip()[:110],
                            "why": f"命中已知密钥形态: {desc}"})
                break
        m = _ASSIGN.search(ln)
        if m and not _is_placeholder(m.group("val")):
            out.append({"file": filename, "line": i, "rule": "hardcoded_secret",
                        "severity": "HIGH",
                        "snippet": re.sub(r"""(["']).+?(["'])""", r"\1<redacted>\2", ln.strip())[:110],
                        "why": f"疑似硬编码密钥: {m.group('name')} = <字面量>"})
        if _URL_CREDS.search(ln):
            out.append({"file": filename, "line": i, "rule": "url_with_creds",
                        "severity": "HIGH",
                        "snippet": _URL_CREDS.sub("://<redacted>@", ln.strip())[:110],
                        "why": "URL 内嵌凭据"})
    return out


def tracked_files(repo: str) -> list:
    try:
        p = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, timeout=60)
        return [x for x in (p.stdout or "").splitlines() if x.strip()]
    except Exception:  # noqa: BLE001
        return []


def scan_repo(repo: str, max_bytes: int = 2_000_000) -> dict:
    """扫描仓库的**跟踪文件**(只看当下; 历史已由 SEC-2 取证确认清白)。"""
    findings: list = []
    files = tracked_files(repo)
    for rel in files:
        fp = os.path.join(repo, rel)
        try:
            if os.path.getsize(fp) > max_bytes:
                continue
            with open(fp, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:  # noqa: BLE001
            continue
        if "\x00" in text[:1000]:
            continue                       # 二进制
        findings.extend(scan_text(text, rel))
    by_rule: dict = {}
    for h in findings:
        by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
    return {"repo": repo, "files": len(files), "findings": findings, "by_rule": by_rule}


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="明文密钥扫描(只看被 git 跟踪的文件)")
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = scan_repo(args.repo)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {r['files']} 个跟踪文件; 命中 {len(r['findings'])} 处")
        print("按规则: " + (", ".join(f"{k}={v}" for k, v in sorted(r["by_rule"].items())) or "(无)"))
        for h in sorted(r["findings"], key=lambda x: (x["severity"], x["file"], x["line"])):
            print(f"  [{h['severity']}] {h['file']}:{h['line']} {h['rule']}")
            print(f"        {h['why']}")
            print(f"        {h['snippet']}")
    return 1 if any(h["severity"] == "CRITICAL" for h in r["findings"]) else 0


if __name__ == "__main__":
    raise SystemExit(_main())

# -*- coding: utf-8 -*-
"""一次性验收: **真实业务模块**走 Ollama 是否通 —— 不只测底层客户端.

## 为什么要测到业务模块

底层 `_post_chat` 通了**不代表**业务模块通了。本仓曾有 4 份 `_post_anthropic`
复制件 + 2 处内联 POST, 各自拼 URL / 各自解响应。只改主模块时, 这些副本会
**单独**在切换协议后失效, 而表现是"没有输出"而不是报错。

故本脚本直接调用**各业务模块自己的 LLM 入口**, 逐模块报通/不通:

  · `alpha_logics._call_llm`      —— 经济逻辑发现
  · `factor_mad._call_llm`        —— 因子挖掘
  · `pre_drl_brief._post_anthropic` —— 盘前简报(副本已改委托)
  · `incremental_learn._post_anthropic` —— 增量学习(副本已改委托)

**只读 + 若干次真实 LLM 调用**, 不写业务状态(不落盘、不改 data/)。
"""

from __future__ import annotations

import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
          "OPENAI_TIMEOUT_SECONDS", "LLM_API_TYPE", "LLM_THINK"):
    os.environ.pop(k, None)

import llm_commentary as lc  # noqa: E402

lc._load_dotenv()
print("端点: %s | 模型: %s | 协议: %s"
      % (os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_MODEL"),
         lc._resolve_api_type(os.environ.get("OPENAI_BASE_URL", ""))))
print("=" * 78)

RESULTS = []


def _check(name, fn):
    try:
        r = fn()
        ok = bool(r)
        RESULTS.append((name, ok, "" if ok else "返回空/None"))
        print("[%s] %s" % ("OK  " if ok else "FAIL", name))
        if ok:
            s = r if isinstance(r, str) else str(r)
            print("        -> %r" % s[:180])
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print("[FAIL] %s -> %s: %s" % (name, type(e).__name__, e))


MSG = [{"role": "system", "content": "Reply with JSON only. No prose."},
       {"role": "user", "content": 'Reply exactly: {"probe": "alpha"}'}]


def _alpha():
    import alpha_logics
    return alpha_logics._call_llm(list(MSG), max_tokens=100)


def _mad():
    import factor_mad
    return factor_mad._call_llm(list(MSG), max_tokens=100)


def _pre_drl():
    import pre_drl_brief
    url = lc._build_messages_url(os.environ.get("OPENAI_BASE_URL", ""))
    res = pre_drl_brief._post_anthropic(
        url, os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", ""),
        "Reply with JSON only.", 'Reply exactly: {"probe": "pre_drl"}',
        max_tokens=100, timeout=180.0)
    body = res["body"]
    return "".join(b.get("text", "") for b in (body.get("content") or []))


def _incr():
    import incremental_learn
    url = lc._build_messages_url(os.environ.get("OPENAI_BASE_URL", ""))
    res = incremental_learn._post_anthropic(
        url, os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", ""),
        "Reply with JSON only.", 'Reply exactly: {"probe": "incr"}',
        max_tokens=100, timeout=180.0)
    body = res["body"]
    return "".join(b.get("text", "") for b in (body.get("content") or []))


_check("alpha_logics._call_llm", _alpha)
_check("factor_mad._call_llm", _mad)
_check("pre_drl_brief._post_anthropic", _pre_drl)
_check("incremental_learn._post_anthropic", _incr)

print("=" * 78)
n_ok = sum(1 for _, ok, _ in RESULTS if ok)
print("小结: %d/%d 个业务模块调用成功" % (n_ok, len(RESULTS)))
for name, ok, err in RESULTS:
    if not ok:
        print("  FAIL %s: %s" % (name, err))
print("=" * 78)
raise SystemExit(0 if n_ok == len(RESULTS) else 1)

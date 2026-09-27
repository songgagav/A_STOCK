# -*- coding: utf-8 -*-
"""一次性验收: 走**生产 dotenv 加载器**验证 Ollama 链路是否真的通了.

## 为什么要走加载器而不是直接读环境变量

`llm_commentary._load_dotenv()` 的语义是「**只补 os.environ 里没有的键**」
(`if k and k not in os.environ`)。所以如果测试进程里已经存在 `OPENAI_BASE_URL`,
那么**.env 改了也不会生效** —— 这正是"配置看着对、实际没生效"的经典形态。

本脚本故意在**干净的 os.environ** 下调用 `_load_dotenv()`, 再走真实调用链,
从而同时验证三件事:
  ① .env 路径推断是否正确(它读的是 research_trader/.env, 不是本仓 .env);
  ② `.env` 里的键是否被真的加载进来;
  ③ 多协议分发 + 响应归一化 + JSON 抽取是否端到端可用。

**只读 + 一次真实 LLM 调用**, 不写任何状态。
"""

from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

# ---- 关键: 先清掉可能从父进程泄漏进来的同名变量, 逼真的从 .env 加载 ----
for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
          "OPENAI_TIMEOUT_SECONDS", "LLM_API_TYPE", "LLM_THINK"):
    os.environ.pop(k, None)

import llm_commentary as lc  # noqa: E402

print("=" * 78)
print("① 加载 .env (干净环境下)")
print("=" * 78)
for p in lc._DEFAULT_ENV_PATHS:
    print("  候选路径: %s  存在=%s" % (p or "(空)", bool(p) and os.path.exists(p)))
lc._load_dotenv()

base = os.environ.get("OPENAI_BASE_URL", "")
print()
print("  OPENAI_BASE_URL   = %s" % base)
print("  OPENAI_MODEL      = %s" % os.environ.get("OPENAI_MODEL", ""))
print("  OPENAI_TIMEOUT_S  = %s" % os.environ.get("OPENAI_TIMEOUT_SECONDS", ""))
print("  LLM_API_TYPE      = %s" % os.environ.get("LLM_API_TYPE", "(未设)"))
print("  LLM_THINK         = %s" % os.environ.get("LLM_THINK", "(未设)"))
print("  OPENAI_API_KEY    = %s" % ("<已设置 len=%d>" % len(os.environ.get("OPENAI_API_KEY", ""))
                                   if os.environ.get("OPENAI_API_KEY") else "(空)"))

print()
print("=" * 78)
print("② 协议解析 与 URL 构造")
print("=" * 78)
api_type = lc._resolve_api_type(base)
url = lc._build_messages_url(base)
print("  _resolve_api_type(base) = %s" % api_type)
print("  _build_messages_url     = %s" % url)
print("  期望: ollama + http://192.168.1.5:11434/api/chat")

print()
print("=" * 78)
print("③ 真实调用 (走 _post_messages, 与各业务模块同一条路径)")
print("=" * 78)
SYS = ("You are a JSON-only API for a quant trading system. "
       "Reply with a single JSON object and no prose.")
USR = ('Return exactly: {"verdict": "ok", "confidence": 0.9}')
try:
    res = lc._post_messages(url, os.environ.get("OPENAI_API_KEY", ""),
                            os.environ.get("OPENAI_MODEL", ""),
                            SYS, USR, max_tokens=200, timeout=180.0,
                            user_agent="A_stock_rotation/probe")
    body = res["body"]
    print("  latency      = %.2fs" % res["latency"])
    print("  api_type     = %s" % res.get("_api_type"))
    print("  url          = %s" % res.get("_url"))
    print("  归一化后键   = %s" % sorted(k for k in body.keys()
                                        if not k.startswith("_")))
    content = body.get("content") or []
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    print("  content[].text 长度 = %d" % len(text))
    print("  text 前 200 字 : %r" % text[:200])
    if body.get("thinking"):
        print("  thinking 长度 = %d (已与正文分离)" % len(body["thinking"]))

    print()
    print("=" * 78)
    print("④ 结构化抽取 (业务模块真正依赖的那一步)")
    print("=" * 78)
    raw = lc._extract_first_json_object(text)
    print("  _extract_first_json_object -> %r" % (raw or "")[:200])
    if raw:
        obj = json.loads(raw)
        print("  json.loads OK -> %s" % obj)
        print()
        print("  ==> 端到端 **通过**: .env -> 加载 -> 协议分发 -> 调用 -> 归一化 -> JSON")
    else:
        print("  ==> 未能抽到 JSON —— 抽取环节需要复核")
except Exception as e:
    print("  调用失败: %s: %s" % (type(e).__name__, e))
    print("  ==> 端到端 **失败**")
    raise SystemExit(1)

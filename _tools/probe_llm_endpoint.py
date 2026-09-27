# -*- coding: utf-8 -*-
"""一次性探测: 实测 Ollama 端点的协议/耗时/返回结构, 为配置与代码改动取证.

## 为什么必须先实测

要把 LLM 从 MiniMax(Anthropic 协议) 切到 Ollama, 有三件事**不能猜**:

1. **路由**: 现有代码 `_build_messages_url` 恒拼 `<base>/anthropic/v1/messages`;
   Ollama 是否有这个路由? 没有的话直接填地址会 404。
2. **返回结构**: 各模块的 `_parse_response` 都读 `body["content"]`(Anthropic 形状)。
   Ollama 原生是 `message.content`, OpenAI 兼容是 `choices[0].message.content`
   ⇒ 若不归一化, 即使连通也会报"响应缺少 content"。
3. **耗时**: 默认超时 90s。qwen3.6:35b 是 36B MoE + thinking 模型,
   若单次调用超过 90s, 配置正确也会**每次超时失败** —— 这是"配置成功但功能仍坏"的
   典型形态, 必须在落盘前量出来。

## 输出

逐路由的状态码 + 一次真实 /api/chat 往返的耗时、content/thinking 长度与结构。
**只读**, 不改任何配置。
"""

from __future__ import annotations

import json
import time
from urllib import error, request

HOST = "192.168.1.5:11434"
MODEL = "qwen3.6:35b"

PROMPT = (
    "You are a JSON-only API. Reply with exactly this JSON and nothing else: "
    '{"ok": true, "n": 3}'
)


def _get(path: str, timeout: float = 10.0):
    url = f"http://{HOST}{path}"
    try:
        with request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _post(path: str, payload: dict, timeout: float):
    url = f"http://{HOST}{path}"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return r.status, body, time.time() - t0, None
    except error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300], time.time() - t0, "HTTPError"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.time() - t0, "exception"


def main() -> int:
    print("=" * 78)
    print("① 路由探测 (哪些路径存在)")
    print("=" * 78)
    for p in ("/api/tags", "/api/version", "/v1/models",
              "/anthropic/v1/messages", "/v1/chat/completions"):
        st, body = _get(p)
        print("  %-28s -> %s   %s" % (p, st, body[:80].replace("\n", " ")))
    print()
    print("  注: 现有代码恒请求 `<base>/anthropic/v1/messages`。")
    print("      上面若为 404, 说明**不能把 base 直接指向 Ollama**。")

    print()
    print("=" * 78)
    print("② 原生 /api/chat 往返 (关闭 thinking, 量耗时)")
    print("=" * 78)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Reply with JSON only. No prose."},
            {"role": "user", "content": PROMPT},
        ],
        "stream": False,
        "think": False,
    }
    st, body, dt, err = _post("/api/chat", payload, timeout=600)
    print("  status=%s  耗时=%.2fs  %s" % (st, dt, err or ""))
    if st == 200:
        try:
            j = json.loads(body)
            msg = j.get("message") or {}
            print("  顶层键        : %s" % sorted(j.keys()))
            print("  message 键    : %s" % sorted(msg.keys()))
            print("  content 长度  : %s" % len(msg.get("content") or ""))
            print("  thinking 长度 : %s" % len(msg.get("thinking") or ""))
            print("  content 内容  : %r" % (msg.get("content") or "")[:160])
        except Exception as e:
            print("  解析失败: %s / 原文: %s" % (e, body[:200]))
    else:
        print("  原文: %s" % body[:300])

    print()
    print("=" * 78)
    print("③ OpenAI 兼容 /v1/chat/completions 往返")
    print("=" * 78)
    payload2 = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Reply with JSON only. No prose."},
            {"role": "user", "content": PROMPT},
        ],
        "stream": False,
    }
    st, body, dt, err = _post("/v1/chat/completions", payload2, timeout=600)
    print("  status=%s  耗时=%.2fs  %s" % (st, dt, err or ""))
    if st == 200:
        try:
            j = json.loads(body)
            ch = (j.get("choices") or [{}])[0]
            print("  顶层键        : %s" % sorted(j.keys()))
            print("  choices[0] 键 : %s" % sorted(ch.keys()))
            print("  message 键    : %s" % sorted((ch.get("message") or {}).keys()))
            print("  content 内容  : %r" % ((ch.get("message") or {}).get("content") or "")[:160])
        except Exception as e:
            print("  解析失败: %s / 原文: %s" % (e, body[:200]))
    else:
        print("  原文: %s" % body[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""一次性验收: `OPENAI_TIMEOUT_SECONDS` 是否真的贯穿到**每个** LLM 调用点.

## 为什么要单独验这一项

超时"读环境变量"很容易做成**半通**: 主模块读了, 但某个业务模块里写死了 60s。
那种情况下把变量从 90 调到 180, 主路径生效、旁路不生效 —— 而旁路只在
**提示词变长导致真的超时**时才暴露, 平时完全看不出来。

故本脚本不测"代码里写没写", 而是**实测行为**: 在多个不同的
`OPENAI_TIMEOUT_SECONDS` 取值下, 逐个调用各业务模块的 LLM 入口, 用假
`urlopen` 捕获**实际传给 urlopen 的 timeout**, 再断言它等于环境变量值。

**不联网**(urlopen 被打桩), 不写状态。
"""

from __future__ import annotations

import json
import os
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "src"))

for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
          "OPENAI_TIMEOUT_SECONDS", "LLM_API_TYPE", "LLM_THINK"):
    os.environ.pop(k, None)

import llm_commentary as lc  # noqa: E402

lc._load_dotenv()
os.environ["OPENAI_BASE_URL"] = "http://192.168.1.5:11434"
os.environ["LLM_API_TYPE"] = "ollama"
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ["OPENAI_MODEL"] = "qwen3.6:35b"

CAPTURED = {}


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        # 归一化能吃的 Anthropic 形状
        return json.dumps(
            {"content": [{"type": "text", "text": '{"ok": true}'}]}).encode()


def _fake_urlopen(req, timeout=None):
    CAPTURED["timeout"] = timeout
    CAPTURED["url"] = req.full_url
    return _Resp()


MSG = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]


def _run_all():
    """依次触发各业务模块的 LLM 入口; 每个入口都会经 urlopen(被打桩)。"""
    out = {}
    import alpha_logics, factor_mad, agent_orchestrator
    import incremental_learn, pre_drl_brief

    # **必须真的打桩**: 各模块都是 `from urllib import request` 后调
    # `request.urlopen`, 而 `request` 就是 `urllib.request` 这个模块对象 ⇒
    # 打 `urllib.request.urlopen` 即可覆盖全部调用点。
    # [2026-09-28 自查] 本脚本第一版只定义了 `_fake_urlopen` 却**从未 patch**,
    # 于是每个调用点都真的去连网、失败返回 None, 报告全是 MISMATCH ——
    # 看上去像"代码没读环境变量", 实际是脚本自己没接线。
    #
    # 还要**同时打掉 `_load_dotenv`**: 各模块入口都会调它, 而真实 .env 里写着
    # `OPENAI_TIMEOUT_SECONDS=180`。若不禁用, 本脚本设的 45 / "未设置" 会被
    # 各模块重新灌回 180 —— 那会让断言测的是 .env 而不是被测代码。
    from unittest import mock
    import urllib.request

    with mock.patch.object(urllib.request, "urlopen", _fake_urlopen), \
            mock.patch.object(lc, "_load_dotenv", lambda: None):
        for label, fn in (
            ("alpha_logics._call_llm",
             lambda: alpha_logics._call_llm(list(MSG), max_tokens=64)),
            ("factor_mad._call_llm",
             lambda: factor_mad._call_llm(list(MSG), max_tokens=64)),
            ("agent_orchestrator._call_llm_orch",
             lambda: agent_orchestrator._call_llm_orch(list(MSG), max_tokens=64)),
            ("llm_commentary._post_chat",
             lambda: lc._post_chat(os.environ["OPENAI_BASE_URL"], "",
                                   "qwen3.6:35b", "S", "U",
                                   timeout=lc._env_timeout())),
            ("pre_drl_brief._post_anthropic",
             lambda: pre_drl_brief._post_anthropic(
                 lc._build_messages_url(os.environ["OPENAI_BASE_URL"]), "",
                 "qwen3.6:35b", "S", "U", max_tokens=64,
                 timeout=lc._env_timeout())),
            ("incremental_learn._post_anthropic",
             lambda: incremental_learn._post_anthropic(
                 lc._build_messages_url(os.environ["OPENAI_BASE_URL"]), "",
                 "qwen3.6:35b", "S", "U", max_tokens=64,
                 timeout=lc._env_timeout())),
        ):
            CAPTURED.clear()
            try:
                fn()
                out[label] = CAPTURED.get("timeout")
            except Exception as e:
                out[label] = f"ERR {type(e).__name__}: {e}"
    return out


def main() -> int:
    print("=" * 78)
    print("各调用点实际传给 urlopen 的 timeout")
    print("=" * 78)
    ok = True
    for val in ("180", "45"):
        os.environ["OPENAI_TIMEOUT_SECONDS"] = val
        print()
        print("OPENAI_TIMEOUT_SECONDS = %s" % val)
        print("-" * 78)
        res = _run_all()
        for label, got in res.items():
            match = (isinstance(got, float) and abs(got - float(val)) < 1e-9)
            print("  %-42s timeout=%-10s %s"
                  % (label, got, "OK" if match else "**MISMATCH**"))
            if not match:
                ok = False

    # 未设置时应回落默认
    print()
    os.environ.pop("OPENAI_TIMEOUT_SECONDS", None)
    print("OPENAI_TIMEOUT_SECONDS = (未设置) -> 期望回落 %.0fs"
          % lc.DEFAULT_LLM_TIMEOUT_S)
    print("-" * 78)
    res = _run_all()
    for label, got in res.items():
        match = (isinstance(got, float)
                 and abs(got - lc.DEFAULT_LLM_TIMEOUT_S) < 1e-9)
        print("  %-42s timeout=%-10s %s"
              % (label, got, "OK" if match else "**MISMATCH**"))
        if not match:
            ok = False

    print()
    print("=" * 78)
    print("结论: %s" % ("所有调用点都跟随环境变量" if ok
                        else "**存在未跟随的调用点**"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

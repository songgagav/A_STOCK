# -*- coding: utf-8 -*-
"""LLM 多协议支持(ollama / openai / anthropic)的判据.

## 为什么需要这一组用例

[2026-09-28] 把 LLM 从 MiniMax(Anthropic 协议)切到本地 Ollama 时, 暴露出
两类**静默失效**风险, 它们都不会抛异常, 只会让功能悄悄不工作:

1. **路由写死**: 旧 `_build_messages_url` 恒拼 `<base>/anthropic/v1/messages`。
   实测该路由在 Ollama v0.34.0 上是 **404**(`404 page not found`)。
   即"地址填对了, 仍然必然失败"。
2. **协议副本**: 本仓曾有 **4 份** `_post_anthropic` 复制件
   (`llm_commentary` / `incremental_learn` / `pre_drl_brief`,
   外加 `alpha_logics`/`factor_mad` 各自内联 POST)。改主模块不会改到副本 ⇒
   副本所在的模块在切换协议后单独失效, 而且**看起来只是"没输出"**。

因此本组用例锁三件事:
  · 协议解析与 URL 构造对三种协议都正确(且不再恒为 anthropic);
  · 三种**不同的返回体形状**都能被归一化成 Anthropic 形状
    —— 否则既有 4 处 `_parse_response`(都只读 `body["content"]`)会集体报
    "响应缺少 content";
  · 副本已改为委托, 不再各自实现协议(防止再次分叉)。

全部用例**不联网**: 只测纯函数与形状归一化。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import llm_commentary as lc  # noqa: E402


class TestResolveApiType(unittest.TestCase):
    """协议解析: 显式变量 > 端口/关键字推断 > anthropic 兜底。"""

    def _t(self, base, env=None):
        e = dict(env or {})
        with mock.patch.dict(os.environ, e, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            for k, v in e.items():
                os.environ[k] = v
            return lc._resolve_api_type(base)

    def test_explicit_env_wins_over_inference(self):
        """显式 LLM_API_TYPE 优先级最高 —— 即使地址看起来像别的协议。"""
        with mock.patch.dict(os.environ, {lc.ENV_API_TYPE: "ollama"}, clear=False):
            self.assertEqual(lc._resolve_api_type("https://api.minimaxi.com/anthropic"),
                             "ollama")
        with mock.patch.dict(os.environ, {lc.ENV_API_TYPE: "anthropic"}, clear=False):
            self.assertEqual(lc._resolve_api_type("http://192.168.1.5:11434"),
                             "anthropic")

    def test_ollama_inferred_from_default_port(self):
        """Ollama 默认端口 11434 是可靠的推断依据。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            self.assertEqual(lc._resolve_api_type("http://192.168.1.5:11434"),
                             "ollama")
            self.assertEqual(lc._resolve_api_type("http://127.0.0.1:11434/"),
                             "ollama")

    def test_openai_inferred_from_keyword(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            self.assertEqual(lc._resolve_api_type("https://api.openai.com"), "openai")
            self.assertEqual(
                lc._resolve_api_type("https://x/v1/chat/completions"), "openai")

    def test_defaults_to_anthropic_for_backward_compat(self):
        """未设变量且无从推断时**必须**回落到 anthropic。

        这是向后兼容的**关键**: 既有 MiniMax 部署未设 `LLM_API_TYPE`,
        若这里改成默认 openai/ollama, 那些部署会在无人改动配置的情况下静默失效。
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            self.assertEqual(lc._resolve_api_type("https://api.minimaxi.com/anthropic"),
                             "anthropic")
            self.assertEqual(lc._resolve_api_type(""), "anthropic")

    def test_invalid_explicit_value_falls_back_to_inference(self):
        """显式值写错(如 `ollma`)时不应崩, 而应回落推断。"""
        with mock.patch.dict(os.environ, {lc.ENV_API_TYPE: "ollma"}, clear=False):
            self.assertEqual(lc._resolve_api_type("http://192.168.1.5:11434"),
                             "ollama")


class TestBuildMessagesUrl(unittest.TestCase):
    """URL 构造: 每种协议落到各自的原生路由。"""

    def _u(self, base, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            for k, v in (env or {}).items():
                os.environ[k] = v
            return lc._build_messages_url(base)

    def test_ollama_uses_native_api_chat(self):
        """Ollama 走**原生** /api/chat。

        实测依据: 同一提示词同一模型, /api/chat 0.46s vs
        /v1/chat/completions 3.58s; 且原生路由的 `think=false` 能把思维链
        干净分离, OpenAI 兼容层则把思维链放在 message.reasoning。
        """
        self.assertEqual(self._u("http://192.168.1.5:11434"),
                         "http://192.168.1.5:11434/api/chat")

    def test_ollama_must_not_produce_the_anthropic_route(self):
        """回归护栏: 绝不能再拼出 `/anthropic/v1/messages`。

        这正是切换后 404 的那条路由 —— 该断言是本组用例的核心。
        """
        for b in ("http://192.168.1.5:11434", "http://192.168.1.5:11434/"):
            self.assertNotIn("/anthropic/", self._u(b))

    def test_openai_route_and_no_double_v1(self):
        self.assertEqual(self._u("https://api.openai.com"),
                         "https://api.openai.com/v1/chat/completions")
        self.assertEqual(self._u("https://api.openai.com/v1"),
                         "https://api.openai.com/v1/chat/completions")

    def test_anthropic_routes_preserved(self):
        """MiniMax 路径必须**逐字不变**, 否则等于悄悄改了线上配置。"""
        self.assertEqual(
            self._u("https://api.minimaxi.com/anthropic"),
            "https://api.minimaxi.com/anthropic/v1/messages")


class TestResponseNormalization(unittest.TestCase):
    """三种返回体形状 -> 统一 Anthropic 形状。

    统一后既有 `_parse_response`(只读 body["content"])无需改动即可消费
    任意协议的返回。若不统一, 会出现"连通了却报响应缺少 content"的半坏状态。
    """

    def test_ollama_shape(self):
        body = {"message": {"role": "assistant", "content": "hello"},
                "done": True}
        out = lc._to_anthropic_shape(body)
        self.assertEqual(out["content"], [{"type": "text", "text": "hello"}])

    def test_ollama_thinking_is_separated_not_merged(self):
        """思维链必须与正文**分离** —— 混进去会污染 JSON 抽取。"""
        body = {"message": {"role": "assistant", "content": '{"a":1}',
                            "thinking": "let me think..."}}
        out = lc._to_anthropic_shape(body)
        self.assertEqual(out["content"], [{"type": "text", "text": '{"a":1}'}])
        self.assertEqual(out["thinking"], "let me think...")
        text = "".join(b["text"] for b in out["content"])
        self.assertNotIn("think", text)

    def test_openai_shape(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        out = lc._to_anthropic_shape(body)
        self.assertEqual(out["content"], [{"type": "text", "text": "hi"}])

    def test_openai_reasoning_separated(self):
        body = {"choices": [{"message": {"content": '{"a":1}',
                                         "reasoning": "chain"}}]}
        out = lc._to_anthropic_shape(body)
        self.assertEqual(out["content"], [{"type": "text", "text": '{"a":1}'}])
        self.assertEqual(out["reasoning"], "chain")

    def test_anthropic_shape_passes_through_unchanged(self):
        body = {"content": [{"type": "text", "text": "ok"}], "id": "msg_1"}
        self.assertIs(lc._to_anthropic_shape(body), body)

    def test_unknown_shape_yields_empty_content_not_exception(self):
        """未知形状**不抛异常** —— 交由调用方统一报"缺少 content"。

        理由: 归一化层是**解析**而非校验。在此处 raise 会把失败点前移,
        使 4 处调用方的错误处理(它们都按"缺 content"兜底)全部失效。
        """
        out = lc._to_anthropic_shape({"weird": 1})
        self.assertEqual(out["content"], [])
        self.assertIn("_raw_keys", out)

    def test_non_dict_input_is_safe(self):
        self.assertEqual(lc._to_anthropic_shape(None), {"content": []})
        self.assertEqual(lc._to_anthropic_shape("x"), {"content": []})


class TestSplitSystem(unittest.TestCase):
    """system 在三种协议里的位置不同, 需要能正确提取。"""

    def test_extracts_and_removes_system(self):
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "U"}]
        sys_text, rest = lc._split_system(msgs)
        self.assertEqual(sys_text, "S")
        self.assertEqual(rest, [{"role": "user", "content": "U"}])

    def test_multiple_system_joined(self):
        msgs = [{"role": "system", "content": "A"},
                {"role": "system", "content": "B"}]
        sys_text, rest = lc._split_system(msgs)
        self.assertEqual(sys_text, "A\nB")
        self.assertEqual(rest, [])

    def test_no_system(self):
        sys_text, rest = lc._split_system([{"role": "user", "content": "U"}])
        self.assertEqual(sys_text, "")
        self.assertEqual(len(rest), 1)

    def test_tolerates_non_dict_and_empty(self):
        self.assertEqual(lc._split_system(None), ("", []))
        self.assertEqual(lc._split_system(["junk", {"role": "user"}])[1],
                         [{"role": "user"}])


class TestDuplicateImplementationsWereDelegated(unittest.TestCase):
    """防止协议实现再次分叉成多份。

    [2026-09-28] 本仓曾有 4 份 `_post_anthropic`。只改主模块时, 副本所在模块
    会**单独**在切换协议后失效, 且表现为"没有输出"而非报错 —— 极难发现。
    故这里静态断言: 副本必须是**委托**, 不得再自己构造 Anthropic 请求头。
    """

    _DELEGATED = ("pre_drl_brief.py", "incremental_learn.py")

    def test_shims_delegate_instead_of_reimplementing(self):
        src_root = Path(_SRC)
        for name in self._DELEGATED:
            body = (src_root / name).read_text(encoding="utf-8")
            self.assertIn("_post_messages", body,
                          f"{name} 的 _post_anthropic 应委托 _post_messages")
            # 不得再自带 Anthropic 专有请求头(那是"自己实现协议"的标志)
            self.assertNotIn('"x-api-key": api_key', body,
                             f"{name} 仍在自行构造 Anthropic 请求头 —— 协议实现又分叉了")

    def test_handwritten_post_sites_are_gone(self):
        """`alpha_logics` / `factor_mad` 原先各自内联 POST, 也应改走共享实现。"""
        src_root = Path(_SRC)
        for name in ("alpha_logics.py", "factor_mad.py"):
            body = (src_root / name).read_text(encoding="utf-8")
            self.assertIn("_post_messages", body, f"{name} 应使用共享的 _post_messages")
            self.assertNotIn('"anthropic-version": "2023-06-01"', body,
                             f"{name} 仍在写死 Anthropic 版本头")


class TestPostChatRequestShapes(unittest.TestCase):
    """`_post_chat` 发出去的**请求体/请求头**是否贴合各协议。

    为什么值得单独锁: 路由对了但请求体不对, 同样会失败, 而且失败信息更难读
    (服务端多半回 400/422, 看起来像"参数错", 不像"协议错")。三处差异:
      · Anthropic: `system` 是**顶层字段**, 鉴权用 `x-api-key` + 版本头;
      · OpenAI:    system 是**一条消息**, 鉴权用 `Authorization: Bearer`;
      · Ollama:    system 是一条消息, 且要带 `stream:false` 与 `think`。
    本组用例用假 `urlopen` 捕获真实 payload, **不联网**。
    """

    def _capture(self, base, env=None, api_key="K", system="S", user="U"):
        captured = {}

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b'{"content":[{"type":"text","text":"ok"}]}'

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Resp()

        e = dict(env or {})
        with mock.patch.dict(os.environ, e, clear=False):
            os.environ.pop(lc.ENV_API_TYPE, None)
            for k, v in e.items():
                os.environ[k] = v
            with mock.patch.object(lc.request, "urlopen", _fake_urlopen):
                lc._post_chat(base, api_key, "m1", system, user,
                              max_tokens=77, timeout=12.0)
        return captured

    def test_ollama_payload_has_stream_false_and_think_flag(self):
        c = self._capture("http://192.168.1.5:11434",
                          env={lc.ENV_API_TYPE: "ollama"})
        self.assertEqual(c["url"], "http://192.168.1.5:11434/api/chat")
        self.assertFalse(c["payload"]["stream"],
                         "Ollama 必须显式 stream=false, 否则返回 NDJSON 流")
        self.assertIn("think", c["payload"],
                      "thinking 模型需显式 think 开关, 否则思维链拖慢并可能干扰抽取")
        roles = [m["role"] for m in c["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user"],
                         "Ollama 的 system 应作为**一条消息**, 不是顶层字段")
        self.assertNotIn("max_tokens", c["payload"],
                         "Ollama 用 options.num_predict, 不接受 max_tokens")

    def test_ollama_sends_no_anthropic_headers(self):
        c = self._capture("http://192.168.1.5:11434",
                          env={lc.ENV_API_TYPE: "ollama"})
        self.assertNotIn("x-api-key", c["headers"])
        self.assertNotIn("anthropic-version", c["headers"])

    def test_openai_payload_uses_bearer_and_max_tokens(self):
        c = self._capture("https://api.openai.com/v1",
                          env={lc.ENV_API_TYPE: "openai"})
        self.assertEqual(c["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(c["headers"].get("authorization"), "Bearer K")
        self.assertEqual(c["payload"]["max_tokens"], 77)
        roles = [m["role"] for m in c["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user"])

    def test_anthropic_payload_uses_top_level_system(self):
        c = self._capture("https://api.minimaxi.com/anthropic",
                          env={lc.ENV_API_TYPE: "anthropic"})
        self.assertEqual(c["url"],
                         "https://api.minimaxi.com/anthropic/v1/messages")
        self.assertEqual(c["headers"].get("x-api-key"), "K")
        self.assertEqual(c["headers"].get("anthropic-version"), "2023-06-01")
        self.assertEqual(c["payload"]["system"], "S",
                         "Anthropic 的 system 是**顶层字段**")
        self.assertEqual([m["role"] for m in c["payload"]["messages"]], ["user"],
                         "Anthropic 的 messages 里不应含 system 条目")

    def test_timeout_is_forwarded(self):
        c = self._capture("http://192.168.1.5:11434",
                          env={lc.ENV_API_TYPE: "ollama"})
        self.assertEqual(c["timeout"], 12.0)


class TestResolveLlmConfig(unittest.TestCase):
    """配置前置校验 —— 直接测 `_resolve_llm_config`, 不经过 `generate_commentary`。

    改动前判据是 `if not base or not key`, 对本地模型是**假前提**:
    配好地址却没配 key 会直接降级成"缺少配置", 而实际完全可用。

    这条判据曾埋在 `generate_commentary` 内部 —— 要测它得先造 market /
    performance 一堆入参, 于是实践中没人测。抽成独立函数后才可直测。
    """

    _KEYS = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
             "OPENAI_TIMEOUT_SECONDS", lc.ENV_API_TYPE)

    def _cfg(self, **env):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in self._KEYS:
                os.environ.pop(k, None)
            for k, v in env.items():
                os.environ[k] = v
            # 避免真实 .env 被读进来干扰(本组只测判据本身)
            with mock.patch.object(lc, "_load_dotenv", lambda: None):
                return lc._resolve_llm_config()

    def test_ollama_allows_empty_key(self):
        cfg, err = self._cfg(OPENAI_BASE_URL="http://192.168.1.5:11434")
        self.assertIsNone(err, f"本地端点不应因缺 key 报错, 实际: {err}")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["api_type"], "ollama")
        self.assertEqual(cfg["key"], "")

    def test_anthropic_requires_key(self):
        cfg, err = self._cfg(OPENAI_BASE_URL="https://api.minimaxi.com/anthropic",
                             OPENAI_MODEL="MiniMax-M3")
        self.assertIsNone(cfg)
        self.assertEqual(err, "缺少 OPENAI_API_KEY")

    def test_missing_base_url_still_fails(self):
        """地址缺失**必须**仍然报错 —— 别把"放宽 key"顺手做成"什么都放过"。"""
        cfg, err = self._cfg(OPENAI_API_KEY="K")
        self.assertIsNone(cfg)
        self.assertEqual(err, "缺少 OPENAI_BASE_URL")

    def test_full_config_is_parsed(self):
        cfg, err = self._cfg(OPENAI_BASE_URL="http://192.168.1.5:11434",
                             OPENAI_API_KEY="K", OPENAI_MODEL="qwen3.6:35b",
                             OPENAI_TIMEOUT_SECONDS="180")
        self.assertIsNone(err)
        self.assertEqual(cfg["model"], "qwen3.6:35b")
        self.assertEqual(cfg["timeout"], 180.0)
        self.assertEqual(cfg["api_type"], "ollama")

    def test_generate_commentary_uses_the_helper(self):
        """静态断言: 入口必须走 helper, 否则判据会再次被埋回去。"""
        src = (Path(_SRC) / "llm_commentary.py").read_text(encoding="utf-8")
        self.assertIn("_resolve_llm_config()", src)
        self.assertIn("stage\": \"config\"", src)


class TestDotenvLoaderSemantics(unittest.TestCase):
    """`.env` 加载器: **读全部候选文件** + 先读者优先 + 不覆盖进程环境变量。

    [2026-09-28] 旧实现读到第一个存在的文件就 `return`。后果: 只要
    `research_trader/.env` 存在, 本仓 `.env` **永远不被读** ——
    把配置写进本仓 .env 会静默失效(文件在、格式对、就是不生效)。
    这类失效不报错, 只在功能层面表现为"改了没用"。

    这些用例把语义钉住, 并用**临时文件**构造场景, 不依赖真实 .env 内容。
    """

    def setUp(self):
        self._orig = lc._DEFAULT_ENV_PATHS
        self._saved = {}
        for k in ("T_HIGH", "T_LOW", "T_CONFLICT", "T_PROC"):
            self._saved[k] = os.environ.pop(k, None)

    def tearDown(self):
        lc._DEFAULT_ENV_PATHS = self._orig
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write(self, td, name, text):
        p = Path(td) / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_loads_all_candidate_files_not_just_the_first(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f1 = self._write(td, "a.env", "T_HIGH=1\nT_CONFLICT=from_a\n")
            f2 = self._write(td, "b.env", "T_LOW=2\nT_CONFLICT=from_b\n")
            lc._DEFAULT_ENV_PATHS = [f1, f2]
            lc._load_dotenv()
        self.assertEqual(os.environ.get("T_HIGH"), "1")
        self.assertEqual(os.environ.get("T_LOW"), "2",
                         "靠后的文件也必须被读到 —— 提前 return 会让它永远不生效")

    def test_earlier_file_wins_on_conflict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f1 = self._write(td, "a.env", "T_CONFLICT=from_a\n")
            f2 = self._write(td, "b.env", "T_CONFLICT=from_b\n")
            lc._DEFAULT_ENV_PATHS = [f1, f2]
            lc._load_dotenv()
        self.assertEqual(os.environ.get("T_CONFLICT"), "from_a")

    def test_process_env_is_never_overwritten(self):
        """进程环境变量优先级最高 —— 显式注入必须能压过 .env。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f1 = self._write(td, "a.env", "T_PROC=from_file\n")
            lc._DEFAULT_ENV_PATHS = [f1]
            os.environ["T_PROC"] = "from_process"
            lc._load_dotenv()
        self.assertEqual(os.environ.get("T_PROC"), "from_process")

    def test_missing_and_blank_paths_are_skipped(self):
        """空路径(环境变量未设时产生)与不存在的路径都不得抛异常。"""
        lc._DEFAULT_ENV_PATHS = ["", "/definitely/not/here.env"]
        lc._load_dotenv()  # 不抛即通过

    def test_repo_root_and_local_env_are_candidates(self):
        """静态/运行时断言: 本仓根 `.env` 必须在候选里。

        这是"两处都写"能生效的前提。曾因层级算错而漏掉本仓根, 症状是静默失效。
        """
        repo_root = Path(_SRC).resolve().parent
        cands = [os.path.abspath(p) for p in lc._DEFAULT_ENV_PATHS if p]
        self.assertIn(str(repo_root / ".env"), cands,
                      f"本仓根 .env 不在候选中: {cands}")


if __name__ == "__main__":
    unittest.main()

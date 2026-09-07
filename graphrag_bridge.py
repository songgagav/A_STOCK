# ============================================================
# graphrag_bridge.py -- GraphRAG 本地知识库接入 LLM 上下文
#
# 改造②: 把 GraphRAG 本地知识库(策略规则/历史复盘/投研笔记/绩效回溯)
#   检索上下文接入 pre_drl_brief / llm_commentary 的 evidence 链路.
#
# 能力:
#   build_kb_evidence(query, top_k)  调独立 venv 的 graphrag_retriever.py
#     对 query 做 LocalSearch, 返回 {available, source, kb_context, meta}.
#
# 设计约束 (与 graph_map 一致):
#   - 知识库或检索失败一律容错, 返回 {"available": False}, 绝不抛异常.
#   - 输出截断并限制 token, 防止打爆 LLM 上下文.
#   - 通过 subprocess 隔离开了导 graphrag (策略系统用系统 Python).
#     检索脚本运行一次约 3~8s (含加载索引 + embedding 查询), 用超时保护.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

_LOG = logging.getLogger("graphrag_bridge")

# GraphRAG 知识库目录 (含独立 venv 与索引产物)
_KB_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "graphrag-kb")
)
_KB_VENV_PY = os.path.join(_KB_ROOT, ".venv", "Scripts", "python.exe")
_KB_RETRIEVER = os.path.join(_KB_ROOT, "graphrag_retriever.py")

# 环境变量可覆盖 (便于不同机器)
_KB_ROOT = os.environ.get("GRAPHRAG_KB_ROOT", _KB_ROOT)
_KB_VENV_PY = os.environ.get("GRAPHRAG_KB_VENV_PY",
                              os.path.join(_KB_ROOT, ".venv", "Scripts", "python.exe"))
_KB_RETRIEVER = os.environ.get("GRAPHRAG_KB_RETRIEVER",
                                os.path.join(_KB_ROOT, "graphrag_retriever.py"))

_TIMEOUT_S = float(os.environ.get("GRAPHRAG_KB_TIMEOUT_S", "30") or 30)


def _env_snapshot(_log):
    pass  # noqa: B018


def build_kb_evidence(query: str,
                      top_k: int = 8,
                      community_level: int = 2,
                      max_context_chars: int = 6000,
                      mode: str = "local") -> dict:
    """对 query 检索 GraphRAG 本地知识库, 组装可拼进 evidence 的 kb 块.

    mode='local' : LocalSearch 图检索(默认).
    mode='global': 全库关键词兜底(社区报告+文本单元), 用于明确指向具体
      数值/规则(如某次回测的 maxDD/夏普)而 local 召回覆盖不足的场景.

    Returns
    -------
    dict: {
      "available": bool,
      "source": str,
      "query": str,
      "kb_context": str,   # 检索到的知识片段(截断), 供 LLM 引用
      "n_sources": int,
      "meta": {...},       # engine / elapsed / error
    }
    """
    if not os.path.exists(_KB_RETRIEVER):
        return {"available": False, "source": "graphrag_retriever 缺失",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": "检索脚本不存在"}}
    if not os.path.exists(_KB_VENV_PY):
        return {"available": False, "source": "graphrag venv 缺失",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": "venv python 不存在"}}

    cmd = [_KB_VENV_PY, _KB_RETRIEVER,
           "--query", query,
           "--top-k", str(top_k),
           "--community-level", str(community_level),
           "--mode", mode]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_TIMEOUT_S,
            cwd=_KB_ROOT,
            creationflags=0,  # noqa: F821
        )
    except subprocess.TimeoutExpired as e:
        _LOG.warning("GraphRAG 检索超时(%.0fs): %s", _TIMEOUT_S, query)
        return {"available": False, "source": "graphrag retriever",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": f"TimeoutExpired: {e}"}}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("GraphRAG 检索启动失败: %s", e)
        return {"available": False, "source": "graphrag retriever",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": f"{type(e).__name__}: {e}"}}

    if proc.returncode != 0:
        # 非零退出: 抓 stderr 尾部用于诊断, 但只截断返回
        err = (proc.stderr or proc.stdout or "")[-500:]
        _LOG.warning("GraphRAG 检索失败 rc=%s: %s", proc.returncode, err[:200])
        return {"available": False, "source": "graphrag retriever",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": f"rc={proc.returncode}: {err[:200]}"}}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _LOG.warning("GraphRAG 检索输出非 JSON: %s", e)
        return {"available": False, "source": "graphrag retriever",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": {"error": f"JSONDecodeError: {e}"}}

    if not data.get("ok"):
        meta = data.get("meta") or {}
        return {"available": False, "source": "graphrag retriever",
                "query": query, "kb_context": "", "n_sources": 0,
                "meta": meta}

    sources = data.get("sources") or []
    # GraphRAG 的 context_text(context_chunks) 是面向 LLM 的标准上下文:
    # 包含 entities 描述/relationships/text_units 的完整原始数值(如回测夏普/回撤).
    # 需优先采用, 否则会被 sources 里偏泛化的关系描述挤掉导致数值丢失.
    parts: list[str] = []
    full = (data.get("context_text") or "").strip()
    if full:
        parts.append(full)
    # 结构化 sources 作引用补充 (entities/relationships/社区报告等)
    for s in sources:
        st = s.get("type", "source")
        content = (s.get("content") or "").strip()
        if not content:
            continue
        s_id = s.get("id") or ""
        parts.append(f"[{st}]" + (f" ({s_id})" if s_id else "") + f": {content}")
    kb_context = "\n".join(parts)
    if len(kb_context) > max_context_chars:
        kb_context = kb_context[:max_context_chars] + "\n...(截断)"

    meta = data.get("meta") or {}
    return {
        "available": bool(kb_context),
        "source": "GraphRAG 本地知识库 (LocalSearch)",
        "query": query,
        "kb_context": kb_context,
        "n_sources": len(sources),
        "meta": {
            "engine": meta.get("engine"),
            "community_level": meta.get("community_level"),
            "elapsed_s": meta.get("elapsed_s"),
            "collected": meta.get("collected"),
        },
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="GraphRAG 知识库桥接 (调试用)")
    ap.add_argument("--query", required=True, help="检索问题")
    a = ap.parse_args()
    r = build_kb_evidence(a.query)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
# ============================================================
# alpha_logics.py -- AlphaLogics 分层架构: 逻辑发现 -> 因子生成
#
# 核心思想 (AlphaLogics 论文):
#   将量化因子挖掘拆分为两个阶段:
#     1. Logic Discovery:  LLM 从市场数据中提取经济逻辑 (Economic Rationales)
#     2. Factor Generation: 工具模块基于逻辑生成具体的因子结构
#
# 与现有架构的融合:
#   现有链路: LLM分析 -> pre_drl_brief -> DRL微调
#   AlphaLogics: LLM提出假说 -> 工具验证 -> 因子入库 -> 权重优化
#
# 工作流:
#   1. 输入: 市场证据 (market_panel + symbolic_ta + 知识库)
#   2. LLM 提出 2-5 个候选经济逻辑
#   3. 每个逻辑由工具模块生成因子结构 + IC 回测验证
#   4. LLM 基于回测结果评估并排序
#   5. 通过验证的因子写入 factor_library 候选池
#
# 设计约束:
#   - LLM 不直接输出预测信号, 只输出可解释的市场逻辑
#   - 因子计算由工具模块完成, 保证可复现
#   - 所有假说必须经过 IC 回测验证才能进入候选池
# ============================================================

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request

_LOG = logging.getLogger("alpha_logics")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DUCKDB_PATH  # noqa: E402

from llm_commentary import _load_dotenv, _build_messages_url, _extract_first_json_object, _repair_json  # noqa: E402

# ============================================================
# 配置
# ============================================================
ALPHA_LOGICS_DIR = os.path.join(DATA_DIR, "alpha_logics")
RATIONALE_HISTORY = os.path.join(ALPHA_LOGICS_DIR, "rationale_history.json")
FACTOR_CANDIDATES = os.path.join(ALPHA_LOGICS_DIR, "factor_candidates.json")

_LOADED = False

_SYSTEM_PROMPT = """你是 A 股量化因子研究员。你的任务是从市场数据中发现可复现的经济逻辑 (Economic Rationales), 而不是直接生成因子代码。

输入: 市场情绪面板、符号化趋势分析、知识库检索结果、近期因子 IC 表现

输出: 严格的 JSON 格式, 包含:
{
  "rationales": [
    {
      "id": "r_001",
      "hypothesis": "string  // 假说描述, 如 '低波动率股票在震荡市中超额收益显著'",
      "mechanism": "string  // 经济机制, 如 '低波动个股在震荡市中被避险资金偏好, 形成相对收益'",
      "market_condition": "string  // 适用市场状态: bull/bear/ranging",
      "data_requirements": ["string"],  // 所需数据: 如 ["close", "volume", "high", "low"]
      "factor_sketch": "string  // 因子结构草图, 如 '20日收益标准差, 取负值, 做截面排名'",
      "expected_ic_sign": "positive | negative",
      "confidence": 0.0  // 0..1, 假说置信度
    }
  ]
}

约束:
- 每个假说必须基于 evidence 中的具体数据, 禁止凭空猜测
- factor_sketch 必须是可计算的结构描述, 不能是抽象概念
- confidence 必须基于 evidence 中的实证支持强度
- 只输出 JSON, 不要任何 Markdown / 解释"""


def _ensure_loaded():
    global _LOADED
    if _LOADED:
        return
    _load_dotenv()
    _LOADED = True


def _call_llm(messages: list[dict], max_tokens: int = 4096) -> str | None:
    _ensure_loaded()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "minimax-m3").strip()
    if not base or not api_key:
        _LOG.warning("alpha_logics: 缺少 LLM 配置")
        return None

    url = _build_messages_url(base)
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with request.urlopen(req, timeout=90) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        content = r.get("content", [])
        if isinstance(content, list) and content:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        return str(content)
    except Exception as e:
        _LOG.warning("alpha_logics: LLM 调用失败: %s", e)
        return None


# ============================================================
# 阶段 1: 经济逻辑发现
# ============================================================
def discover_rationales(day: str | None = None,
                        max_rationales: int = 5) -> dict:
    """LLM 从市场证据中提取经济逻辑假说.

    Returns:
        {"ok": bool, "rationales": [...], "evidence": {...}, "day": str}
    """
    day = day or date.today().strftime("%Y-%m-%d")
    _ensure_loaded()

    # 收集证据
    evidence = _collect_evidence(day)
    if not evidence.get("ok"):
        return {"ok": False, "rationales": [], "day": day,
                "error": evidence.get("error", "证据收集失败")}

    # 构建 prompt
    user_msg = _build_discovery_prompt(evidence, day, max_rationales)
    messages = [
        {"role": "user", "content": _SYSTEM_PROMPT + "\n\n" + user_msg},
    ]

    text = _call_llm(messages)
    if not text:
        return {"ok": False, "rationales": [], "day": day,
                "error": "LLM 调用失败"}

    # 解析
    raw = _extract_first_json_object(text)
    if not raw:
        return {"ok": False, "rationales": [], "day": day,
                "error": "LLM 输出非 JSON"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        if repaired:
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                return {"ok": False, "rationales": [], "day": day,
                        "error": "JSON 解析失败"}
        else:
            return {"ok": False, "rationales": [], "day": day,
                    "error": "JSON 解析失败"}

    rationales = data.get("rationales", [])[:max_rationales]

    # 存历史
    _save_rationale_history(day, rationales)

    return {"ok": True, "rationales": rationales, "day": day,
            "n": len(rationales)}


def _collect_evidence(day: str) -> dict:
    """收集 LLM 所需的市场证据."""
    from agent_tools import execute_parallel

    calls = [
        ("market_panel", {"day": day}),
        ("symbolic_ta", {"day": day}),
        ("graphrag_search", {"query": "A股因子表现 市场风格 近期alpha"}),
    ]
    results = execute_parallel(calls)
    panel, sym_ta, kb = results[0], results[1], results[2]

    return {
        "ok": True,
        "market_panel": panel,
        "symbolic_ta": sym_ta,
        "knowledge_base": kb.get("kb_context", "")[:2000] if kb.get("ok") else "",
        "day": day,
    }


def _build_discovery_prompt(evidence: dict, day: str, max_n: int) -> str:
    panel = evidence.get("market_panel", {})
    sym_ta = evidence.get("symbolic_ta", {})
    kb = evidence.get("knowledge_base", "")

    parts = [f"## 任务: 基于 {day} 的市场证据, 提出 {max_n} 个可验证的经济逻辑假说\n"]

    parts.append("## 市场情绪面板")
    parts.append(json.dumps(panel, ensure_ascii=False, indent=2, default=str)[:1500])

    parts.append("\n## 符号化趋势分析")
    parts.append(json.dumps(sym_ta, ensure_ascii=False, indent=2, default=str)[:1000])

    if kb:
        parts.append("\n## 知识库检索")
        parts.append(kb[:1500])

    parts.append(f"\n## 要求: 提出 {max_n} 个假说, 每个假说包含假设/机制/适用条件/因子草图/置信度")
    return "\n".join(parts)


def _save_rationale_history(day: str, rationales: list[dict]):
    os.makedirs(ALPHA_LOGICS_DIR, exist_ok=True)
    entry = {"day": day, "n": len(rationales), "rationales": rationales,
             "timestamp": datetime.now().isoformat()}
    history = []
    if os.path.exists(RATIONALE_HISTORY):
        try:
            with open(RATIONALE_HISTORY, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    history.append(entry)
    if len(history) > 100:
        history = history[-100:]
    with open(RATIONALE_HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ============================================================
# 阶段 2: 因子生成与验证
# ============================================================
def generate_and_validate(rationale: dict, day: str | None = None) -> dict:
    """基于经济逻辑假说, 生成因子并 IC 回测验证.

    Args:
        rationale: 来自 discover_rationales 的单个假说

    Returns:
        {"ok": bool, "rationale_id": str, "factor_spec": {...},
         "ic_backtest": {...}, "passed": bool}
    """
    day = day or date.today().strftime("%Y-%m-%d")

    # 1. 解析因子草图 -> 因子参数
    spec = _parse_factor_sketch(rationale)
    if not spec.get("ok"):
        return {"ok": False, "rationale_id": rationale.get("id", "unknown"),
                "error": spec.get("error", "因子草图解析失败")}

    # 2. IC 回测验证
    from agent_tools import execute as tool_execute

    ic_result = tool_execute("ic_backtest", {
        "factor_name": spec.get("base_factor", "vol"),
        "k": spec.get("window", 20),
        "start": "2015-01-01",
        "end": day,
    })

    # 3. 判断是否通过
    passed = _evaluate_validation(ic_result, rationale)

    result = {
        "ok": True,
        "rationale_id": rationale.get("id", "unknown"),
        "hypothesis": rationale.get("hypothesis", ""),
        "factor_spec": spec,
        "ic_backtest": ic_result,
        "passed": passed,
        "day": day,
    }

    # 4. 通过验证的因子写入候选池
    if passed:
        _save_factor_candidate(result)

    return result


def _parse_factor_sketch(rationale: dict) -> dict:
    """将 LLM 的因子草图解析为可计算的因子参数."""
    sketch = (rationale.get("factor_sketch", "") or
              rationale.get("hypothesis", ""))

    # 从草图中提取关键参数
    window = _extract_number(sketch, 20)
    base_factor = "vol"

    lower = sketch.lower()
    if "波动" in lower or "标准差" in lower or "volatil" in lower:
        base_factor = "vol"
    elif "动量" in lower or "momentum" in lower or "涨幅" in lower:
        base_factor = "mom"
    elif "反转" in lower or "reversal" in lower or "跌幅" in lower:
        base_factor = "reversal"

    ic_sign = rationale.get("expected_ic_sign", "positive")
    direction = "low" if ic_sign == "negative" else "high"

    return {
        "ok": True,
        "base_factor": base_factor,
        "window": window,
        "direction": direction,
        "sketch": sketch,
        "market_condition": rationale.get("market_condition", "ranging"),
    }


def _extract_number(text: str, default: int = 20) -> int:
    nums = re.findall(r"(\d+)\s*日", text)
    return int(nums[0]) if nums else default


def _evaluate_validation(ic_result: dict, rationale: dict) -> bool:
    """评估因子是否通过 IC 回测验证."""
    if not ic_result.get("ok"):
        return False
    icir = abs(ic_result.get("icir", 0))
    ic_mean = abs(ic_result.get("rank_ic_mean", 0))
    return icir > 0.3 and ic_mean > 0.005


def _save_factor_candidate(result: dict):
    os.makedirs(ALPHA_LOGICS_DIR, exist_ok=True)
    candidates = []
    if os.path.exists(FACTOR_CANDIDATES):
        try:
            with open(FACTOR_CANDIDATES, "r", encoding="utf-8") as f:
                candidates = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    entry = {
        "rationale_id": result["rationale_id"],
        "hypothesis": result["hypothesis"],
        "factor_spec": result["factor_spec"],
        "ic_backtest": {k: v for k, v in result["ic_backtest"].items()
                        if k != "ic_series"},
        "day": result["day"],
        "timestamp": datetime.now().isoformat(),
    }
    candidates.append(entry)
    if len(candidates) > 50:
        candidates = candidates[-50:]
    with open(FACTOR_CANDIDATES, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)


# ============================================================
# 阶段 3: LLM 审查与排序
# ============================================================
def review_candidates(day: str | None = None) -> dict:
    """LLM 审查所有通过验证的候选因子, 排序并给出建议."""
    day = day or date.today().strftime("%Y-%m-%d")

    candidates = []
    if os.path.exists(FACTOR_CANDIDATES):
        try:
            with open(FACTOR_CANDIDATES, "r", encoding="utf-8") as f:
                candidates = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if not candidates:
        return {"ok": True, "reviewed": [], "day": day,
                "message": "无候选因子"}

    recent = candidates[-20:]
    review_prompt = f"""审查以下候选因子 (按 IC 回测结果排序):

{json.dumps(recent, ensure_ascii=False, indent=2, default=str)[:3000]}

输出 JSON:
{{
  "reviewed": [
    {{
      "rationale_id": "string",
      "rank": 1,
      "recommendation": "adopt | monitor | reject",
      "reasoning": "string  // 简短理由",
      "suggested_weight": 0.0  // 建议权重 0..1
    }}
  ],
  "summary": "string  // 整体审查摘要"
}}"""

    messages = [{"role": "user", "content": review_prompt}]
    text = _call_llm(messages)
    if not text:
        return {"ok": False, "reviewed": [], "day": day,
                "error": "LLM 调用失败"}

    raw = _extract_first_json_object(text)
    if not raw:
        return {"ok": False, "reviewed": [], "day": day,
                "error": "LLM 输出非 JSON"}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        if repaired:
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                return {"ok": False, "reviewed": [], "day": day,
                        "error": "JSON 解析失败"}
        else:
            return {"ok": False, "reviewed": [], "day": day,
                    "error": "JSON 解析失败"}

    return {"ok": True, "reviewed": data.get("reviewed", []),
            "summary": data.get("summary", ""), "day": day,
            "n_candidates": len(candidates)}


# ============================================================
# 完整工作流
# ============================================================
def run_alpha_logics_pipeline(day: str | None = None,
                              max_rationales: int = 5) -> dict:
    """运行完整 AlphaLogics 流水线: 发现 -> 生成 -> 验证 -> 审查.

    Returns:
        {"ok": bool, "discovery": {...}, "validation": [...],
         "review": {...}, "elapsed_s": float}
    """
    t0 = time.perf_counter()
    day = day or date.today().strftime("%Y-%m-%d")

    # 阶段 1: 逻辑发现
    discovery = discover_rationales(day, max_rationales=max_rationales)
    if not discovery.get("ok"):
        return {"ok": False, "discovery": discovery,
                "validation": {"n_tested": 0, "n_passed": 0, "results": []},
                "review": {}, "elapsed_s": round(time.perf_counter() - t0, 2),
                "error": discovery.get("error")}

    # 阶段 2: 因子生成与验证
    validations = []
    for r in discovery.get("rationales", []):
        v = generate_and_validate(r, day)
        validations.append(v)

    passed = [v for v in validations if v.get("passed")]

    # 阶段 3: 审查
    review = review_candidates(day) if passed else {"ok": True, "reviewed": []}

    return {
        "ok": True,
        "day": day,
        "discovery": {"n_rationales": discovery.get("n", 0)},
        "validation": {
            "n_tested": len(validations),
            "n_passed": len(passed),
            "results": validations,
        },
        "review": review,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


# ============================================================
# 命令行调试
# ============================================================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AlphaLogics 因子发现")
    ap.add_argument("--day", help="YYYY-MM-DD")
    ap.add_argument("--pipeline", action="store_true",
                    help="运行完整流水线")
    ap.add_argument("--discover", action="store_true",
                    help="仅运行逻辑发现")
    ap.add_argument("--review", action="store_true",
                    help="仅运行候选审查")
    args = ap.parse_args()

    d = args.day or date.today().strftime("%Y-%m-%d")

    if args.pipeline:
        result = run_alpha_logics_pipeline(d)
    elif args.review:
        result = review_candidates(d)
    elif args.discover:
        result = discover_rationales(d)
    else:
        ap.print_help()
        sys.exit(0)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

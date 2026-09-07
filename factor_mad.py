# ============================================================
# factor_mad.py -- FactorMAD 多Agent辩论机制
#
# 核心思想 (FactorMAD 论文):
#   通过多个 LLM Agent 围绕因子信号进行结构化辩论 (Debate),
#   有效减少单模型的推理错误, 提高因子的可解释性和质量.
#
# 与现有架构的融合:
#   在"三模型融合预测"验证环节引入辩论机制:
#     - 技术面 Agent: 从量价角度批判信号
#     - 基本面 Agent: 从估值/财务角度批判信号
#     - 资金面 Agent: 从资金流向/筹码角度批判信号
#     - 仲裁 Agent: 综合各方论点, 输出最终共识
#
# 辩论流程:
#   1. 输入: 融合预测信号 + 市场证据 + 知识库
#   2. 第 1 轮: 正方 (技术面) 论证, 反方 (基本面/资金面) 批判
#   3. 第 2 轮: 各方回应对方论点, 修正立场
#   4. 仲裁: 综合各方论点, 输出共识评分和信号调整
#
# 设计约束:
#   - 每个 Agent 必须基于 evidence 中的具体数据, 禁止虚构
#   - 辩论输出必须可追溯 (谁说了什么, 基于什么证据)
#   - 共识评分直接用于调整融合预测信号的置信度
# ============================================================

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Any
from urllib import error, request

_LOG = logging.getLogger("factor_mad")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR  # noqa: E402
from llm_commentary import _load_dotenv, _build_messages_url, _extract_first_json_object, _repair_json  # noqa: E402

# ============================================================
# 配置
# ============================================================
MAD_DIR = os.path.join(DATA_DIR, "factor_mad")
DEBATE_HISTORY = os.path.join(MAD_DIR, "debate_history.json")

_LOADED = False

# Agent 角色定义
AGENT_ROLES = {
    "technical": {
        "name": "技术面分析师",
        "perspective": "量价关系/趋势/波动率/支撑阻力/技术指标",
        "system_prompt": """你是 A 股技术面分析师。你从量价关系、趋势、波动率、技术指标角度评估因子信号。

职责:
1. 论证信号是否与当前技术形态一致
2. 指出技术面可能的风险点 (如假突破/背离/缩量上涨)
3. 基于 evidence 中的具体数据给出技术面评分 (0..1)

输出 JSON:
{
  "agent": "technical",
  "stance": "support | oppose | neutral",
  "score": 0.0,
  "arguments": ["string"],
  "evidence_refs": ["string"],
  "confidence": 0.0
}""",
    },
    "fundamental": {
        "name": "基本面分析师",
        "perspective": "估值/盈利/成长性/财务质量/行业地位",
        "system_prompt": """你是 A 股基本面分析师。你从估值、盈利、成长性、财务质量角度评估因子信号。

职责:
1. 论证信号是否与基本面逻辑一致
2. 指出基本面风险 (如高估值陷阱/盈利质量差/行业下行)
3. 基于 evidence 中的具体数据给出基本面评分 (0..1)

输出 JSON:
{
  "agent": "fundamental",
  "stance": "support | oppose | neutral",
  "score": 0.0,
  "arguments": ["string"],
  "evidence_refs": ["string"],
  "confidence": 0.0
}""",
    },
    "capital_flow": {
        "name": "资金面分析师",
        "perspective": "资金流向/筹码分布/北向资金/主力动向/换手率",
        "system_prompt": """你是 A 股资金面分析师。你从资金流向、筹码分布、主力动向角度评估因子信号。

职责:
1. 论证信号是否得到资金面验证
2. 指出资金面风险 (如主力出货/散户接盘/流动性枯竭)
3. 基于 evidence 中的具体数据给出资金面评分 (0..1)

输出 JSON:
{
  "agent": "capital_flow",
  "stance": "support | oppose | neutral",
  "score": 0.0,
  "arguments": ["string"],
  "evidence_refs": ["string"],
  "confidence": 0.0
}""",
    },
}

_ARBITER_PROMPT = """你是 A 股量化策略仲裁员。你综合三位分析师(技术面/基本面/资金面)的辩论结果, 输出最终共识。

规则:
1. 各方的评分和论点权重相等优先
2. 如果各方分歧大 (方差 > 0.15), 降低共识置信度
3. 如果某方 confidence 明显低于其他方, 其论点权重减半
4. 最终信号调整必须在 [-0.3, 0.3] 范围内, 保守调整

输出 JSON:
{
  "consensus_score": 0.0,
  "signal_adjustment": 0.0,
  "consensus_confidence": 0.0,
  "majority_stance": "support | oppose | split",
  "key_agreements": ["string"],
  "key_disagreements": ["string"],
  "final_recommendation": "string"
}"""


def _ensure_loaded():
    global _LOADED
    if _LOADED:
        return
    _load_dotenv()
    _LOADED = True


def _call_llm(messages: list[dict], max_tokens: int = 2048) -> str | None:
    _ensure_loaded()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "minimax-m3").strip()
    if not base or not api_key:
        return None

    url = _build_messages_url(base)
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    data = json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with request.urlopen(req, timeout=60) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        content = r.get("content", [])
        if isinstance(content, list) and content:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        return str(content)
    except Exception as e:
        _LOG.warning("factor_mad: LLM 调用失败: %s", e)
        return None


def _parse_agent_response(text: str | None) -> dict | None:
    if not text:
        return None
    raw = _extract_first_json_object(text)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
    return None


# ============================================================
# 证据收集
# ============================================================
def _collect_debate_evidence(signal: dict, day: str) -> dict:
    from agent_tools import execute_parallel

    symbols = [s.get("canon", "") for s in signal.get("top_n", [])[:5]]
    calls = [
        ("market_panel", {"day": day}),
        ("symbolic_ta", {"day": day}),
        ("knowledge_graph", {"canons": symbols, "day": day}),
        ("graphrag_search", {"query": "A股因子信号 风格轮动 量价关系"}),
        ("spc_check", {"day": day}),
    ]
    results = execute_parallel(calls)

    return {
        "ok": True,
        "market_panel": results[0],
        "symbolic_ta": results[1],
        "knowledge_graph": results[2],
        "knowledge_base": results[3].get("kb_context", "")[:1500]
        if results[3].get("ok") else "",
        "spc_check": results[4],
        "day": day,
    }


# ============================================================
# 辩论执行
# ============================================================
def debate(signal: dict, day: str | None = None,
           rounds: int = 2) -> dict:
    """对融合预测信号进行多Agent辩论.

    Args:
        signal: 融合预测信号, 至少包含 top_n (持仓列表) 和 scores
        day: 日期
        rounds: 辩论轮次 (默认 2 轮)

    Returns:
        {"ok": bool, "rounds": [...], "consensus": {...}, "signal_adjustment": float}
    """
    day = day or date.today().strftime("%Y-%m-%d")
    _ensure_loaded()

    evidence = _collect_debate_evidence(signal, day)
    if not evidence.get("ok"):
        return {"ok": False, "error": "证据收集失败", "day": day}

    debate_log = []
    agent_positions = {}

    # 第 1 轮: 各方独立评估
    round1 = _run_debate_round(1, signal, evidence, agent_positions,
                               previous_round=None)
    debate_log.append(round1)
    agent_positions = round1.get("positions", {})

    # 第 2 轮: 各方回应对方论点
    if rounds >= 2:
        round2 = _run_debate_round(2, signal, evidence, agent_positions,
                                   previous_round=round1)
        debate_log.append(round2)
        agent_positions = round2.get("positions", {})

    # 仲裁
    consensus = _run_arbitration(signal, evidence, debate_log)

    # 保存记录
    _save_debate_history(day, signal, debate_log, consensus)

    return {
        "ok": True,
        "day": day,
        "rounds": debate_log,
        "consensus": consensus,
        "signal_adjustment": consensus.get("signal_adjustment", 0.0),
    }


def _run_debate_round(round_num: int, signal: dict, evidence: dict,
                      prev_positions: dict,
                      previous_round: dict | None = None) -> dict:
    round_prompt = _build_round_prompt(round_num, signal, evidence,
                                       prev_positions, previous_round)

    positions = {}
    for agent_id, role in AGENT_ROLES.items():
        messages = [
            {"role": "user", "content": role["system_prompt"] + "\n\n" + round_prompt},
        ]
        text = _call_llm(messages)
        parsed = _parse_agent_response(text)
        if parsed:
            positions[agent_id] = parsed
        else:
            positions[agent_id] = {
                "agent": agent_id, "stance": "neutral", "score": 0.5,
                "arguments": ["LLM 响应解析失败"], "evidence_refs": [],
                "confidence": 0.3,
            }

    # 计算分歧度
    scores = [p.get("score", 0.5) for p in positions.values()]
    variance = float(sum((s - sum(scores) / len(scores)) ** 2
                        for s in scores) / len(scores)) if scores else 0

    return {
        "round": round_num,
        "positions": positions,
        "divergence": round(variance, 4),
        "n_agents": len(positions),
    }


def _build_round_prompt(round_num: int, signal: dict, evidence: dict,
                        prev_positions: dict,
                        previous_round: dict | None = None) -> str:
    parts = [f"## 辩论第 {round_num} 轮\n"]

    # 信号上下文
    top_n = signal.get("top_n", [])
    if top_n:
        parts.append("## 当前持仓信号")
        parts.append(json.dumps([{
            "canon": s.get("canon"), "name": s.get("name"),
            "score": s.get("score"), "signal": s.get("signal"),
            "fml": s.get("fml"),
        } for s in top_n[:5]], ensure_ascii=False, indent=2))

    # 市场证据
    parts.append("\n## 市场情绪面板")
    parts.append(json.dumps(evidence.get("market_panel", {}),
                            ensure_ascii=False, indent=2, default=str)[:1000])

    parts.append("\n## 符号化趋势")
    parts.append(json.dumps(evidence.get("symbolic_ta", {}),
                            ensure_ascii=False, indent=2, default=str)[:800])

    if evidence.get("knowledge_base"):
        parts.append("\n## 知识库")
        parts.append(str(evidence["knowledge_base"])[:1000])

    # 前一轮论点 (第 2 轮)
    if previous_round and round_num >= 2:
        parts.append("\n## 上一轮其他Agent的论点 (请回应)")
        for agent_id, pos in previous_round.get("positions", {}).items():
            parts.append(f"\n### {AGENT_ROLES.get(agent_id, {}).get('name', agent_id)}")
            parts.append(f"- 立场: {pos.get('stance')}")
            parts.append(f"- 评分: {pos.get('score')}")
            parts.append(f"- 论点: {json.dumps(pos.get('arguments', []), ensure_ascii=False)}")

    parts.append("\n## 请输出你的评估 JSON")
    return "\n".join(parts)


def _run_arbitration(signal: dict, evidence: dict,
                     debate_log: list[dict]) -> dict:
    all_positions = {}
    for r in debate_log:
        all_positions.update(r.get("positions", {}))

    arb_prompt = f"""## 辩论总结与仲裁

### 各方最终立场
{json.dumps(all_positions, ensure_ascii=False, indent=2)[:2000]}

### 原始信号
{json.dumps(signal.get("scores", {}), ensure_ascii=False, indent=2)[:500]}

请输出仲裁 JSON:"""

    messages = [{"role": "user", "content": _ARBITER_PROMPT + "\n\n" + arb_prompt}]
    text = _call_llm(messages)
    parsed = _parse_agent_response(text)
    if parsed:
        return parsed

    return {
        "consensus_score": 0.5,
        "signal_adjustment": 0.0,
        "consensus_confidence": 0.3,
        "majority_stance": "split",
        "key_agreements": ["仲裁 LLM 响应解析失败"],
        "key_disagreements": [],
        "final_recommendation": "无法仲裁, 维持原信号",
    }


def _save_debate_history(day: str, signal: dict,
                         debate_log: list[dict], consensus: dict):
    os.makedirs(MAD_DIR, exist_ok=True)
    entry = {
        "day": day,
        "timestamp": datetime.now().isoformat(),
        "signal_summary": {
            "n_positions": len(signal.get("top_n", [])),
            "basket_signal": signal.get("basket_signal"),
        },
        "consensus": consensus,
        "n_rounds": len(debate_log),
    }
    history = []
    if os.path.exists(DEBATE_HISTORY):
        try:
            with open(DEBATE_HISTORY, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    history.append(entry)
    if len(history) > 100:
        history = history[-100:]
    with open(DEBATE_HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ============================================================
# 快速辩论 (轻量模式, 不调 LLM, 基于规则)
# ============================================================
def quick_debate(signal: dict, day: str | None = None) -> dict:
    """轻量辩论: 不调 LLM, 基于市场状态和信号规则直接输出共识.

    用于不想消耗 LLM token 的场景, 或在 LLM 不可用时的降级方案.
    """
    day = day or date.today().strftime("%Y-%m-%d")

    from agent_tools import execute_parallel

    calls = [
        ("symbolic_ta", {"day": day}),
        ("market_panel", {"day": day}),
    ]
    results = execute_parallel(calls)
    sym_ta = results[0]
    panel = results[1]

    market_state = sym_ta.get("market_state", "ranging")
    sentiment = panel.get("sentiment_score", 50.0) if panel.get("ok") else 50.0

    # 规则: 震荡市 + 中性情绪 -> 保守评分
    #       牛市 + 乐观情绪 -> 积极评分
    #       熊市 + 恐慌情绪 -> 保守评分
    if market_state == "bull" and sentiment > 60:
        consensus_score = 0.75
        adjustment = 0.1
    elif market_state == "bear" or sentiment < 35:
        consensus_score = 0.35
        adjustment = -0.15
    else:
        consensus_score = 0.55
        adjustment = 0.0

    return {
        "ok": True,
        "day": day,
        "mode": "quick",
        "consensus": {
            "consensus_score": consensus_score,
            "signal_adjustment": adjustment,
            "consensus_confidence": 0.6,
            "majority_stance": "support" if adjustment >= 0 else "oppose",
            "key_agreements": [f"市场状态: {market_state}, 情绪: {sentiment:.0f}"],
            "key_disagreements": [],
            "final_recommendation": f"基于规则: {market_state}市 + 情绪{sentiment:.0f}, "
                                    f"调整={adjustment:+.2f}",
        },
        "signal_adjustment": adjustment,
        "rounds": [],
    }


# ============================================================
# 命令行调试
# ============================================================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="FactorMAD 多Agent辩论")
    ap.add_argument("--day", help="YYYY-MM-DD")
    ap.add_argument("--quick", action="store_true", help="快速规则模式")
    ap.add_argument("--signal-file", help="信号 JSON 文件路径")
    args = ap.parse_args()

    d = args.day or date.today().strftime("%Y-%m-%d")

    signal = {"top_n": [], "scores": {}}
    if args.signal_file and os.path.exists(args.signal_file):
        with open(args.signal_file, "r", encoding="utf-8") as f:
            signal = json.load(f)

    if args.quick:
        result = quick_debate(signal, d)
    else:
        result = debate(signal, d, rounds=2)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

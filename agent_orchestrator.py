# ============================================================
# agent_orchestrator.py -- 统一 Agent 编排层
#
# 架构定位: 替代原有的隐式流水线 (Analyst->Decision->Risk->Execution),
#   升级为显式的自主协作 Agent 编排, 支持:
#     - Task Planning: 分解复杂任务为子任务
#     - Tool Selection: 自动发现和调用工具
#     - Multi-Agent Coordination: 协调多个 Agent 协作
#     - Result Synthesis: 综合各 Agent 输出
#
# 与现有架构的融合:
#   本模块不替代 run_daily.py, 而是作为其增强层:
#     - run_daily.py 仍负责确定性流程 (数据同步/因子计算/选股)
#     - agent_orchestrator 负责需要 LLM 推理的环节 (研判/验证/优化)
#     - 在 run_daily.py 的 feedback 步骤中调用本模块
#
# 执行模式:
#   1. 盘后分析模式 (post_market_analysis):
#      - 运行 AlphaLogics 因子发现
#      - 运行 FactorMAD 辩论验证
#      - 生成 LLM 点评
#      - 更新增量学习
#
#   2. 实时监控模式 (real_time_monitor):
#      - 运行 SPC 退化检测
#      - 触发快速辩论
#      - 生成告警
#
#   3. 周末深度模式 (weekend_deep):
#      - 全量 AlphaLogics 流水线
#      - 全量因子审查
#      - 知识库更新
#      - 策略优化建议
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Any

_LOG = logging.getLogger("agent_orchestrator")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DAILY_DIR  # noqa: E402

ORCHESTRATOR_DIR = os.path.join(DATA_DIR, "orchestrator")
TASK_LOG = os.path.join(ORCHESTRATOR_DIR, "task_log.json")


# ============================================================
# 任务定义
# ============================================================
class TaskPlan:
    def __init__(self, task_id: str, description: str, subtasks: list[dict]):
        self.task_id = task_id
        self.description = description
        self.subtasks = subtasks  # [{name, tool, params, depends_on}]


# ============================================================
# 模式 1: 盘后分析
# ============================================================
def post_market_analysis(day: str | None = None,
                         use_llm: bool = True) -> dict:
    """盘后综合分析: AlphaLogics 因子发现 + FactorMAD 辩论 + LLM 点评.

    替代原有的 llm_commentary + pre_drl_brief 串联调用,
    提供统一的证据收集、工具调度和结果综合.
    """
    t0 = time.perf_counter()
    day = day or date.today().strftime("%Y-%m-%d")
    results = {"day": day, "mode": "post_market_analysis"}

    # Step 1: 并行收集所有证据
    evidence = _parallel_collect_evidence(day)
    results["evidence"] = {"ok": evidence is not None}
    if not evidence:
        return {**results, "ok": False, "error": "证据收集失败",
                "elapsed_s": round(time.perf_counter() - t0, 2)}

    # Step 2: AlphaLogics 因子发现 (并行)
    if use_llm:
        try:
            from alpha_logics import run_alpha_logics_pipeline
            alpha_result = run_alpha_logics_pipeline(day, max_rationales=3)
            validation = alpha_result.get("validation", {})
            if not isinstance(validation, dict):
                validation = {}
            results["alpha_logics"] = {
                "ok": alpha_result.get("ok", False),
                "n_rationales": alpha_result.get("discovery", {}).get("n_rationales", 0),
                "n_passed": validation.get("n_passed", 0),
            }
        except Exception:
            _LOG.warning("AlphaLogics 失败: %s", traceback.format_exc())
            results["alpha_logics"] = {"ok": False,
                                       "error": str(traceback.format_exc()[-200:])}

    # Step 3: SPC 退化检测
    try:
        from degradation import run_full_check
        spc = run_full_check()
        results["spc"] = spc
    except Exception:
        results["spc"] = {"ok": False}
        _LOG.warning("SPC 失败: %s", traceback.format_exc())

    # Step 4: LLM 点评 (如果 LLM 可用)
    if use_llm:
        try:
            from llm_commentary import run_llm_commentary
            commentary = run_llm_commentary(day, day)
            results["commentary"] = {"ok": commentary is not None and "error" not in str(commentary)}
        except Exception:
            _LOG.warning("LLM 点评失败: %s", traceback.format_exc())
            results["commentary"] = {"ok": False}

    # Step 5: 综合输出
    results["ok"] = True
    results["elapsed_s"] = round(time.perf_counter() - t0, 2)
    _save_task_log("post_market_analysis", day, results)

    return results


def _parallel_collect_evidence(day: str) -> dict | None:
    from agent_tools import execute_parallel

    calls = [
        ("market_panel", {"day": day}),
        ("symbolic_ta", {"day": day}),
        ("spc_check", {"day": day}),
    ]
    try:
        results = execute_parallel(calls)
    except Exception:
        return None

    return {
        "market_panel": results[0],
        "symbolic_ta": results[1],
        "spc_check": results[2],
        "day": day,
    }


# ============================================================
# 模式 2: 实时监控
# ============================================================
def real_time_monitor(day: str | None = None) -> dict:
    """实时监控: SPC 退化检测 + 快速辩论 + 告警生成.

    用于盘中或盘后快速检查, 不调用 LLM 缩减延迟.
    """
    t0 = time.perf_counter()
    day = day or date.today().strftime("%Y-%m-%d")

    from agent_tools import execute_parallel

    calls = [
        ("spc_check", {"day": day}),
        ("symbolic_ta", {"day": day}),
        ("market_panel", {"day": day}),
    ]
    results = execute_parallel(calls)
    spc, sym_ta, panel = results[0], results[1], results[2]

    alerts = _generate_alerts(spc, sym_ta, panel)

    monitor_result = {
        "ok": True,
        "day": day,
        "mode": "real_time_monitor",
        "spc": spc,
        "symbolic_ta": {"market_state": sym_ta.get("market_state", "unknown"),
                        "confidence": sym_ta.get("confidence", 0)},
        "market_panel": {"sentiment": panel.get("sentiment_score", 50)
                         if panel.get("ok") else None},
        "alerts": alerts,
        "n_alerts": len(alerts),
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }

    _save_task_log("real_time_monitor", day, monitor_result)
    return monitor_result


def _generate_alerts(spc: dict, sym_ta: dict, panel: dict) -> list[dict]:
    alerts = []

    # SPC 退化告警
    if spc.get("ok"):
        level = spc.get("level", "")
        if level in ("P0", "P1"):
            alerts.append({
                "type": "spc_degradation",
                "severity": "critical" if level == "P0" else "warning",
                "message": f"策略退化检测: {level}",
                "detail": spc.get("detail", ""),
            })

    # 市场状态突变告警
    if sym_ta.get("ok"):
        ms = sym_ta.get("market_state", "")
        intensity = sym_ta.get("intensity", "")
        if ms == "bear" and intensity == "strong":
            alerts.append({
                "type": "market_regime",
                "severity": "warning",
                "message": f"市场进入强熊市状态 (intensity={intensity})",
            })

    # 情绪极端告警
    if panel.get("ok"):
        sentiment = panel.get("sentiment_score", 50)
        if sentiment < 25:
            alerts.append({
                "type": "extreme_sentiment",
                "severity": "warning",
                "message": f"市场情绪极度恐慌 (sentiment={sentiment:.0f})",
            })
        elif sentiment > 80:
            alerts.append({
                "type": "extreme_sentiment",
                "severity": "info",
                "message": f"市场情绪极度亢奋 (sentiment={sentiment:.0f})",
            })

    return alerts


# ============================================================
# 模式 3: 周末深度分析
# ============================================================
def weekend_deep_analysis(day: str | None = None) -> dict:
    """周末深度分析: 全量因子发现 + 审查 + 策略优化建议.

    耗时长 (~5-10 min), 只应在周末或节假日运行.
    """
    t0 = time.perf_counter()
    day = day or date.today().strftime("%Y-%m-%d")

    results = {"day": day, "mode": "weekend_deep_analysis"}

    # Step 1: 全量 AlphaLogics 流水线
    try:
        from alpha_logics import run_alpha_logics_pipeline, review_candidates
        alpha = run_alpha_logics_pipeline(day, max_rationales=8)
        results["alpha_logics"] = {
            "ok": alpha.get("ok", False),
            "n_discovered": alpha.get("discovery", {}).get("n_rationales", 0),
            "n_passed": alpha.get("validation", {}).get("n_passed", 0),
        }

        # Step 2: 因子审查
        review = review_candidates(day)
        results["factor_review"] = {
            "ok": review.get("ok", False),
            "n_reviewed": len(review.get("reviewed", [])),
            "summary": review.get("summary", ""),
        }
    except Exception:
        _LOG.warning("AlphaLogics 周末深度分析失败: %s",
                     traceback.format_exc())
        results["alpha_logics"] = {"ok": False}

    # Step 3: 全量 IC 回测
    try:
        from ic_curve_refresh import refresh
        refresh(full=True)
        results["ic_refresh"] = {"ok": True, "mode": "full"}
    except Exception:
        _LOG.warning("全量 IC 刷新失败: %s", traceback.format_exc())
        results["ic_refresh"] = {"ok": False}

    # Step 4: 策略优化建议
    try:
        from incremental_learn import run_incremental_learn
        opt = run_incremental_learn(day)
        results["optimization"] = {"ok": opt is not None,
                                   "triggered": opt.get("triggered", False) if opt else False}
    except Exception:
        results["optimization"] = {"ok": False}
        _LOG.warning("策略优化失败: %s", traceback.format_exc())

    # Step 5: LLM 综合报告
    try:
        report = _generate_weekly_report(day, results)
        results["weekly_report"] = report
    except Exception:
        results["weekly_report"] = {"ok": False}
        _LOG.warning("周报生成失败: %s", traceback.format_exc())

    results["ok"] = True
    results["elapsed_s"] = round(time.perf_counter() - t0, 2)
    _save_task_log("weekend_deep_analysis", day, results)

    return results


def _call_llm_orch(messages, max_tokens=2048):
    from llm_commentary import _load_dotenv as _ld, _build_messages_url as _bmu
    _ld()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "minimax-m3").strip()
    if not base or not api_key:
        return None
    from urllib import request
    url = _bmu(base)
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
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
        _LOG.warning("orchestrator: LLM call failed: %s", e)
        return None


def _generate_weekly_report(day: str, results: dict) -> dict:
    prompt = f"""## 生成周末策略分析报告

### 日期: {day}

### AlphaLogics 因子发现
{json.dumps(results.get('alpha_logics', {}), ensure_ascii=False, indent=2)[:2000]}

### 因子审查
{json.dumps(results.get('factor_review', {}), ensure_ascii=False, indent=2)[:2000]}

### 策略优化
{json.dumps(results.get('optimization', {}), ensure_ascii=False, indent=2)[:1000]}

请输出 JSON:
{{
  "weekly_summary": "string  // 本周策略运行摘要",
  "key_findings": ["string"],
  "factor_insights": ["string"],
  "optimization_recommendations": ["string"],
  "next_week_outlook": "string",
  "risk_alerts": ["string"]
}}"""

    messages = [{"role": "user", "content": prompt}]
    text = _call_llm_orch(messages, max_tokens=2048)
    if not text:
        return {"ok": False, "error": "LLM 调用失败"}

    from llm_commentary import _extract_first_json_object, _repair_json
    raw = _extract_first_json_object(text)
    if not raw:
        return {"ok": False, "error": "非 JSON 输出"}

    try:
        return {"ok": True, **json.loads(raw)}
    except json.JSONDecodeError:
        repaired = _repair_json(raw)
        if repaired:
            try:
                return {"ok": True, **json.loads(repaired)}
            except json.JSONDecodeError:
                pass
        return {"ok": False, "error": "JSON 解析失败"}


def _load_dotenv():
    from llm_commentary import _load_dotenv as _ld
    _ld()


# ============================================================
# 任务日志
# ============================================================
def _save_task_log(mode: str, day: str, results: dict):
    os.makedirs(ORCHESTRATOR_DIR, exist_ok=True)
    logs = []
    if os.path.exists(TASK_LOG):
        try:
            with open(TASK_LOG, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    entry = {
        "mode": mode,
        "day": day,
        "timestamp": datetime.now().isoformat(),
        "ok": results.get("ok", False),
        "elapsed_s": results.get("elapsed_s", 0),
    }
    logs.append(entry)
    if len(logs) > 200:
        logs = logs[-200:]
    with open(TASK_LOG, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)


# ============================================================
# 公共入口
# ============================================================
def run(mode: str = "post_market", day: str | None = None,
        use_llm: bool = True) -> dict:
    """统一 Agent 编排入口.

    Args:
        mode: "post_market" | "real_time" | "weekend"
        day: 日期 YYYY-MM-DD
        use_llm: 是否使用 LLM (False 时降级为纯规则)

    Returns:
        {"ok": bool, "mode": str, "day": str, ...}
    """
    if mode == "post_market":
        return post_market_analysis(day, use_llm=use_llm)
    elif mode == "real_time":
        return real_time_monitor(day)
    elif mode == "weekend":
        return weekend_deep_analysis(day)
    else:
        return {"ok": False, "error": f"未知模式: {mode}",
                "available": ["post_market", "real_time", "weekend"]}


# ============================================================
# 命令行调试
# ============================================================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Agent 编排器")
    ap.add_argument("--mode", default="real_time",
                    choices=["post_market", "real_time", "weekend"],
                    help="运行模式")
    ap.add_argument("--day", help="YYYY-MM-DD")
    ap.add_argument("--no-llm", action="store_true",
                    help="禁用 LLM 调用")
    args = ap.parse_args()

    d = args.day or date.today().strftime("%Y-%m-%d")
    result = run(mode=args.mode, day=d, use_llm=not args.no_llm)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

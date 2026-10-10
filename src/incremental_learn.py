# ============================================================
# incremental_learn.py -- 增量学习闭环
#
# 输入: degradation.run_full_check() 退化指数 + 增量样本
# 流程:
#   1) 读取增量样本 (P0/P1)
#   2) 触发 LLM (MiniMax-M3) 输出策略优化建议 (基于退化样本)
#   3) 写 ArcticDB factor_ic (供后续 DRL 微调作为先验)
#   4) 调整 DRL reward 权重 (严重退化 -> 增大 vnpy_real reward 占比,
#      以让 PPO 更依赖真实回测信号而非 IC 近似)
#   5) 输出 strategy_optimization_<day>.json 供 dashboard 与人工 review
#
# 关键设计:
#   - LLM 输出必须是结构化 JSON (action, weight_adjustments, sample_meta)
#   - reward 调整只是权重重映射, 不动 PPO 算法本身
#   - 失败一律容错, 不阻断 run_daily 主流程
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

import pandas as pd
from config import DATA_DIR
from reward_weights import DEFAULT_REWARD_WEIGHTS, normalize_reward_weights
from utils import atomic_write_json

_LOG = logging.getLogger("incremental_learn")

# 复用现有 LLM helper
from llm_commentary import (  # noqa: E402
    _load_dotenv, _build_messages_url, _env_timeout, _resolve_api_type)


# ============================================================================
# Prompt
# ============================================================================
_SYSTEM_PROMPT = """你是 A 股全 A 轮动模拟盘的策略退化诊断与优化助手.
任务: 基于提供的"近 N 日退化样本"(每条样本含指标 + 退化信号 + 优先级标签), 输出策略优化建议 JSON.
严禁虚构证据外的数字; 严禁臆测未给出的因子表现. 只输出 JSON, 不要 Markdown.

JSON 字段:
{
  "diagnosis_summary": "string  // 中文, 200字内, 概括当前策略状态",
  "root_causes": ["string", ...],    // 推断的根因, 3-5 条, 每条不超过 50 字
  "weight_adjustments": {
    "signal": float [0, 2],         // 对当前 6 因子权重的调整建议, 1.0=维持
    "trend": float [0, 2],
    "govern": float [0, 2],
    "liquidity": float [0, 2],
    "vol": float [0, 2],
    "mom_rev": float [0, 2]
  },
  "reward_rebalance": {
    "vnpy_weight": float [0, 1],    // 真实回测奖励权重
    "ic_weight": float [0, 1],      // IC 近似奖励权重
    "attr_weight": float [0, 1],    // 绩效归因奖励权重 (改造②), 可选
    "rationale": "string"            // 不超过 80 字
  },
  "action_recommendation": {
    "primary": "freeze | reweight | rebuild | hold",  // 主操作
    "secondary": ["string", ...],   // 辅助建议, 1-3 条
    "priority": "P0 | P1 | P2 | OK"  // 紧急度
  },
  "sample_evaluations": [
    {
      "day": "string",
      "agrees_with_label": bool,    // 是否同意样本本身的 label
      "comment": "string"           // 一句话点评
    }
  ],
  "confidence": float [0, 1]       // 整体置信度, 证据不足时 <=0.3
}
"""


def _build_user_payload(samples: list[dict], degradation: dict) -> str:
    payload = {
        "task": "策略退化诊断与增量学习优化建议",
        "degradation_overview": {
            "overall_score": degradation.get("overall_score"),
            "worst_level": degradation.get("worst_level"),
            "components": degradation.get("components"),
        },
        "incremental_samples": samples,
        "rules": {
            "no_external_knowledge": True,
            "max_chars_per_string": 200,
            "if_missing_evidence": "把 confidence 调到 <=0.3",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _extract_json(text: str) -> dict:
    """健壮地从 LLM 响应文本中提取完整 JSON 对象.

    策略:
      1) 先尝试贪心提取 {...} (兼容 LLM 只输出 JSON 或 Markdown 包裹).
      2) 失败 (LLM 输出被截断/多余文本) 时, 用 raw_decode 从第一个 '{' 开始
         逐个考察候选闭合点, 取能完整解码的最长前缀, 保证截断也能救回前段.
    """
    import json as _json
    import re as _re

    def _try(s):
        try:
            return _json.loads(s)
        except Exception:
            return None

    # 1) 常规: 提取首个 {...} 整体
    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    if m:
        obj = _try(m.group(0))
        if obj is not None:
            return obj

    # 2) 容错: 从第一个 '{' 扫描, 逐候选闭合位置取最长可解码前缀
    start = text.find("{")
    if start < 0:
        raise ValueError("响应中未找到 JSON 起始 '{'")
    decoder = _json.JSONDecoder()
    buf = text[start:]
    # 尝试不同深度的闭合截断: 从后往前截, 取第一个能完整解码的
    idxs = [i for i in range(1, len(buf)) if buf[i] == "}"][::-1]
    for i in idxs[:2000]:  # 最多尝试 2000 个闭合点
        frag = buf[: i + 1]
        # 平衡检查粗略: 只接收以 { 开头、以 } 结尾、括号数量匹配的候选
        try:
            obj, end = decoder.raw_decode(frag)
            if end == len(frag):  # 整段恰好是一个完整 JSON
                return obj
        except Exception:
            continue
    raise ValueError("无法从响应中恢复完整 JSON (截断严重)")


def _parse_response(body: dict) -> dict:
    try:
        text = "".join(
            b.get("text", "") for b in (body.get("content") or []) if isinstance(b, dict)
        )
    except Exception as e:
        raise RuntimeError(f"LLM 响应缺 content: {e}") from e

    try:
        parsed = _extract_json(text)
    except Exception as e:
        raise RuntimeError(f"LLM JSON 解析失败: {e}; text={text[:300]}") from e

    wa = parsed.get("weight_adjustments") or {}
    rr = parsed.get("reward_rebalance") or {}
    ar = parsed.get("action_recommendation") or {}
    try:
        reward = {"vnpy_weight": rr.get("vnpy_weight", 0.5),
                  "ic_weight": rr.get("ic_weight", 0.5)}
        if rr.get("attr_weight") is not None:
            reward["attr_weight"] = rr["attr_weight"]
        normalize_reward_weights({**reward, "attr_weight": reward.get("attr_weight", 0.15)})
        reward = {key: float(value) for key, value in reward.items()}
        reward["rationale"] = str(rr.get("rationale") or "").strip()
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"invalid reward_rebalance: {exc}") from exc
    return {
        "diagnosis_summary": str(parsed.get("diagnosis_summary") or "").strip(),
        "root_causes": [str(s) for s in (parsed.get("root_causes") or [])][:5],
        "weight_adjustments": {
            k: max(0.0, min(2.0, float(wa.get(k, 1.0)))) for k in
            ("signal", "trend", "govern", "liquidity", "vol", "mom_rev")
        },
        "reward_rebalance": reward,
        "action_recommendation": {
            "primary": str(ar.get("primary") or "hold").strip(),
            "secondary": [str(s) for s in (ar.get("secondary") or [])][:3],
            "priority": str(ar.get("priority") or "OK").strip(),
        },
        "sample_evaluations": [
            {
                "day": str(e.get("day") or ""),
                "agrees_with_label": bool(e.get("agrees_with_label", True)),
                "comment": str(e.get("comment") or "").strip()[:200],
            }
            for e in (parsed.get("sample_evaluations") or [])
        ],
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence") or 0))),
    }


def _post_anthropic(messages_url: str, api_key: str, model: str,
                    system_text: str, user_text: str,
                    max_tokens: int = 4096, timeout: float = 90.0) -> dict:
    """[2026-09-28] 改为委托 `llm_commentary._post_messages` —— 支持 ollama/openai/anthropic.

    此前本函数是 `llm_commentary._post_anthropic` 的**第 2 份复制**(仅 max_tokens
    与 User-Agent 不同)。复制件不会随主实现一起支持新协议 ⇒ 切到 Ollama 后本模块
    **静默失效**。协议细节现集中在 `llm_commentary` 一处。
    """
    from llm_commentary import _post_messages
    return _post_messages(messages_url, api_key, model, system_text, user_text,
                          max_tokens=max_tokens, timeout=timeout,
                          user_agent="A_stock_rotation/incremental_learn")


# ============================================================================
# 主入口
# ============================================================================
def run_incremental_learn(day: str, days: int = 10,
                           trigger_threshold: str = "P1") -> dict:
    """增量学习主流程.

    Args:
        day: 触发日 (YYYY-MM-DD)
        days: 取最近 N 天退化样本
        trigger_threshold: 触发 LLM 的退化最低级别 (P0/P1/P2).
            P1 表示只有 P0/P1 才调 LLM (避免每次盘后都烧 token).
    """
    from config import DATA_DIR
    from degradation import run_full_check
    from heartbeat import Heartbeat

    # 心跳: 证明增量学习闭环在推进 (区别于"未触发跳过"与"静默崩溃")
    hb = Heartbeat(os.path.join(DATA_DIR, "daily", day.replace("-", "")),
                   "incremental_learn", extra={"day": day, "trigger_threshold": trigger_threshold})
    hb.start(phase="running_degradation_check")

    rank_trigger = {"P0": 0, "P1": 1, "P2": 2}.get(trigger_threshold, 1)
    out = {"day": day, "triggered": False, "samples_n": 0,
           "optimization": None, "error": None}

    # 1) 退化检测
    try:
        check = run_full_check(days=days)
    except Exception as e:
        out["error"] = f"degradation.run_full_check: {e}"
        hb.stop(phase="degradation_check_failed", ok=False, error=out["error"])
        return out

    samples = check.get("incremental_samples", [])
    index = check.get("degradation_index", {})
    out["samples_n"] = len(samples)
    out["degradation_overall_score"] = index.get("overall_score")
    out["degradation_worst_level"] = index.get("worst_level")

    # 触发条件: 最差级别 (rank 越小越严重) <= trigger_threshold rank, 且样本数 >= 3
    worst_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "OK": 4}.get(
        index.get("worst_level", "OK"), 4
    )
    if worst_rank > rank_trigger or len(samples) < 3:
        out["triggered"] = False
        out["reason"] = (
            f"worst={index.get('worst_level')}(rank={worst_rank}) "
            f"未达到 trigger={trigger_threshold}(rank={rank_trigger}) 或样本<3 ({len(samples)})"
        )
        hb.stop(phase="not_triggered", ok=True,
                error="正常跳过(未达退化阈值)")
        return out

    # 2) 调 LLM
    hb.ping(phase="calling_llm")
    _load_dotenv()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "MiniMax-M3").strip()
    timeout = _env_timeout()
    # [2026-09-28] 本地端点(Ollama)无鉴权, 不强制 key(与 llm_commentary 同源判据)。
    if not base or (not key and _resolve_api_type(base) != "ollama"):
        out["error"] = ("缺少 OPENAI_BASE_URL" if not base
                        else "缺少 OPENAI_API_KEY")
        hb.stop(phase="config_error", ok=False, error=out["error"])
        return out
    url = _build_messages_url(base)

    user_text = _build_user_payload(samples, index)
    try:
        result = _post_anthropic(url, key, model, _SYSTEM_PROMPT, user_text,
                                 max_tokens=4096, timeout=timeout)
    except error.HTTPError as e:
        out["error"] = f"LLM HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
        hb.stop(phase="llm_http_error", ok=False, error=out["error"])
        return out
    except error.URLError as e:
        out["error"] = f"LLM 连接失败: {e.reason}"
        hb.stop(phase="llm_connect_error", ok=False, error=out["error"])
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        hb.stop(phase="llm_post_error", ok=False, error=out["error"])
        return out

    body = result["body"]
    usage = (body.get("usage") or {}) if isinstance(body, dict) else {}
    try:
        parsed = _parse_response(body)
    except RuntimeError as e:
        # JSON 解析失败: 重试一次(追加"必须完整闭合"约束, 降低再次截断概率)
        out["parse_error"] = str(e)[:200]
        hb.ping(phase="parse_retry")
        try:
            retry_user = user_text + (
                "\n\n[重试指示] 你上一次输出被截断/格式错误。请务必输出一个结构完整、"
                "括号闭合的单个 JSON 对象；不要省略任何外层花括号，不要加 Markdown。"
            )
            result2 = _post_anthropic(url, key, model, _SYSTEM_PROMPT, retry_user,
                                      max_tokens=4096, timeout=timeout)
            body = result2["body"]
            parsed = _parse_response(body)
            out["retried"] = True
        except Exception as e2:
            out["error"] = f"LLM 重试仍失败: {e2}"
            hb.stop(phase="llm_retry_failed", ok=False, error=out["error"])
            return out

    out["triggered"] = True
    out["optimization"] = parsed
    out["meta"] = {
        "model": model,
        "latency_seconds": round(result["latency"], 2),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 3) 落盘 strategy_optimization_<day>.json
    out_dir = os.path.join(DATA_DIR, "daily", day.replace("-", ""))
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "strategy_optimization.json")
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        out["write_error"] = str(e)

    # 4) ArcticDB 增量学习样本写入
    try:
        from arctic_store import get_store
        store = get_store()
        lib = store._lib("factor_ic")
        if lib is not None:
            for adj_factor, mult in parsed["weight_adjustments"].items():
                store.append_factor_ic(
                    f"adj_{adj_factor}", day,
                    ic=mult - 1.0,  # 偏离 1.0 的程度, 越偏离越说明 LLM 强调
                    n=len(samples),
                    recent_mean20=None,
                )
    except Exception as e:
        out["arcticdb_warn"] = str(e)

    hb.stop(phase="done", ok=True)
    return out


# ============================================================================
# DRL reward 动态调整 (供 drl_train 读取)
# ============================================================================
REWARD_CONFIG_FILE = os.path.join(DATA_DIR, "reward_config.json")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _generation_day() -> dt.date:
    return dt.datetime.now(_SHANGHAI).date()


def get_reward_weight_state(*, as_of: str | None = None) -> dict:
    """Read scheduled config, exposing every fallback as explicit evidence."""
    default = normalize_reward_weights(DEFAULT_REWARD_WEIGHTS)
    state = {"weights": default, "status": "default", "reason": "config_missing",
             "source": REWARD_CONFIG_FILE, "generated_on": None, "effective_from": None}
    state["active_provenance"] = {key: state[key] for key in
                                  ("status", "reason", "source", "generated_on", "effective_from")}
    try:
        day = dt.date.fromisoformat(as_of) if as_of else _generation_day()
        with open(REWARD_CONFIG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or d.get("schema_version") != 2:
            raise ValueError("reward_config schema_version must be 2")
        required = {"generated_on", "effective_from", "calendar_status", "calendar_identity",
                    "previous_weights", "previous_provenance", "source", "updated_at"}
        if required - d.keys():
            raise ValueError(f"reward_config missing fields: {sorted(required - d.keys())}")
        weights = normalize_reward_weights(d)
        previous = normalize_reward_weights(d.get("previous_weights"))
        previous_provenance = d["previous_provenance"]
        if (not isinstance(previous_provenance, dict)
                or previous_provenance.get("status") not in ("default", "degraded_default", "available")
                or not previous_provenance.get("source") or "reason" not in previous_provenance):
            raise ValueError("reward_config previous_provenance invalid")
        calendar_identity = d["calendar_identity"]
        if not isinstance(calendar_identity, dict) or not {"source", "version"} <= calendar_identity.keys():
            raise ValueError("reward_config calendar_identity invalid")
        generated = dt.date.fromisoformat(d["generated_on"])
        if day < generated:
            raise ValueError("future-generated reward_config cannot supply historical weights")
        effective_raw = d.get("effective_from")
        state.update({"generated_on": generated.isoformat(), "effective_from": effective_raw,
                      "calendar_identity": d.get("calendar_identity")})
        if d.get("calendar_status") == "pending_calendar_resolution" and effective_raw is None:
            return {**state, "weights": previous, "status": "pending_calendar_resolution",
                    "active_provenance": previous_provenance,
                    "reason": "next_authoritative_session_unknown"}
        if d.get("calendar_status") != "resolved":
            raise ValueError("reward_config calendar_status invalid")
        effective = dt.date.fromisoformat(effective_raw)
        if effective <= generated:
            raise ValueError("effective_from must follow generated_on")
        expected, _identity = _next_reward_session(generated)
        if not calendar_identity.get("source") or not calendar_identity.get("version") or expected != effective_raw:
            raise ValueError("effective_from is not supported by authoritative calendar evidence")
        if day < effective:
            return {**state, "weights": previous, "status": "pending_effective",
                    "active_provenance": previous_provenance,
                    "reason": "effective_session_not_reached"}
        active = {"status": "available", "reason": None, "source": REWARD_CONFIG_FILE,
                  "config_source": d["source"], "generated_on": generated.isoformat(),
                  "effective_from": effective_raw}
        return {**state, "weights": weights, "status": "available", "reason": None,
                "active_provenance": active}
    except FileNotFoundError:
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        return {**state, "weights": default, "status": "degraded_default",
                "reason": reason, "active_provenance": {
                    "status": "degraded_default", "reason": reason, "source": REWARD_CONFIG_FILE}}


def get_reward_weights(*, as_of: str | None = None) -> dict:
    state = get_reward_weight_state(as_of=as_of)
    if state["status"] == "degraded_default":
        _LOG.warning("reward config degraded: %s", state["reason"])
    return state["weights"]


def _next_reward_session(generated: dt.date) -> tuple[str | None, dict]:
    """Resolve only cached authoritative sessions; never fall back to weekdays."""
    import trading_calendar
    cache = trading_calendar._load_cache()
    source = str(cache.get("source") or "")
    identity = {"source": source or None, "version": cache.get("updated")}
    if not cache.get("ok") or not identity["version"]:
        return None, identity
    try:
        days = sorted(dt.datetime.strptime(str(day), "%Y%m%d").date() for day in cache["days"])
        official = ("akshare" in source.lower() or "sina" in source.lower()
                    or any(day > dt.datetime.now(_SHANGHAI).date() for day in days))
        if not official:
            return None, identity
        return next((day.isoformat() for day in days if day > generated), None), identity
    except (ValueError, TypeError):
        return None, identity


def set_reward_weights(vnpy_weight: float, ic_weight: float,
                       attr_weight: float | None = None,
                       source: str = "incremental_learn",
                       rationale: str = "", *, generated_on: str | None = None,
                       processing_day: str | None = None) -> bool:
    """写 DRL reward 权重. attr_weight 可选: None 时保留配置中现有值 (默认 0.15).
    归一化保证三权和=1; 生效日未知时保留原有效配置, 不猜测工作日."""
    try:
        generated = dt.date.fromisoformat(generated_on) if generated_on else _generation_day()
        if processing_day is not None:
            processing_day = dt.date.fromisoformat(processing_day).isoformat()
        previous_state = get_reward_weight_state(as_of=generated.isoformat())
        previous = previous_state["weights"]
        weights = normalize_reward_weights({
            "vnpy_weight": vnpy_weight, "ic_weight": ic_weight,
            "attr_weight": previous["attr_weight"] if attr_weight is None else attr_weight,
        })
        effective, calendar_identity = _next_reward_session(generated)
        payload = {**weights, "schema_version": 2, "source": source, "rationale": rationale,
                   "updated_at": dt.datetime.now(_SHANGHAI).isoformat(),
                   "generated_on": generated.isoformat(), "effective_from": effective,
                   "processing_day": processing_day,
                   "calendar_status": "resolved" if effective else "pending_calendar_resolution",
                   "calendar_identity": calendar_identity, "previous_weights": previous,
                   "previous_provenance": previous_state["active_provenance"]}
        atomic_write_json(REWARD_CONFIG_FILE, payload)
        return True
    except (OSError, ValueError, TypeError) as exc:
        _LOG.warning("reward config not written: %s", exc)
        return False


if __name__ == "__main__":
    import sys
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().strftime("%Y-%m-%d")
    r = run_incremental_learn(day, days=10, trigger_threshold="P1")
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))

# ============================================================
# pre_drl_brief.py -- DRL 微调前的市场研判摘要 + 情绪因子
#
# 读 evidence: market.json + performance_report.json + vnpy_backtest summary
# 调 MiniMax-M3 (Anthropic 兼容协议) -> 结构化市场研判:
#   - market_summary    当前市场结构摘要 (中文, 200字内)
#   - regime            trend / range / volatile / crash
#   - stance            加仓/维持/减仓/观望
#   - sentiment_factors 4 维 [-1, 1]: risk_on_off / rotation_intensity /
#                       liquidity_stress / policy_catalyst
#   - factor_recommendations 6 维 [0, 2]: 对 signal/trend/govern/liquidity/
#                       vol/mom_rev 的相对基础权重乘子
#   - confidence        0..1, evidence 不足时 <=0.3
#
# 落盘: data/drl/<YYYYMMDD>/pre_drl_brief.json
# DRL 端读取此产物: sentiment_factors 拼入 obs,
#                  factor_recommendations 调整先验权重, stance 调节探索幅度.
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request


_LOG = logging.getLogger("pre_drl_brief")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DUCKDB_PATH  # noqa: E402

# 复用 llm_commentary 的 .env / MiniMax 协议代码
from llm_commentary import _load_dotenv, _build_messages_url, _extract_first_json_object

SCORE_FACTORS = ["signal", "trend", "govern", "liquidity", "vol", "mom_rev"]

_SYSTEM_PROMPT = """你是 A 股全 A 轮动模拟盘的盘后宏观/情绪研判助手. 任务: 基于提供的市场证据(情绪分/宽度/涨跌停/成交额/IC 趋势/持仓归因/vnpy 主链路回测), 给出 DRL 因子权重微调前所需的结构化研判.

禁止虚构任何 evidence 中未提供的数字或事件. 所有结论必须直接基于 evidence 推理, 严禁外推或猜测.

只输出 JSON, 不要任何 Markdown / 解释 / 客套话. JSON 字段:

{
  "market_summary": "string  // 当前市场结构摘要, 中文, 200 字内, 直陈要点",
  "regime": "trend | range | volatile | crash  // 市场状态四选一",
  "stance": "加仓 | 维持 | 减仓 | 观望  // 仓位倾向四选一",
  "sentiment_factors": {
    "risk_on_off":      float [-1,1],  // 风险偏好: +1 极强风险偏好, -1 极强避险
    "rotation_intensity": float [-1,1], // 板块轮动强度: +1 剧烈轮动, -1 无轮动
    "liquidity_stress":   float [-1,1], // 流动性压力: +1 宽松, -1 紧张 (反转语义)
    "policy_catalyst":    float [-1,1]  // 政策催化预期: +1 强催化预期, -1 紧缩担忧
  },
  "factor_recommendations": {
    "signal":    float [0, 2],  // 对基础权重的乘子, 1.0 = 维持
    "trend":     float [0, 2],
    "govern":    float [0, 2],
    "liquidity": float [0, 2],
    "vol":       float [0, 2],
    "mom_rev":   float [0, 2]
  },
  "confidence": float [0, 1]  // 研判本身的可信度; evidence 不足时 <=0.3
}

要点:
- regime 选择应基于近 N 日趋势/波动特征, 而非单日.
- stance 应在 regime 之上叠加情绪分与基准超额决定, 不允许跳过 regime 直接给 stance.
- sentiment_factors 各分量的语义如注释所示 (liquidity_stress 反转).
- factor_recommendations 是 DRL 的先验, 不是硬性指令. 在不确定时保持 1.0.
- 证据明显不足 (如 sentiment_score 缺失 / attribution 为空) 时, 调低 confidence 并在
  market_summary 中说明"证据不足".
"""


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _evidence_payload(day: str,
                      market: dict | None,
                      perf: dict | None,
                      vnpy_summary: dict | None,
                      ic_trend: list[float] | None,
                      graph_evidence: dict | None = None,
                      kb_evidence: dict | None = None) -> dict:
    """组装给 LLM 的 evidence 块, 控制 token 数.
    每块带 availability 标记: True/False, 让 LLM 明确知道哪个证据可用.
    graph_evidence: 知识图谱(概念/行业)摘要, 由 graph_map.build_graph_evidence 产出.
    kb_evidence: GraphRAG 本地知识库(历史复盘/规则)检索上下文, 由 graphrag_bridge.build_kb_evidence 产出.
    """
    availability: dict[str, bool] = {}

    market_min: dict = {}
    if market and market.get("ok"):
        availability["market"] = True
        market_min = {
            "day": market.get("day"),
            "sentiment_score": market.get("sentiment_score"),
            "market_width": market.get("market_width") or {},
            "limit": market.get("limit") or {},
            "breadth": market.get("breadth") or {},
            "turnover": market.get("turnover") or {},
        }
    else:
        availability["market"] = False

    perf_min: dict = {}
    if perf and perf.get("ok"):
        m = perf.get("metrics") or {}
        b = perf.get("benchmark") or {}
        attribution = perf.get("attribution") or []
        if attribution:
            att_sorted = sorted(attribution, key=lambda x: -(x.get("unrealized_pnl") or 0))
            att_payload = {
                "top5_winners": [
                    {k: a.get(k) for k in ("canon", "weight", "unrealized_pnl")}
                    for a in att_sorted[:5]
                ],
                "bottom3_losers": [
                    {k: a.get(k) for k in ("canon", "weight", "unrealized_pnl")}
                    for a in att_sorted[-3:]
                ],
            }
        else:
            att_payload = {}
        availability["perf"] = True
        perf_min = {
            "period": perf.get("period") or {},
            "metrics": {k: m.get(k) for k in (
                "total_return", "annual_return", "max_drawdown", "sharpe_annual", "final_equity"
            )},
            "benchmark": {
                "bench_total_ret": b.get("bench_total_ret"),
                "excess_total": b.get("excess_total"),
            },
            "attribution": att_payload,
        }
    else:
        availability["perf"] = False

    vnpy_min: dict = {}
    if vnpy_summary and not vnpy_summary.get("fallback"):
        s = vnpy_summary.get("stats", {}) if "stats" in vnpy_summary else vnpy_summary
        availability["vnpy"] = True
        vnpy_min = {
            "engine": vnpy_summary.get("engine"),
            "total_return": s.get("total_return"),
            "sharpe_ratio": s.get("sharpe_ratio"),
            "max_ddpercent": s.get("max_ddpercent"),
            "trades": s.get("total_trade_count"),
        }
    else:
        availability["vnpy"] = False

    availability["ic_trend"] = bool(ic_trend and len(ic_trend) >= 5)
    availability["graph"] = bool(graph_evidence and graph_evidence.get("available"))
    availability["kb"] = bool(kb_evidence and kb_evidence.get("available"))

    return {
        "task": "DRL 微调前的市场研判 + 情绪因子",
        "day": day,
        "availability": availability,  # True/False 标记, LLM 据此判 confidence
        "available_market_evidence": {
            "market": market_min,
            "perf": perf_min,
            "vnpy_backtest": vnpy_min,
            "ic_trend_last_10d": (ic_trend or [])[-10:],
            "ic_trend_mean_60d": (
                round(sum(ic_trend) / len(ic_trend), 6) if ic_trend else None
            ),
            "graph": graph_evidence or {"available": False},
            "kb": {
                "available": bool(kb_evidence and kb_evidence.get("available")),
                "query": (kb_evidence or {}).get("query", ""),
                "kb_context": (kb_evidence or {}).get("kb_context", ""),
                "n_sources": (kb_evidence or {}).get("n_sources", 0),
                "source": (kb_evidence or {}).get("source", "GraphRAG 本地知识库"),
            } if kb_evidence else {"available": False},
        },
        "rules": {
            "no_external_knowledge": True,
            "if_missing_evidence": "返回字段空值或'证据不足', 并把 confidence 调到 <=0.3",
            "availability_field": "请先读取 availability 字段, 仅基于 availability=True 的证据做出判断",
            "graph_block": (
                "若 availability.graph=True: graph 块给出持仓(归因)的行业/概念分布"
                "与当日涨幅榜热概念. 用它辅助判断 rotation_intensity(板块轮动强度)"
                "与 regime, 并可顺带指出持仓板块集中度风险; 严禁引用 graph 之外的"
                "板块/概念信息. 若 graph 缺失, 直接忽略该块, 不要臆测板块信息."
            ),
            "kb_block": (
                "若 availability.kb=True: kb 块是 GraphRAG 本地知识库"
                "(历史复盘/投研笔记/策略规则/绩效回溯)检索命中的上下文片段. "
                "把它当作'历史经验': 当当前 market/ic_trend 等证据与历史模式相似时, "
                "可用于印证或修正 regime/stance 判断; 严禁把 kb_context 中的历史回测"
                "数字当作当日已发生的事实. 若 kb 缺失, 直接忽略该块."
            ),
        },
    }


def _parse_response(body: dict) -> dict:
    try:
        text = "".join(
            b.get("text", "") for b in (body.get("content") or []) if isinstance(b, dict)
        )
    except Exception as e:
        raise RuntimeError(f"LLM 响应缺少 content: {e}") from e

    raw = _extract_first_json_object(text)
    if not raw:
        raise RuntimeError(f"LLM 响应未包含 JSON: {text[:200]}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM JSON 解析失败: {e}; text={text[:300]}") from e

    sentiment = parsed.get("sentiment_factors") or {}
    factors = parsed.get("factor_recommendations") or {}

    out = {
        "market_summary": str(parsed.get("market_summary") or "证据不足").strip(),
        "regime": str(parsed.get("regime") or "range").strip(),
        "stance": str(parsed.get("stance") or "观望").strip(),
        "sentiment_factors": {
            "risk_on_off":       _clamp(sentiment.get("risk_on_off", 0), -1, 1),
            "rotation_intensity": _clamp(sentiment.get("rotation_intensity", 0), -1, 1),
            "liquidity_stress":   _clamp(sentiment.get("liquidity_stress", 0), -1, 1),
            "policy_catalyst":    _clamp(sentiment.get("policy_catalyst", 0), -1, 1),
        },
        "factor_recommendations": {
            k: _clamp(factors.get(k, 1.0), 0.0, 2.0) for k in SCORE_FACTORS
        },
        "confidence": _clamp(parsed.get("confidence", 0), 0.0, 1.0),
    }
    # 合法性收口
    if out["regime"] not in ("trend", "range", "volatile", "crash"):
        out["regime"] = "range"
    if out["stance"] not in ("加仓", "维持", "减仓", "观望"):
        out["stance"] = "观望"
    return out


def _post_anthropic(messages_url: str, api_key: str, model: str,
                    system_text: str, user_text: str,
                    max_tokens: int = 1000, timeout: float = 60.0) -> dict:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_text,
        "messages": [{"role": "user", "content": user_text}],
    }
    req = request.Request(
        messages_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 A_stock_rotation/pre_drl_brief",
        },
        method="POST",
    )
    t0 = time.time()
    with request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {"body": body, "latency": time.time() - t0}


def _latest_trade_day_from_db(day: str) -> str | None:
    """从 DuckDB 取 <= day 的最近一个有 daily_bars 数据的交易日.
    用于 fallback: 当 day_dir/<X>.json 不存在时, 回退到最近一日的 evidence.
    """
    try:
        import duckdb
        day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        row = con.execute(
            "SELECT MAX(date) FROM daily_bars WHERE date <= ?", [day_dt]
        ).fetchone()
        con.close()
        if row and row[0]:
            d = row[0]
            return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
    except Exception:
        return None
    return None


def _load_market(day: str) -> tuple[dict | None, str | None]:
    """读市场情绪. 优先用当日, 否则 fallback 到 DuckDB 最近一日.
    返回 (market_dict, effective_day). 任一缺失时 effective_day=None.
    """
    day_dir = day.replace("-", "")
    p = os.path.join(DATA_DIR, "market", day_dir, "market.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            if m.get("ok"):
                return m, day
        except Exception:
            pass
    # fallback: DuckDB 最近一日
    fallback_day = _latest_trade_day_from_db(day)
    if not fallback_day:
        return None, None
    fb_dir = fallback_day.replace("-", "")
    fb_path = os.path.join(DATA_DIR, "market", fb_dir, "market.json")
    if not os.path.exists(fb_path):
        return None, fallback_day
    try:
        with open(fb_path, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("ok"):
            return m, fallback_day
    except Exception:
        pass
    return None, fallback_day


def _load_perf() -> dict | None:
    """绩效报告: 单文件无日期, 直接读. 若不含当天, 仍可用(LLM 自己判 confidence)."""
    p = os.path.join(DATA_DIR, "performance_report.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_vnpy_summary(day: str) -> tuple[dict | None, str | None]:
    """读 vnpy 主链路回测. 优先当日, 否则 fallback 到 DuckDB 最近一日.
    返回 (summary_dict, effective_day).
    """
    day_dir = day.replace("-", "")
    p = os.path.join(DATA_DIR, "vnpy_backtest", day_dir, "summary.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
            if not s.get("fallback"):
                return s, day
        except Exception:
            pass
    fallback_day = _latest_trade_day_from_db(day)
    if not fallback_day:
        return None, None
    fb_dir = fallback_day.replace("-", "")
    fb_path = os.path.join(DATA_DIR, "vnpy_backtest", fb_dir, "summary.json")
    if not os.path.exists(fb_path):
        return None, fallback_day
    try:
        with open(fb_path, encoding="utf-8") as f:
            s = json.load(f)
        if not s.get("fallback"):
            return s, fallback_day
    except Exception:
        pass
    return None, fallback_day


def _load_ic_trend(day: str, lookback: int = 60) -> list[float]:
    """复用 drl_train 的 DuckDB 取 IC 序列, 取第一维(signal IC)做 trend."""
    try:
        import duckdb
        day_dt = dt.datetime.strptime(day, "%Y-%m-%d").date()
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        rows = con.execute(
            "SELECT DISTINCT date FROM daily_bars "
            "WHERE date<=? AND date>=? ORDER BY date",
            [day_dt, day_dt - dt.timedelta(days=lookback)],
        ).fetchall()
        dates = [r[0] for r in rows]
        if len(dates) < 5:
            con.close()
            return []
        import numpy as np
        rets = []
        for i, d in enumerate(dates):
            prev = dates[i - 1] if i > 0 else None
            if prev:
                r = con.execute(
                    "SELECT AVG(b.close/p.close - 1) FROM daily_bars b "
                    "JOIN daily_bars p ON b.symbol=p.symbol AND p.date=? "
                    "WHERE b.date=?", [prev, d]
                ).fetchone()
                rets.append(float(r[0]) if r and r[0] is not None else 0.0)
            else:
                rets.append(0.0)
        con.close()
        # signal IC 近似: 价量 trend 相关性
        arr = np.array(rets, dtype=np.float64)
        ics: list[float] = []
        for t in range(len(arr)):
            if t >= 5:
                win = arr[max(0, t - 19):t + 1]
                if len(win) > 1:
                    ics.append(float(np.corrcoef(win, np.arange(len(win)))[0, 1]))
                else:
                    ics.append(0.0)
            else:
                ics.append(0.0)
        return ics
    except Exception:
        return []


def generate_pre_drl_brief(day: str,
                           market: dict | None = None,
                           perf: dict | None = None,
                           vnpy_summary: dict | None = None,
                           ic_trend: list[float] | None = None,
                           graph_evidence: dict | None = None,
                           kb_evidence: dict | None = None) -> dict:
    """调 LLM 生成研判, 返回结构化 dict."""
    _load_dotenv()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "MiniMax-M3").strip()
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60") or 60)
    if not base or not key:
        return {"ok": False, "error": "缺少 OPENAI_BASE_URL 或 OPENAI_API_KEY", "stage": "config"}

    messages_url = _build_messages_url(base)
    payload = _evidence_payload(day, market, perf, vnpy_summary, ic_trend,
                                graph_evidence=graph_evidence,
                                kb_evidence=kb_evidence)
    user_text = json.dumps(payload, ensure_ascii=False)

    try:
        result = _post_anthropic(messages_url, key, model,
                                 _SYSTEM_PROMPT, user_text,
                                 max_tokens=1000, timeout=timeout)
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        return {"ok": False, "error": f"LLM HTTP {e.code}: {detail}", "stage": "http"}
    except error.URLError as e:
        return {"ok": False, "error": f"LLM 连接失败: {e.reason}", "stage": "connect"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "stage": "post"}

    body = result["body"]
    usage = (body.get("usage") or {}) if isinstance(body, dict) else {}
    try:
        parsed = _parse_response(body)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "stage": "parse"}

    return {
        "ok": True,
        "brief": parsed,
        "meta": {
            "model": model,
            "base_url": base,
            "latency_seconds": round(result["latency"], 2),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def run_pre_drl_brief(day: str, day_dir: str) -> dict:
    """供 run_daily.py 调用: 自动读 evidence + 调 LLM + 写盘.
    evidence 缺失时自动 fallback 到 DuckDB 最近一日, 在 brief meta 标注 effective_day.
    """
    # 心跳: 证明 pre_drl_brief 正在加载 evidence / 调 LLM (卡死时 last_seen 停更)
    from heartbeat import Heartbeat
    hb = Heartbeat(os.path.join(DATA_DIR, "drl", day_dir),
                   "pre_drl_brief", extra={"day": day_dir})
    hb.start(phase="loading_evidence")

    market, m_eff = _load_market(day)
    perf = _load_perf()
    vnpy, v_eff = _load_vnpy_summary(day)
    ic_trend = _load_ic_trend(day, 60)

    # 改造①: 知识图谱接入 LLM 上下文
    # 从绩效归因的持仓 canon 反查行业/概念分布 + 当日涨幅榜热概念,
    # 供 LLM 判断板块轮动强度与持仓集中度; 任何失败都静默降级为 None.
    graph_evidence = None
    try:
        from graph_map import build_graph_evidence
        attribution = (perf or {}).get("attribution") if perf else None
        graph_evidence = build_graph_evidence(day, attribution=attribution, with_hot=True)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("构建 graph_evidence 失败, 降级为 None: %s", e)
        graph_evidence = None

    # 改造②: GraphRAG 本地知识库接入 LLM 上下文
    # 从历史复盘/投研笔记检索"超跌/轮动/情绪冰点"等历史经验, 拼入 kb 块.
    # subprocess 隔离, 失败静默降级, 不阻断主流程.
    kb_evidence = None
    try:
        from graphrag_bridge import build_kb_evidence
        kb_query = (
            f"当日日期 {day}, 当前市场情绪与因子表现 (IC趋势 "
            f"{' '.join(str(round(x, 3)) for x in (ic_trend or [])[-5:]) if ic_trend else 'NA'}), "
            f"历史复盘中的相似市场环境(超跌反弹/板块轮动/情绪冰点)下"
            f"策略如何操作"
        )
        kb_evidence = build_kb_evidence(kb_query, top_k=8)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("构建 kb_evidence 失败, 降级为 None: %s", e)
        kb_evidence = None
    hb.ping(phase="calling_llm")

    result = generate_pre_drl_brief(day, market, perf, vnpy, ic_trend,
                                    graph_evidence=graph_evidence,
                                    kb_evidence=kb_evidence)

    # 把 effective_day 写到 meta 顶层, 便于审计 fallback 行为
    if result.get("ok"):
        meta = result.setdefault("meta", {})
        meta["effective_day"] = {
            "requested": day,
            "market": m_eff or day,
            "vnpy": v_eff or day,
            "any_fallback": (m_eff != day) or (v_eff != day),
        }

    out_dir = os.path.join(DATA_DIR, "drl", day_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "pre_drl_brief.json")
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result["output"] = out_file
        hb.stop(phase="done", ok=bool(result.get("ok")),
                error=None if result.get("ok") else result.get("error"))
    except Exception as e:
        _LOG.warning("写 pre_drl_brief.json 失败: %s", e)
        hb.stop(phase="write_failed", ok=False, error=f"{type(e).__name__}: {e}")

    return result


def load_pre_drl_brief(day_dir: str) -> dict | None:
    """供 drl_train.py 调用: 读取已落盘的 brief.
    返回结构化 brief dict (sentiment_factors / stance / factor_recommendations /
    market_summary / regime / confidence), 而不是 result 包装.
    """
    p = os.path.join(DATA_DIR, "drl", day_dir, "pre_drl_brief.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("ok"):
        return None
    return d.get("brief") or None


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().strftime("%Y-%m-%d")
    day_dir = day.replace("-", "")
    r = run_pre_drl_brief(day, day_dir)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
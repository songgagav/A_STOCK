# ============================================================
# llm_commentary.py -- MiniMax 盘后归因点评 (Anthropic 兼容协议)
# 读 performance_report.json + market.json, 调 MiniMax 生成结构化中文点评.
# 调用失败容错 (不阻断 run_daily 主流程), 失败时返回 error 信息供 dashboard 提示.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib import error, request


_LOG = logging.getLogger("llm_commentary")

# .env 路径推断: 与 research_trader/.env 共享. 支持环境变量覆盖.
_DEFAULT_ENV_PATHS = [
    os.environ.get("RESEARCH_TRADER_ENV", "").strip(),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "research_trader", ".env")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "research_trader", ".env")),
]


def _load_dotenv() -> None:
    """最小化 .env 加载, 优先用 os.environ 中已存在的值."""
    for p in _DEFAULT_ENV_PATHS:
        if p and os.path.exists(p) and os.path.isfile(p):
            try:
                for raw_line in Path(p).read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k and k not in os.environ:
                        os.environ[k] = v.strip().strip('"').strip("'")
                _LOG.debug("llm_commentary: 已加载 %s", p)
                return
            except Exception as e:
                _LOG.warning("llm_commentary: 加载 %s 失败: %s", p, e)


def _build_messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/anthropic"):
        return f"{base}/v1/messages"
    if base.endswith("/v1"):
        return f"{base[:-3]}/anthropic/v1/messages"
    return f"{base}/anthropic/v1/messages"


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _extract_first_json_object(text: str) -> str | None:
    t = _strip_think(text)
    m = re.search(r"\{.*\}", t, re.DOTALL)
    return m.group(0) if m else None


def _repair_json(raw: str) -> str | None:
    """对 LLM 输出的 JSON 做启发式修复, 返回修复后仍无法解析返回 None.

    常见 LLM 笔误: 数组元素间缺逗号 / 多余尾逗号 / 空字段 / 键未加引号等.
    只做安全、幂等的替换, 修复后必须能通过 json.loads 校验才返回.
    """
    if not raw:
        return None
    # 候选修复: 先放原始文本兜底, 再放各单规则修复, 再放组合修复
    cands = [raw]
    pats = [
        # 补缺逗号: `"a" "b"` -> `"a","b"`
        (re.compile(r'"\s+("|\{)'), r'",\1'),
        # 去尾逗号: `,}` / `,]`
        (re.compile(r",\s*([}\]])"), r"\1"),
        # 键未加引号: `{key: value}` -> `{"key": value}`
        (re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\s*:"), r'{"\1":'),
        # 数组/对象元素缺开引号: `"..",裸文本..` -> `"..","裸文本..`
        # 闭引号后逗号后紧跟非引号/空白/括号字符 -> 补回开引号
        # 误补会破坏结构, 但只有能通过 json.loads 的候选才会被采纳
        (re.compile(r'"(\s*,\s*)(?=[^"\s\[,}\]])'), r'"\1"'),
    ]
    for pat, rep in pats:
        cands.append(pat.sub(rep, raw))
    # 组合: 补缺引号/补开引号 与 其它修复叠加
    for i, (pat, rep) in enumerate(pats):
        for j, (pat2, rep2) in enumerate(pats):
            if i == j:
                continue
            c = pat2.sub(rep2, pat.sub(rep, raw))
            cands.append(c)
    # 所有候选最后再统一叠加一遍去尾逗号, 覆盖"修复后多出尾逗号"的情况
    trailing = pats[1][0], pats[1][1]
    cands = cands + [trailing[0].sub(trailing[1], c) for c in cands[1:]]
    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        try:
            json.loads(c)
            return c
        except (json.JSONDecodeError, TypeError):
            continue
    return None


_SYSTEM_PROMPT = """你是 A 股全 A 轮动模拟盘 (只做多 / T+1 / 单标的等权 / 滑点 0.0005 / 全额资金) 的盘后研究助手。
任务: 基于每日回执 + 绩效归因 + 市场情绪证据, 输出结构化 JSON 点评。
严禁虚构未在 evidence 中提供的数字或事件。所有数字必须直接引自 evidence, 严禁捏造。
只输出 JSON, 不要任何 Markdown / 解释 / 客套话。JSON 字段:

{
  "commentary": "string  // 中文一句话总结当日盘面, 不超过 80 字, 直陈要点",
  "performance_review": {
    "headline": "string  // 净值/基准一句话解读, 不超过 60 字",
    "drivers": ["string", ...],   // 关键驱动因素 (持仓个股/因子/事件), 每条不超过 30 字, 3-5 条
    "risks":    ["string", ...],  // 主要风险信号, 每条不超过 30 字, 2-4 条
    "score": 0                    // 综合评分 0-100 (基于收益/回撤/Sharpe/超额)
  },
  "next_session_brief": {
    "stance": "string  // 仓位倾向: 加仓/维持/减仓/观望 四选一",
    "watchlist": ["string", ...], // 后续关注要点, 每条不超过 25 字, 3-5 条
    "action_items": ["string", ...] // 建议操作事项, 1-3 条
  },
  "sentiment_narrative": "string  // 基于市场情绪数据的解读, 不超过 80 字",
  "confidence": 0.0               // 点评本身的可信度 [0,1]; 证据不足时 <=0.3
}

evidence 解读规则 (按优先级):
1. attribution 或 perf.metrics 缺失 -> 持仓/绩效字段返回空数组/0, 但其它字段照常输出, confidence >= 0.3
2. 若有 market_history (多日序列), sentiment_narrative 必须基于情绪分变化趋势 + 市场宽度/涨跌停结构给出解读, 禁止写"证据不足"
3. 若 evidence.market 缺失 sentiment_score, 但 market_history 提供多日 sentiment_score 序列, 用 history 中最新一日的 sentiment_score 作为情绪参考, 严禁写"证据不足"
4. 仅当 market_history 完全为空且 evidence.market 缺失 sentiment_score, 才允许 sentiment_narrative = "证据不足", 此时 confidence <= 0.3
5. 严禁虚构未在 evidence 中提供的数字或事件。所有数字必须直接引自 evidence。
"""


def _build_user_payload(day: str, perf: dict, market: dict | None,
                        market_history: list | None = None,
                        graph_evidence: dict | None = None,
                        kb_evidence: dict | None = None) -> str:
    """组装 user 消息: 任务 + 证据块 + 规则. market_history 优先 (用于趋势解读).
    graph_evidence 由 graph_map.build_graph_evidence 提供 (持仓行业/概念分布),
    缺失时 availability.graph=False, LLM 不得臆测板块信息.
    kb_evidence 由 graphrag_bridge.build_kb_evidence 提供 (GraphRAG 历史经验),
    缺失时 availability.kb=False."""
    metrics = perf.get("metrics") or {}
    bench = perf.get("benchmark") or {}
    attribution = perf.get("attribution") or []
    ic = perf.get("ic_summary") or {}
    period = perf.get("period") or {}

    # 持仓归因 -> 截断 top5 + bottom3 防止 prompt 过大
    if attribution:
        attr_sorted = sorted(attribution, key=lambda x: -(x.get("unrealized_pnl") or 0))
        att_payload = {
            "top5_winners": [
                {k: a.get(k) for k in ("canon", "weight", "unrealized_pnl", "unrealized_pct")}
                for a in attr_sorted[:5]
            ],
            "bottom3_losers": [
                {k: a.get(k) for k in ("canon", "weight", "unrealized_pnl", "unrealized_pct")}
                for a in attr_sorted[-3:]
            ],
            "count": len(attribution),
        }
    else:
        att_payload = {"count": 0}

    payload = {
        "task": "为 A 股全 A 轮动模拟盘生成盘后归因点评",
        "day": day,
        "availability": {
            "perf": bool(perf and perf.get("ok")),
            "market": bool(market and (not isinstance(market, dict) or market.get("ok") or market)),
            "attribution": bool(att_payload.get("count") if isinstance(att_payload, dict) else False),
            "ic_summary": bool(ic),
            "market_history": bool(market_history),
            "graph": bool(graph_evidence and graph_evidence.get("available")),
            "kb": bool(kb_evidence and kb_evidence.get("available")),
        },
        "evidence": {
            "period": period,
            "metrics": metrics,
            "benchmark_summary": {
                "bench_total_ret": bench.get("bench_total_ret"),
                "excess_total": bench.get("excess_total"),
            },
            "attribution": att_payload,
            "ic_summary": ic,
            "market": (market or {}) if isinstance(market, dict) else {},
            "market_history": market_history or [],
            "market_history_summary": _summarize_market_history(market_history or []),
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
            "max_chars_per_string": 120,
            "availability_field": "请先读取 availability 字段, 仅基于 availability=True 的证据做出判断",
            "graph_block": (
                "若 availability.graph=True: graph 块给出持仓(归因)的行业/概念分布. "
                "用它辅助判断 performance_review.drivers 中的板块因素与持仓集中度风险, "
                "以及 next_session_brief.watchlist 中的板块轮动方向; 严禁引用 graph 之外的"
                "板块/概念信息. 若 graph 缺失, 直接忽略该块, 不要臆测板块信息."
            ),
            "kb_block": (
                "若 availability.kb=True: kb 块是 GraphRAG 本地知识库"
                "(历史复盘/投研笔记/策略规则/绩效回溯)检索命中的上下文片段. "
                "把它当作'历史经验': 当当前驱动/风险与历史模式相似时, 可辅助丰富"
                "drivers / risks / watchlist 的措辞; 严禁把 kb_context 中的历史回测"
                "数字当作当日已发生的事实. 若 kb 缺失, 直接忽略该块."
            ),
            "sentiment_reading": (
                "sentiment_narrative: 优先基于 market_history 趋势解读情绪。"
                "若 market_history 至少 1 条, sentiment_narrative 必须输出 (禁止'证据不足'); "
                "参考 sentiment_score 走势、涨跌停家数、市场宽度给出 1 句 80 字内解读。"
            ),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _post_anthropic(messages_url: str, api_key: str, model: str,
                    system_text: str, user_text: str,
                    max_tokens: int = 1200, timeout: float = 90.0) -> dict:
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
            "User-Agent": "Mozilla/5.0 A_stock_rotation/llm_commentary",
        },
        method="POST",
    )
    t0 = time.time()
    with request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {"body": body, "latency": time.time() - t0}


def _parse_response(body: dict) -> dict:
    """解析 LLM 返回 -> 结构化 commentary dict. 失败抛 RuntimeError."""
    try:
        text = "".join(
            b.get("text", "")
            for b in (body.get("content") or [])
            if isinstance(b, dict)
        )
    except Exception as e:
        raise RuntimeError(f"LLM 响应缺少 content: {e}") from e

    raw = _extract_first_json_object(text)
    if not raw:
        raise RuntimeError(f"LLM 响应未包含 JSON: {text[:200]}")

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # 先尝试启发式修复 (补缺逗号/去尾逗号等), 成功则继续, 失败降级为占位点评
        fixed = _repair_json(raw)
        if fixed is not None:
            try:
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                parsed = None
        if parsed is None:
            _LOG.warning("LLM JSON 解析失败后修复未果, 降级为占位点评: %s", e)
            return _fallback_commentary()

    # 字段容错
    perf_r = parsed.get("performance_review") or {}
    nxt = parsed.get("next_session_brief") or {}
    out = {
        "commentary": str(parsed.get("commentary") or "").strip(),
        "performance_review": {
            "headline": str(perf_r.get("headline") or "证据不足").strip(),
            "drivers": [str(s) for s in (perf_r.get("drivers") or [])][:5],
            "risks": [str(s) for s in (perf_r.get("risks") or [])][:4],
            "score": float(perf_r.get("score") or 0),
        },
        "next_session_brief": {
            "stance": str(nxt.get("stance") or "观望").strip(),
            "watchlist": [str(s) for s in (nxt.get("watchlist") or [])][:5],
            "action_items": [str(s) for s in (nxt.get("action_items") or [])][:3],
        },
        "sentiment_narrative": str(parsed.get("sentiment_narrative") or "证据不足").strip(),
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence") or 0))),
    }
    return out


def _fallback_commentary() -> dict:
    """JSON 解析彻底失败时的占位点评, 保证盘后点评不因 LLM 输出异常而整体中断."""
    return {
        "commentary": "今日 LLM 点评解析异常, 已降级为占位文本。",
        "performance_review": {
            "headline": "证据不足",
            "drivers": [],
            "risks": [],
            "score": 0,
        },
        "next_session_brief": {
            "stance": "观望",
            "watchlist": [],
            "action_items": [],
        },
        "sentiment_narrative": "证据不足",
        "confidence": 0.0,
        "fallback": True,
    }


def generate_commentary(day: str,
                        perf_report: dict,
                        market_report: dict | None = None,
                        perf_path: str | None = None,
                        market_path: str | None = None,
                        graph_evidence: dict | None = None,
                        kb_evidence: dict | None = None) -> dict:
    """主入口: 拉取 evidence, 调 LLM, 返回结构化 commentary.
    返回: {"ok": True, "commentary": {...}, "meta": {...}} 或 {"ok": False, "error": "..."}.
    """
    _load_dotenv()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("OPENAI_MODEL", "MiniMax-M3").strip()
    timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90") or 90)

    if not base or not key:
        return {"ok": False, "error": "缺少 OPENAI_BASE_URL 或 OPENAI_API_KEY", "stage": "config"}

    messages_url = _build_messages_url(base)
    # 拉近 30 日 market 序列 (有就用, 没有传空 list 让 LLM 看到 availability=False)
    market_history = _load_market_history(day, n_days=30)
    user_text = _build_user_payload(day, perf_report, market_report,
                                    market_history=market_history,
                                    graph_evidence=graph_evidence,
                                    kb_evidence=kb_evidence)

    try:
        result = _post_anthropic(messages_url, key, model,
                                 _SYSTEM_PROMPT, user_text,
                                 max_tokens=1200, timeout=timeout)
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
        return {"ok": False, "error": str(e), "stage": "parse",
                "raw": _strip_think("".join(
                    b.get("text", "") for b in (body.get("content") or [])
                    if isinstance(b, dict)
                ))[:500]}

    return {
        "ok": True,
        "commentary": parsed,
        "meta": {
            "model": model,
            "base_url": base,
            "latency_seconds": round(result["latency"], 2),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "perf_path": perf_path,
            "market_path": market_path,
            "market_history_n": len(market_history),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def run_llm_commentary(day: str, day_dir: str) -> dict:
    """供 run_daily.py 调用: 读 perf/market -> 调 LLM -> 写盘.

    market 缺失时自动 fallback 到 DuckDB 最近一日 (复用 pre_drl_brief._load_market).
    返回值直接作为 report["steps"]["llm_commentary"] 的内容.
    异常一律容错, 不抛.
    """
    try:
        from config import DATA_DIR
        from heartbeat import Heartbeat

        hb = Heartbeat(os.path.join(DATA_DIR, "daily", day_dir),
                       "llm_commentary", extra={"day": day_dir})
        hb.start(phase="read_perf")

        perf_path = os.path.join(DATA_DIR, "performance_report.json")

        perf = {}
        if os.path.exists(perf_path):
            try:
                with open(perf_path, encoding="utf-8") as f:
                    perf = json.load(f)
                hb.ping(phase="perf_loaded")
            except Exception as e:
                hb.stop(phase="read_perf_failed", ok=False, error=f"{type(e).__name__}: {e}")
                return {"ok": False, "error": f"读 performance_report.json 失败: {e}", "stage": "read_perf"}

        # 复用 pre_drl_brief 的 fallback 逻辑: 当日缺 -> DuckDB 最近一日
        try:
            from pre_drl_brief import _load_market as _load_market_with_fb
            market, m_eff = _load_market_with_fb(day)
        except Exception:
            market, m_eff = None, None
        market_path = os.path.join(DATA_DIR, "market", (m_eff or day).replace("-", ""), "market.json")

        if not perf:
            hb.stop(phase="no_perf", ok=False,
                    error="performance_report.json 缺失或为空, 跳过 LLM 点评")
            return {"ok": False, "error": "performance_report.json 缺失或为空, 跳过 LLM 点评", "stage": "no_perf"}

        # 改造①: 知识图谱接入盘后点评
        # 从当日归因持仓反查行业/概念分布 (盘后点评不取涨幅榜热度, 聚焦持仓结构),
        # 任何失败都静默降级为 None.
        graph_evidence = None
        try:
            from graph_map import build_graph_evidence
            attribution = perf.get("attribution") or []
            graph_evidence = build_graph_evidence(day, attribution=attribution,
                                                  with_hot=False)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("构建 graph_evidence 失败, 降级为 None: %s", e)
            graph_evidence = None

        # 改造②: GraphRAG 本地知识库接入盘后点评
        # 检索历史复盘/投研笔记中的相似市场环境经验, 拼入 kb 块.
        # subprocess 隔离, 失败静默降级, 不阻断主流程.
        kb_evidence = None
        try:
            from graphrag_bridge import build_kb_evidence
            kb_query = (
                f"当日日期 {day}, 持仓驱动与市场情绪证据, "
                f"历史复盘/投研笔记中相似市场环境(超跌反弹/板块轮动/情绪冰点)下"
                f"组合表现与后续关注要点"
            )
            kb_evidence = build_kb_evidence(kb_query, top_k=8)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("构建 kb_evidence 失败, 降级为 None: %s", e)
            kb_evidence = None

        hb.ping(phase="calling_llm")
        result = generate_commentary(day, perf, market,
                                     perf_path=perf_path, market_path=market_path,
                                     graph_evidence=graph_evidence,
                                     kb_evidence=kb_evidence)
        # 写盘: data/daily/<YYYYMMDD>/llm_commentary.json + 把 commentary 写回 perf
        if result.get("ok"):
            out_dir = os.path.join(DATA_DIR, "daily", day_dir)
            os.makedirs(out_dir, exist_ok=True)
            out_file = os.path.join(out_dir, "llm_commentary.json")
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            # 同时回写 performance_report.json (前端可直接读)
            try:
                perf["llm_commentary"] = result.get("commentary")
                perf["llm_commentary_meta"] = result.get("meta")
                with open(perf_path, "w", encoding="utf-8") as f:
                    json.dump(perf, f, ensure_ascii=False, indent=2, default=str)
            except Exception as e:
                _LOG.warning("回写 perf 失败: %s", e)
            result["output"] = out_file
            hb.stop(phase="done", ok=True)
        else:
            hb.stop(phase="llm_failed", ok=False, error=result.get("error"))
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            hb.stop(phase="error", ok=False, error=f"{type(e).__name__}: {e}")
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "stage": "exception"}


def _load_market_history(day: str, n_days: int = 30) -> list:
    """读 data/market/<YYYYMMDD>/market.json 近 n_days 个交易日 (倒序).
    返回 [{day, sentiment_score, market_width, limit, breadth, turnover, sector}, ...]
    数据点缺失时该 slot 跳过, 不阻塞.
    """
    out = []
    try:
        from config import DATA_DIR
    except Exception:
        return out
    mdir = os.path.join(DATA_DIR, "market")
    if not os.path.isdir(mdir):
        return out
    days_avail = sorted([d for d in os.listdir(mdir) if os.path.isdir(os.path.join(mdir, d))], reverse=True)
    for d in days_avail[:n_days]:
        p = os.path.join(mdir, d, "market.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
            if not m.get("ok"):
                continue
            # 转日期 key 为 YYYY-MM-DD
            day_iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            out.append({
                "day": day_iso,
                "sentiment_score": m.get("sentiment_score"),
                "market_width": m.get("market_width"),
                "limit": m.get("limit"),
                "breadth": m.get("breadth"),
                "turnover": m.get("turnover"),
                "sector": m.get("sector"),
            })
        except Exception:
            continue
    return out


def _summarize_market_history(history: list) -> dict:
    """对多日序列计算极简统计: 区间、最高/最低/均值/最近一日。供 LLM 趋势判断."""
    if not history:
        return {"n": 0, "recent": [], "extrema": {}}
    scores = [h["sentiment_score"] for h in history if h.get("sentiment_score") is not None]
    recent = history[:5]  # 最近5日
    extrema = {}
    if scores:
        extrema = {
            "max_score": max(scores),
            "min_score": min(scores),
            "avg_score": round(sum(scores) / len(scores), 2),
            "n_with_score": len(scores),
        }
    return {
        "n": len(history),
        "date_range": [history[-1]["day"], history[0]["day"]] if history else [],
        "extrema": extrema,
        "recent": recent,
    }


if __name__ == "__main__":
    import sys
    day = sys.argv[1] if len(sys.argv) > 1 else None
    day = day or time.strftime("%Y-%m-%d")
    day_dir = day.replace("-", "")
    r = run_llm_commentary(day, day_dir)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
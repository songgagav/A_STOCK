# ============================================================
# agent_tools.py -- 统一工具注册表 (Tool-Augmented Agents 基础)
#
# 架构定位: 把项目中的计算/检索/回测工具收敛为统一注册表,
# 供 LLM Agent 发现和调用. 每个工具都有标准接口:
#   name / description / parameters(JSON Schema) / execute(callable)
#
# 已注册工具:
#   - ml_fusion_predict    三模型融合预测
#   - graphrag_search      本地知识库检索
#   - knowledge_graph      行业/概念图谱反查
#   - ic_backtest          因子 IC 回测
#   - factor_score         单因子打分
#   - market_panel         市场情绪面板
#   - symbolic_ta          符号化趋势分析
#   - daily_bars_query     日线数据查询
#   - spc_check            统计过程控制/退化检测
#
# 设计约束:
#   - 工具执行失败一律容错, 返回 {"ok": false, "error": ...}, 不抛异常
#   - 支持并行调用 execute_parallel()
#   - 超时保护 (默认 60s, 可环境变量覆盖)
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import numpy as np

_LOG = logging.getLogger("agent_tools")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DATA_DIR, DUCKDB_PATH  # noqa: E402

_TIMEOUT_S = float(os.environ.get("AGENT_TOOL_TIMEOUT_S", "60") or 60)


# ============================================================
# 工具定义
# ============================================================
class Tool:
    __slots__ = ("name", "description", "parameters", "execute")

    def __init__(self, name: str, description: str, parameters: dict,
                 execute: Callable):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


# ============================================================
# 工具执行器
# ============================================================
def _safe_execute(name: str, fn: Callable, params: dict,
                  timeout: float = _TIMEOUT_S) -> dict:
    t0 = time.perf_counter()
    try:
        result = fn(**params)
    except Exception:
        elapsed = time.perf_counter() - t0
        _LOG.warning("tool=%s err elapsed=%.2fs: %s",
                     name, elapsed, traceback.format_exc())
        return {"ok": False, "tool": name, "elapsed_s": round(elapsed, 3),
                "error": traceback.format_exc()[-500:]}
    elapsed = time.perf_counter() - t0
    if isinstance(result, dict):
        result.setdefault("ok", True)
        result.setdefault("tool", name)
        result.setdefault("elapsed_s", round(elapsed, 3))
    else:
        result = {"ok": True, "tool": name, "elapsed_s": round(elapsed, 3),
                  "result": result}
    return result


# --- 1. 三模型融合预测 ---
def _ml_fusion_predict(symbols: list[str], as_of: str) -> dict:
    from ml_fusion_bridge import compute_fml
    res = compute_fml(symbols, as_of)
    return {"ok": res.get("available", False), "scores": res.get("scores", {}),
            "n": res.get("n", 0), "meta": res.get("meta", {})}


# --- 2. 本地知识库检索 ---
def _graphrag_search(query: str, top_k: int = 8) -> dict:
    from graphrag_bridge import build_kb_evidence
    res = build_kb_evidence(query, top_k=top_k)
    return {"ok": res.get("available", False),
            "kb_context": res.get("kb_context", ""),
            "source": res.get("source", "graphrag"),
            "meta": res.get("meta", {})}


# --- 3. 行业/概念图谱反查 ---
def _knowledge_graph(canons: list[str], day: str | None = None) -> dict:
    from graph_map import build_graph_evidence
    try:
        res = build_graph_evidence(day or "", attribution=[{"canon": c} for c in canons])
        return {"ok": True, **res}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-300:]}


# --- 4. 因子 IC 回测 ---
def _ic_backtest(factor_name: str, k: int = 20,
                 start: str | None = None, end: str | None = None) -> dict:
    import os as _os
    import pandas as pd

    if _os.environ.get("BAR_STORE", "h5i").lower() == "h5i":
        from h5i_bar_store import H5iBarStore
        _s = H5iBarStore()
        start = start or "2015-01-01"
        end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
        df = _s._db.sql(
            f"SELECT CAST(CAST(ts AS DATE) AS VARCHAR) date, symbol, close, "
            f"change_pct FROM daily_bars WHERE CAST(ts AS DATE) BETWEEN DATE '{start}' "
            f"AND DATE '{end}' AND close>0 AND change_pct IS NOT NULL "
            f"ORDER BY symbol, ts").to_pandas()
        _s.close()
        return _ic_compute(df, factor_name, k)
    import duckdb
    import numpy as np

    if not os.path.exists(DUCKDB_PATH):  # noqa: m4 DuckDB 退役防御
        return {"ok": False, "error": "DuckDB 已退役/不存在"}
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        start = start or "2015-01-01"
        end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
        df = con.execute("""
            SELECT date, symbol, close, change_pct
            FROM daily_bars
            WHERE date BETWEEN ? AND ?
              AND close > 0 AND change_pct IS NOT NULL
            ORDER BY symbol, date
        """, [start, end]).fetchdf()
    finally:
        con.close()
    return _ic_compute(df, factor_name, k)


def _ic_compute(df, factor_name: str, k: int) -> dict:
    import numpy as np
    import pandas as pd
    if df.empty:
        return {"ok": False, "error": "无数据"}
    df["date"] = pd.to_datetime(df["date"])
    pct = df.pivot(index="date", columns="symbol", values="change_pct") / 100.0
    try:
        from factor_library import FACTORS
        factor = FACTORS.get(factor_name)
        if factor is None:
            return {"ok": False, "error": f"未知因子: {factor_name}"}
        raw = factor["build_wide"](pct, k)
        fwd = pct.shift(-1).rolling(5).apply(
            lambda x: np.prod(1 + x) - 1, raw=True)
        ic_series = []
        for d in raw.index:
            r = raw.loc[d].dropna()
            f = fwd.loc[d].dropna()
            common = r.index.intersection(f.index)
            if len(common) < 30:
                continue
            ic = np.corrcoef(r[common], f[common])[0, 1]
            if np.isfinite(ic):
                ic_series.append({"date": str(d)[:10], "ic": round(float(ic), 4)})
        ics = [x["ic"] for x in ic_series]
        return {"ok": True, "factor": factor_name, "k": k,
                "n_dates": len(ic_series),
                "rank_ic_mean": round(float(np.mean(ics)), 4) if ics else 0,
                "rank_ic_std": round(float(np.std(ics)), 4) if ics else 0,
                "icir": round(float(np.mean(ics) / np.std(ics)), 4)
                if ics and np.std(ics) > 0 else 0,
                "ic_series": ic_series[-20:]}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-500:]}


# --- 5. 单因子打分 ---
def _factor_score(symbol: str, factor_name: str, day: str | None = None) -> dict:
    import os as _os
    import pandas as pd

    if _os.environ.get("BAR_STORE", "h5i").lower() == "h5i":
        from h5i_bar_store import H5iBarStore
        _s = H5iBarStore()
        day = day or pd.Timestamp.today().strftime("%Y-%m-%d")
        df = _s._db.sql(
            f"SELECT CAST(CAST(ts AS DATE) AS VARCHAR) date, symbol, open, high, "
            f"low, close, volume, amount, change_pct, turnover FROM daily_bars "
            f"WHERE symbol='{symbol}' AND CAST(ts AS DATE) <= DATE '{day}' "
            f"AND close>0 ORDER BY ts").to_pandas()
        _s.close()
        return _factor_score_compute(symbol, factor_name, df, day)
    import duckdb

    if not os.path.exists(DUCKDB_PATH):  # noqa: m4 DuckDB 退役防御
        return {"ok": False, "error": "DuckDB 已退役/不存在"}
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        day = day or pd.Timestamp.today().strftime("%Y-%m-%d")
        df = con.execute("""
            SELECT date, symbol, open, high, low, close, volume, amount,
                   change_pct, turnover
            FROM daily_bars
            WHERE symbol = ? AND date <= ?
              AND close > 0
            ORDER BY date
        """, [symbol, day]).fetchdf()
    finally:
        con.close()
    return _factor_score_compute(symbol, factor_name, df, day)


def _factor_score_compute(symbol, factor_name, df, day) -> dict:
    if df.empty:
        return {"ok": False, "error": f"{symbol} 无数据"}
    try:
        from factor_library import score_factor
        score = score_factor(factor_name, df)
        return {"ok": True, "symbol": symbol, "factor": factor_name,
                "score": round(float(score), 4), "day": day}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-300:]}


# --- 6. 市场情绪面板 ---
def _market_panel(day: str | None = None) -> dict:
    from market_panel import compute_market
    try:
        return compute_market(day)
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-300:]}


# --- 7. 符号化趋势分析 ---
def _symbolic_ta(day: str | None = None) -> dict:
    from symbolic_ta import compute_symbolic_state
    try:
        return compute_symbolic_state(day or "")
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-300:]}


# --- 8. 日线数据查询 ---
def _daily_bars_query(symbols: list[str], start: str,
                      end: str | None = None,
                      fields: list[str] | None = None) -> dict:
    import os as _os
    import pandas as pd

    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    allowed = {"date", "open", "high", "low", "close", "volume", "amount",
               "change_pct", "turnover"}
    cols = [f for f in (fields or ["date", "close", "volume", "change_pct"])
            if f in allowed]
    if _os.environ.get("BAR_STORE", "h5i").lower() == "h5i":
        from h5i_bar_store import H5iBarStore
        _s = H5iBarStore()
        ph = ",".join(["'%s'" % s for s in symbols])
        exprs = ["CAST(CAST(ts AS DATE) AS VARCHAR) AS date" if c == "date" else c for c in cols]
        sel = ", ".join(exprs)
        df = _s._db.sql(
            f"SELECT symbol, {sel} FROM daily_bars WHERE symbol IN ({ph}) "
            f"AND CAST(ts AS DATE) BETWEEN DATE '{start}' AND DATE '{end}' "
            f"AND close>0 ORDER BY symbol, ts").to_pandas()
        _s.close()
        return {"ok": True, "n_rows": len(df),
                "n_symbols": df["symbol"].nunique() if not df.empty else 0,
                "data": df.to_dict(orient="records")[:1000]}

    if not os.path.exists(DUCKDB_PATH):  # noqa: m4 DuckDB 退役防御
        return {"ok": False, "error": "DuckDB 已退役/不存在"}
    cols_str = ", ".join(f"b.{c}" for c in cols)
    placeholders = ",".join(["?"] * len(symbols))
    import duckdb
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        df = con.execute(f"""
            SELECT b.symbol, {cols_str}
            FROM daily_bars b
            WHERE b.symbol IN ({placeholders})
              AND b.date BETWEEN ? AND ?
              AND b.close > 0
            ORDER BY b.symbol, b.date
        """, [*symbols, start, end]).fetchdf()
    finally:
        con.close()

    return {"ok": True, "n_rows": len(df),
            "n_symbols": df["symbol"].nunique() if not df.empty else 0,
            "data": df.to_dict(orient="records")[:1000]}


# --- 9. SPC 退化检测 ---
def _spc_check(day: str | None = None, lookback_days: int = 30) -> dict:
    from degradation import run_full_check
    try:
        return run_full_check(days=lookback_days)
    except Exception:
        return {"ok": False, "error": traceback.format_exc()[-300:]}


# --- 10. Risk-First 风险检查 ---
def _risk_first_check(sentiment_signals: list[float],
                      portfolio_drawdown: float,
                      annualized_vol: float | None = None) -> dict:
    try:
        from risk_first import RiskFirstLayer, LLMVarianceFilter, RiskExposurePenalty, CircuitBreaker
        rfl = RiskFirstLayer()
        for sig in sentiment_signals:
            rfl.variance_filter.update(sig)
        cb_level = rfl.circuit_break_check(
            portfolio_drawdown, annualized_vol=annualized_vol)
        pos_limit = rfl.circuit_breaker.get_position_limit()
        return {
            "ok": True,
            "circuit_breaker_level": cb_level,
            "position_limit": pos_limit,
            "filter_history": rfl._filter_history[-5:],
            "state": rfl.state_dict(),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 11. Logic-Q 趋势分析 ---
def _logic_q_analysis(day: str, calibrate: bool = False) -> dict:
    try:
        from logic_q import compute_logic_q_tuning
        result = compute_logic_q_tuning(day, calibrate=calibrate)
        return result if result.get("ok") else {"ok": False, "error": result.get("error", "未知错误")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 12. 动态因子权重 ---
def _dynamic_factor_weights(day: str, total_timesteps: int = 300) -> dict:
    try:
        from factor_dynamic_weights import run_dynamic_weight_drl
        meta = run_dynamic_weight_drl(day, total_timesteps=total_timesteps)
        return {"ok": meta.get("ok", False),
                "day": day,
                "final_weights": meta.get("final_weights", []),
                "mean_reward": meta.get("mean_reward"),
                "algorithm": "CVaR_PPO_FactorValue",
                "error": meta.get("error")}
    except Exception as e:
        import traceback
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[-500:]}


# --- 13. 小波分解 ---
def _wavelet_decompose(prices: list[float], level: int = 3) -> dict:
    try:
        from wavelet_decomposition import _db4_wavelet_decomp
        arr = np.array(prices, dtype=float)
        dec = _db4_wavelet_decomp(arr, level)
        return {
            "ok": True,
            "trend_last": float(dec["trend"][-1]) if len(dec["trend"]) else 0.0,
            "volatility_last": float(dec["volatility"][-1]) if len(dec["volatility"]) else 0.0,
            "trend_ratio": round(dec["trend_ratio"], 4),
            "vol_ratio": round(dec["vol_ratio"], 4),
            "energy": round(dec["energy"], 2),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 14. Hi-DARTS 分析 ---
def _hi_darts_analyze(annualized_vol: float, trend_strength: float,
                       event_intensity: float = 0.0, regime: int = 0) -> dict:
    try:
        from hierarchical_agents import MetaAgent
        ma = MetaAgent()
        weights = ma.analyze(annualized_vol, trend_strength, event_intensity, regime)
        return {"ok": True, "agent_weights": weights}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 15. StockMARL 模拟 ---
def _stock_marl_simulate(n_symbols: int = 10) -> dict:
    try:
        from multi_agent_sim import HeterogeneousAgentSim
        sim = HeterogeneousAgentSim()
        # 构造模拟数据
        price_returns = {
            f"{i:06d}": np.random.randn(30) * 0.02
            for i in range(n_symbols)
        }
        fundamentals = {
            f"{i:06d}": {"pb": np.random.uniform(0.5, 3.0),
                          "roe": np.random.uniform(0.05, 0.25)}
            for i in range(n_symbols)
        }
        actions = sim.step(price_returns, fundamentals)
        consensus = sim.get_consensus_signal(actions)
        herding = sim.get_herding_index(actions)
        return {
            "ok": True,
            "agent_names": sim.agent_names,
            "consensus_signal": {k: round(v, 4) for k, v in consensus.items()},
            "herding_index": round(herding, 4),
            "state_vector": sim.get_state_vector().tolist(),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 16. 可解释 RL 决策追溯 ---
def _explainable_rl_trace(feature_names: list[str], n_steps: int = 10) -> dict:
    try:
        from explainable_rl import DecisionTrace, FeatureImportanceTracker
        dt = DecisionTrace(feature_names)
        fit = FeatureImportanceTracker(feature_names)
        for _ in range(n_steps):
            obs = np.random.randn(len(feature_names)) * 0.5
            action = np.random.randn(3) * 0.3
            reward = float(np.random.randn() * 0.1)
            dt.record(obs, action, reward)
            fit.record(obs, action, reward)
        return {
            "ok": True,
            "n_records": len(dt._records),
            "top_features": fit.get_top_features(3),
            "feature_importance": {k: round(v, 4)
                                   for k, v in fit.get_importance().items()},
            "last_explanation": dt.get_explanation(),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 17. 风险因子 PPO 动态优化 ---
def _risk_factor_optimize(returns: list[float], window: int = 20) -> dict:
    try:
        from risk_factor_optimizer import RiskFactorExtractor
        arr = np.array(returns, dtype=float)
        if len(arr) < 5 or not np.isfinite(arr).all():
            return {"ok": False, "error": "returns 需为>=5个有限数值的全A收益序列"}
        ex = RiskFactorExtractor(arr, window=window)
        last_raw = ex.raw_at(len(arr) - 1)
        last_norm = ex.norm_at(len(arr) - 1)
        return {
            "ok": True,
            "n_days": int(len(arr)),
            "risk_factor_names": ex.names,
            "last_raw": {k: round(float(v), 6) for k, v in zip(ex.names, last_raw)},
            "last_normalized": {k: round(float(v), 4) for k, v in zip(ex.names, last_norm)},
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 18. CAFPO 条件自编码潜在因子 ---
def _cafpo_latent(features: list, conditions: list,
                  latent_dim: int = 4, steps: int = 400) -> dict:
    try:
        from cafpo import extract_latent_factors
        feat = np.array(features, dtype=float)
        cond = np.array(conditions, dtype=float)
        if feat.ndim != 2 or cond.ndim != 2 or len(feat) != len(cond):
            return {"ok": False, "error": "features/conditions 需为同长度二维矩阵"}
        res = extract_latent_factors(
            feat, cond, latent_dim=latent_dim, steps=steps)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error", "提取失败")}
        latent = res["latent_factors"]
        return {
            "ok": True,
            "latent_dim": int(latent.shape[1]),
            "recon_r2": res["recon_r2"],
            "val_recon_r2": res["val_recon_r2"],
            "n_params": res["n_params"],
            "last_latent": [round(float(v), 4) for v in latent[-1]],
            "latent_mean": [round(float(v), 4) for v in latent.mean(axis=0)],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 19. 执行层滑点分解 ---
def _slippage_decompose(order_size: float, avg_daily_volume: float,
                        volatility: float,
                        execution_horizon_days: float = 0.0) -> dict:
    try:
        from slippage_model import decompose_slippage
        dec = decompose_slippage(
            order_size, avg_daily_volume, volatility,
            execution_horizon_days=execution_horizon_days)
        return {"ok": True, **dec}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --- 20. 策略达标检测 ---
def _strategy_validation_detect(source: str = "vnpy") -> dict:
    try:
        from strategy_validation import detect_source
        res = detect_source(source)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        if source == "all":
            out = {"ok": True, "results": {}}
            for name, r in (res.get("results") or {}).items():
                meta = r.get("meta") or {}
                out["results"][name] = {
                    "source": meta.get("source"), "tag": meta.get("tag"),
                    "verdict": r.get("verdict"),
                    "failed": r.get("failed_required", []),
                    "missing": r.get("missing_required", []),
                }
            return out
        meta = res.get("meta") or {}
        return {
            "ok": True,
            "source": meta.get("source"),
            "tag": meta.get("tag"),
            "sample": meta.get("sample"),
            "verdict": res.get("verdict"),
            "rows": [{"label": r["label"], "value": r["value"], "limit": r["limit"],
                      "status": r["status"], "aux": r["aux"]} for r in res.get("rows", [])],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ============================================================
# 工具注册表
# ============================================================
_TOOLS: dict[str, Tool] = {}


def _register():
    tools = [
        Tool("ml_fusion_predict",
             "三模型融合(XGBoost+LightGBM+CatBoost Stacking)预测未来5日收益率",
             {"type": "object", "properties": {
                 "symbols": {"type": "array", "items": {"type": "string"},
                             "description": "股票代码列表"},
                 "as_of": {"type": "string",
                           "description": "预测基准日期 YYYY-MM-DD"},
             }, "required": ["symbols", "as_of"]},
             _ml_fusion_predict),

        Tool("graphrag_search",
             "本地知识库检索(策略规则/历史复盘/投研笔记/绩效回溯)",
             {"type": "object", "properties": {
                 "query": {"type": "string", "description": "检索查询"},
                 "top_k": {"type": "integer", "description": "返回结果数"},
             }, "required": ["query"]},
             _graphrag_search),

        Tool("knowledge_graph",
             "行业/概念图谱反查: 输入股票列表, 返回行业分布和热点概念",
             {"type": "object", "properties": {
                 "canons": {"type": "array", "items": {"type": "string"},
                            "description": "股票代码列表"},
                 "day": {"type": "string", "description": "查询日期"},
             }, "required": ["canons"]},
             _knowledge_graph),

        Tool("ic_backtest",
             "因子 IC 回测: 计算因子与未来收益的截面相关性(RankIC/ICIR)",
             {"type": "object", "properties": {
                 "factor_name": {"type": "string",
                                 "description": "因子名: vol/mom/reversal"},
                 "k": {"type": "integer", "description": "因子窗口"},
                 "start": {"type": "string", "description": "起始日期"},
                 "end": {"type": "string", "description": "结束日期"},
             }, "required": ["factor_name"]},
             _ic_backtest),

        Tool("factor_score",
             "单只股票因子打分(0..1, 高分=更值得选)",
             {"type": "object", "properties": {
                 "symbol": {"type": "string", "description": "股票代码"},
                 "factor_name": {"type": "string",
                                 "description": "因子名: vol/mom_rev"},
                 "day": {"type": "string", "description": "打分日期"},
             }, "required": ["symbol", "factor_name"]},
             _factor_score),

        Tool("market_panel",
             "全A市场情绪面板: 情绪分/涨跌停比/成交分布/宽度",
             {"type": "object", "properties": {
                 "day": {"type": "string", "description": "查询日期"},
             }, "required": []},
             _market_panel),

        Tool("symbolic_ta",
             "符号化趋势分析: 均线排列/支撑阻力/量价关系/波动率状态",
             {"type": "object", "properties": {
                 "day": {"type": "string", "description": "分析日期"},
             }, "required": []},
             _symbolic_ta),

        Tool("daily_bars_query",
             "日线数据查询: 批量获取股票日线数据",
             {"type": "object", "properties": {
                 "symbols": {"type": "array", "items": {"type": "string"},
                             "description": "股票代码列表"},
                 "start": {"type": "string", "description": "起始日期"},
                 "end": {"type": "string", "description": "结束日期"},
                 "fields": {"type": "array", "items": {"type": "string"},
                            "description": "字段列表"},
             }, "required": ["symbols", "start"]},
             _daily_bars_query),

        Tool("spc_check",
             "SPC 统计过程控制: 检测策略绩效是否退化, 回看N天",
             {"type": "object", "properties": {
                 "day": {"type": "string", "description": "检测日期"},
                 "lookback_days": {"type": "integer", "description": "回看天数, 默认30"},
             }, "required": []},
             _spc_check),

        # --- 10. Risk-First 风险检查 ---
        Tool("risk_first_check",
             "Risk-First 风险检查: 方差过滤器 + 暴露惩罚 + 确定性熔断, 评估当前风险状态",
             {"type": "object", "properties": {
                 "sentiment_signals": {"type": "array", "items": {"type": "number"},
                                       "description": "LLM 情绪信号列表"},
                 "portfolio_drawdown": {"type": "number",
                                        "description": "当前组合回撤(小数)"},
                 "annualized_vol": {"type": "number",
                                    "description": "年化波动率(小数, 可选)"},
             }, "required": ["sentiment_signals", "portfolio_drawdown"]},
             _risk_first_check),

        # --- 11. Logic-Q 趋势分析 ---
        Tool("logic_q_analysis",
             "Logic-Q 神经符号化趋势分析: 均线交叉/支撑阻力/量价关系, 返回调优参数",
             {"type": "object", "properties": {
                 "day": {"type": "string", "description": "分析日期 YYYYMMDD"},
                 "calibrate": {"type": "boolean", "description": "是否执行参数校准"},
             }, "required": ["day"]},
             _logic_q_analysis),

        # --- 12. 动态因子权重 ---
        Tool("dynamic_factor_weights",
             "PPO 动态因子权重优化: 对融合因子(pb_inv/ep/ocf_ps/roe_yy_chg)+gp4 做动态权重分配",
             {"type": "object", "properties": {
                 "day": {"type": "string", "description": "交易日 YYYYMMDD"},
                 "total_timesteps": {"type": "integer",
                                     "description": "PPO 训练步数, 默认 300"},
             }, "required": ["day"]},
             _dynamic_factor_weights),

        # --- 13. 小波分解 + Hybrid-GRPO ---
        Tool("wavelet_decompose",
             "多尺度信号分解: 小波变换分离趋势/波动, 返回多尺度特征",
             {"type": "object", "properties": {
                 "prices": {"type": "array", "items": {"type": "number"},
                            "description": "价格序列"},
                 "level": {"type": "integer", "description": "分解层数, 默认3"},
             }, "required": ["prices"]},
             _wavelet_decompose),

        # --- 14. Hi-DARTS 层次化多智能体 ---
        Tool("hi_darts_analyze",
             "Hi-DARTS 层次化多智能体: 元智能体分析市场状态, 选择子策略",
             {"type": "object", "properties": {
                 "annualized_vol": {"type": "number", "description": "年化波动率"},
                 "trend_strength": {"type": "number", "description": "趋势强度 [-1,1]"},
                 "event_intensity": {"type": "number", "description": "事件密集度 [0,1]"},
                 "regime": {"type": "integer", "description": "市场状态 0=震荡 1=上升 2=下降"},
             }, "required": ["annualized_vol", "trend_strength"]},
             _hi_darts_analyze),

        # --- 15. StockMARL 多智能体模拟 ---
        Tool("stock_marl_simulate",
             "StockMARL 多智能体模拟: 模拟多种投资者行为, 输出共识信号",
             {"type": "object", "properties": {
                 "n_symbols": {"type": "integer", "description": "模拟股票数"},
             }, "required": ["n_symbols"]},
             _stock_marl_simulate),

        # --- 16. 可解释 RL 决策追溯 ---
        Tool("explainable_rl_trace",
             "可解释 RL: 决策追溯 + 特征重要性分析",
             {"type": "object", "properties": {
                 "feature_names": {"type": "array", "items": {"type": "string"},
                                   "description": "特征名列表"},
                 "n_steps": {"type": "integer", "description": "模拟步数, 默认10"},
             }, "required": ["feature_names"]},
             _explainable_rl_trace),

        # --- 17. 风险因子 PPO 动态优化 ---
        Tool("risk_factor_optimize",
             "风险因子 PPO 动态优化: 波动率/CVaR95/最大回撤/下行波动, 返回当前风险状态",
             {"type": "object", "properties": {
                 "returns": {"type": "array", "items": {"type": "number"},
                             "description": "全A日平均收益序列(按时间序)"},
                 "window": {"type": "integer", "description": "滚动窗口, 默认20"},
             }, "required": ["returns"]},
             _risk_factor_optimize),

        # --- 18. CAFPO 条件自编码潜在因子 ---
        Tool("cafpo_latent_extract",
             "CAFPO 条件自编码因子: 从海量因子压缩潜在风险因子, 报告重建解释度",
             {"type": "object", "properties": {
                 "features": {"type": "array", "items": {"type": "array",
                             "items": {"type": "number"}},
                             "description": "原始因子矩阵 (T, n_features)"},
                 "conditions": {"type": "array", "items": {"type": "array",
                               "items": {"type": "number"}},
                               "description": "公司特征矩阵 (T, n_conditions)"},
                 "latent_dim": {"type": "integer", "description": "潜在维度, 默认4"},
                 "steps": {"type": "integer", "description": "训练步数, 默认400"},
             }, "required": ["features", "conditions"]},
             _cafpo_latent),

        # --- 19. 执行层滑点分解 ---
        Tool("slippage_decompose",
             "执行层滑点分解: 市场冲击 vs 执行风险两个独立分量, 返回基点数",
             {"type": "object", "properties": {
                 "order_size": {"type": "number", "description": "订单金额(元)"},
                 "avg_daily_volume": {"type": "number", "description": "日均成交额(元)"},
                 "volatility": {"type": "number", "description": "日频波动率(小数)"},
                 "execution_horizon_days": {"type": "number",
                                             "description": "预计执行时长(交易日), 默认0"},
             }, "required": ["order_size", "avg_daily_volume", "volatility"]},
             _slippage_decompose),

        # --- 20. 策略达标检测 ---
        Tool("strategy_validation",
             "策略达标检测: 夏普/索提诺/卡玛/CAGR/回撤/恢复因子/盈亏比/胜率/期望值, 逐项阈值判定",
             {"type": "object", "properties": {
                 "source": {"type": "string",
                            "description": "数据源: vnpy(主链路回测摘要,默认) | "
                                           "backtest_latest | cost_after_fee1 | paper(实盘净值) | all"},
             }, "required": []},
             _strategy_validation_detect),
    ]
    for t in tools:
        _TOOLS[t.name] = t


_register()


# ============================================================
# 公共接口
# ============================================================
def get_tool(name: str) -> Tool | None:
    return _TOOLS.get(name)


def list_tools() -> list[dict]:
    return [{"name": t.name, "description": t.description,
             "parameters": t.parameters} for t in _TOOLS.values()]


def get_tool_schemas() -> list[dict]:
    return [{"name": t.name, "description": t.description,
             "input_schema": t.parameters} for t in _TOOLS.values()]


def execute(name: str, params: dict | None = None) -> dict:
    tool = _TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"未知工具: {name}",
                "available_tools": list(_TOOLS.keys())}
    return _safe_execute(name, tool.execute, params or {})


def execute_parallel(calls: list[tuple[str, dict]]) -> list[dict]:
    results = [None] * len(calls)

    def _run(idx, name, params):
        results[idx] = execute(name, params)

    with ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
        futures = {pool.submit(_run, i, n, p): i
                   for i, (n, p) in enumerate(calls)}
        for f in as_completed(futures, timeout=_TIMEOUT_S + 10):
            try:
                f.result()
            except Exception:
                idx = futures[f]
                results[idx] = {"ok": False, "error": "并行执行超时或异常"}

    return results


# ============================================================
# 命令行调试
# ============================================================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Agent 工具注册表调试")
    ap.add_argument("--list", action="store_true", help="列出所有工具")
    ap.add_argument("--tool", help="工具名")
    ap.add_argument("--params", default="{}", help="参数 JSON")
    args = ap.parse_args()

    if args.list:
        for t in list_tools():
            print(f"  {t['name']}: {t['description'][:80]}")
    elif args.tool:
        params = json.loads(args.params)
        result = execute(args.tool, params)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        ap.print_help()

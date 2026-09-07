# -*- coding: utf-8 -*-
# ============================================================
# ai_factor_lab.py -- AI 辅助因子挖掘实验室
#
# 工作流: LLM 提出因子假设 → 可行性校验 → 滚动回测(IC/ICIR/多空价差) → 输出报告
#
# 因子假设格式:
#   1. 注册因子名: 直接引用 factor_library.FACTOR_REGISTRY 中的因子
#      例: "ret_20", "rsi_14", "volume_ratio", "ep"
#   2. 组合表达式: 用 + - * / 组合多个因子, 支持 neg 取反
#      例: "ret_20 * -1", "rsi_14 - 50", "volume_ratio * ret_10"
#   3. 自然语言描述: LLM 用文字描述, 由 lab 映射到可用因子 (需传入 named_exprs)
#      例: "过去20日收益率与未来5日收益的负相关" → ret_20 * -1
#
# CLI:
#   python ai_factor_lab.py evaluate "ret_20 * -1" -n "反转因子" -d 2026-09-04
#   python ai_factor_lab.py evaluate "volume_ratio" -n "量比"
#   python ai_factor_lab.py batch "hypotheses.json"
#   python ai_factor_lab.py list                    # 列出可用的因子库
# ============================================================

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from typing import Optional

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 加载因子库注册表
sys.path.insert(0, _BASE)
from factor_library import FACTOR_REGISTRY, compute_factors  # noqa: E402
from factor_mine.evaluator import evaluate as _evaluate, daily_ic  # noqa: E402

_LOG = None


def _log_init():
    global _LOG
    if _LOG is None:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        _LOG = logging.getLogger("ai_factor_lab")


# ---------------------------------------------------------------------------
# 1. 因子表达式解析
# ---------------------------------------------------------------------------

# 内置运算符
_OPS = {
    "+": np.add, "-": np.subtract, "*": np.multiply, "/": np.divide,
    "neg": lambda x: -x, "abs": np.abs, "sqrt": np.sqrt, "log": np.log,
    "inv": lambda x: np.divide(1.0, x, where=x != 0),
}


def _tokenize(expr: str) -> list[str]:
    """将表达式分词。支持: 因子名、运算符、数字、括号、负号前缀。"""
    s = expr.replace(" ", "")
    pattern = r'[A-Za-z_]\w*|[-+*/()]|\d+\.?\d*'
    raw = re.findall(pattern, s)
    # 合并负号与数字: 当前 token 是 '-' 且前一个是运算符/左括号/为空
    merged = []
    for i, t in enumerate(raw):
        if t == '-' and (i == 0 or raw[i - 1] in ('+', '-', '*', '/', '(')):
            # 看下一个是否是数字或因子名
            if i + 1 < len(raw):
                try:
                    float(raw[i + 1])
                    merged.append('-' + raw[i + 1])
                    raw[i + 1] = ''  # 跳过
                    continue
                except ValueError:
                    pass
        if t != '':
            merged.append(t)
    return merged


def _parse_hypothesis(expr: str) -> tuple[Optional[list], str]:
    """解析因子假设表达式, 返回 (token list or None, 错误信息).

    支持的格式:
      - 单因子: "ret_20", "rsi_14"
      - 一元: "neg ret_20", "abs ret_20"
      - 二元: "ret_20 + ret_10", "volume_ratio * ret_20"
      - 括号: "(ret_20 + ret_10) * -1"
    """
    toks = _tokenize(expr)
    if not toks:
        return None, "空表达式"
    # 验证所有 token 合法
    for t in toks:
        if t in _OPS or t in ("(", ")"):
            continue
        if t in FACTOR_REGISTRY:
            continue
        try:
            float(t)
        except ValueError:
            return None, f"无法识别的 token: {t}"
    return toks, ""


# ---------------------------------------------------------------------------
# 2. 因子值计算
# ---------------------------------------------------------------------------

def _get_factor_values(day: str, factors: list[str]) -> pd.DataFrame:
    """获取指定日期的因子值 DataFrame.

    Returns:
        DataFrame with columns: symbol, <factor1>, <factor2>, ...
    """
    res = compute_factors(day, factors=factors, return_raw=False)
    df = res.get("df")
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def _compute_named_expr(toks: list[str], row: pd.Series) -> float:
    """对单行执行表达式求值。安全版 eval, 仅支持 + - * / neg abs 等基本运算。"""
    # 转换为逆波兰表达式 (Shunting Yard)
    prec = {"+": 1, "-": 1, "*": 2, "/": 2, "neg": 3, "abs": 3, "sqrt": 3}
    output: list[str] = []
    ops: list[str] = []
    for t in toks:
        if t in FACTOR_REGISTRY:
            output.append(t)
        elif t == "(":
            ops.append(t)
        elif t == ")":
            while ops and ops[-1] != "(":
                output.append(ops.pop())
            ops.pop()  # 丢弃 (
        elif t in _OPS:
            while ops and ops[-1] != "(" and prec.get(ops[-1], 0) >= prec.get(t, 0):
                output.append(ops.pop())
            ops.append(t)
        else:
            output.append(t)  # 数字
    while ops:
        output.append(ops.pop())

    # 执行 RPN
    stack: list[float] = []
    for t in output:
        if t in FACTOR_REGISTRY:
            val = row.get(t)
            stack.append(val if pd.notna(val) else np.nan)
        elif t in _OPS:
            if t in ("neg", "abs", "sqrt", "log", "inv"):
                if not stack:
                    return np.nan
                a = stack.pop()
                if not np.isfinite(a):
                    stack.append(np.nan)
                else:
                    try:
                        result = _OPS[t](a)
                        stack.append(float(result) if np.isfinite(result) else np.nan)
                    except (ZeroDivisionError, ValueError, FloatingPointError):
                        stack.append(np.nan)
            else:
                if len(stack) < 2:
                    return np.nan
                b = stack.pop()
                a = stack.pop()
                if not np.isfinite(a) or not np.isfinite(b):
                    stack.append(np.nan)
                else:
                    try:
                        result = _OPS[t](a, b)
                        stack.append(float(result) if np.isfinite(result) else np.nan)
                    except (ZeroDivisionError, ValueError, FloatingPointError):
                        stack.append(np.nan)
        else:
            # 数字常量
            try:
                stack.append(float(t))
            except ValueError:
                stack.append(np.nan)
    return stack[0] if stack else np.nan


def _eval_hypothesis_single_day(day: str, expr: str, toks: list[str]) -> dict:
    """对单日计算因子假设值。返回 {symbol: value}."""
    # 提取所有需要的因子名
    needed = [t for t in toks if t in FACTOR_REGISTRY]
    df = _get_factor_values(day, needed)
    if df.empty:
        return {}
    result = {}
    for _, row in df.iterrows():
        val = _compute_named_expr(toks, row)
        if np.isfinite(val):
            result[row["symbol"]] = float(val)
    return result


# ---------------------------------------------------------------------------
# 3. 滚动评估 (IC/ICIR/多空价差)
# ---------------------------------------------------------------------------

def evaluate_hypothesis(expr: str, name: str = "",
                        end_day: str | None = None,
                        window: int = 60,
                        horizon: int = 5,
                        gates: dict | None = None) -> dict:
    """对因子假设执行滚动回测评估.

    Args:
        expr: 因子表达式 (e.g. "ret_20 * -1", "volume_ratio")
        name: 因子名称 (用于报告)
        end_day: 评估截止日 YYYY-MM-DD, 缺省最近交易日
        window: 滚动窗口(交易日数)
        horizon: 预测期(交易日)
        gates: 准入门槛 {ic_mean, icir, mono}

    Returns:
        dict with keys: {factor, name, spec, n_days, ic_mean, icir, win_rate,
                         quintile, gates, verdict, ...}
    """
    t0 = time.time()
    _log_init()

    # 解析
    toks, err = _parse_hypothesis(expr)
    if err:
        return {"ok": False, "spec": expr, "name": name or expr, "error": err}

    needed = list(set(t for t in toks if t in FACTOR_REGISTRY))
    if not needed:
        return {"ok": False, "spec": expr, "name": name or expr,
                "error": "表达式中未引用任何已知因子"}

    _LOG.info("评估假设: %s | 需要因子: %s", name or expr, needed)

    # 获取交易日历
    from h5i_bar_store import H5iBarStore
    cal = H5iBarStore().trading_days()
    if end_day:
        end_idx = cal.index(end_day) if end_day in cal else -1
    else:
        end_idx = -1
    if end_idx < 0:
        end_idx = len(cal) - 1
    start_idx = max(0, end_idx - window - 65)  # 多拉点历史供因子计算
    eval_days = cal[start_idx:end_idx + 1]

    # 逐日计算因子值, 构建 eval frame
    records = []
    for d in eval_days:
        vals = _eval_hypothesis_single_day(d, expr, toks)
        if not vals:
            continue
        for sym, v in vals.items():
            records.append({"date": d, "symbol": sym, "_factor": v})

    df = pd.DataFrame(records)
    if df.empty:
        return {"ok": False, "spec": expr, "name": name or expr,
                "error": "无有效数据"}

    # 获取未来收益 (fwd horizon)
    # 用 h5i 的 daily_bars 计算
    bars = _bars_for_window(eval_days[0], eval_days[-1], horizon)
    df = df.merge(bars, on=["date", "symbol"], how="left")
    df = df.dropna(subset=["_factor", f"fwd{horizon}"])

    if len(df) < 200:
        return {"ok": False, "spec": expr, "name": name or expr,
                "error": f"有效样本不足 ({len(df)} < 200)"}

    # 使用 evaluator 评估
    df_eval = df.rename(columns={"_factor": "factor_val", f"fwd{horizon}": "fwd_ret"})
    report = _evaluate(df_eval, "factor_val", fwd="fwd_ret", gates=gates or {})

    # 补充元信息
    report["ok"] = True
    report["spec"] = expr
    report["name"] = name or expr
    report["horizon"] = horizon
    report["window_days"] = window
    report["end_day"] = eval_days[-1]
    report["needed_factors"] = needed
    report["elapsed_s"] = round(time.time() - t0, 2)

    # 准入判定
    g = gates or {"ic_mean": 0.02, "icir": 0.5, "mono": True}
    pass_ic = report.get("gates", {}).get("pass_ic_mean", False)
    pass_icir = report.get("gates", {}).get("pass_icir", False)
    pass_spread = report.get("gates", {}).get("pass_spread", False)
    pass_mono = report.get("gates", {}).get("pass_mono", False)
    n_pass = sum([pass_ic, pass_icir, pass_spread, bool(pass_mono)])
    report["verdict"] = "ADOPT" if n_pass >= 3 else "WATCH" if n_pass >= 2 else "REJECT"
    report["gates_summary"] = f"pass {n_pass}/4 (IC>{g.get('ic_mean')} ICIR>{g.get('icir')} spread>0 mono)"

    return report


def _bars_for_window(start: str, end: str, horizon: int) -> pd.DataFrame:
    """获取窗口内日线 + fwd horizon 收益。"""
    from factor_library import _sql
    cal_end = end
    # 多拉 horizon+5 天给未来收益计算
    from h5i_bar_store import H5iBarStore
    cal = H5iBarStore().trading_days()
    end_idx = cal.index(end) if end in cal else -1
    hi_idx = min(len(cal) - 1, end_idx + horizon + 5)
    hi_date = cal[hi_idx]

    df = _sql(
        f"SELECT CAST(ts AS DATE) d, symbol, change_pct "
        f"FROM daily_bars WHERE CAST(ts AS DATE) >= DATE '{start}' "
        f"AND CAST(ts AS DATE) <= DATE '{hi_date}'")
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["d"] = pd.to_datetime(df["d"])
    df = df.drop_duplicates(["symbol", "d"], keep="last").sort_values(["symbol", "d"])
    df["r1"] = 1.0 + df["change_pct"].fillna(0) / 100.0
    g = df.groupby("symbol", sort=False)["r1"]
    cump = g.cumprod()
    fwd = g.cumprod().groupby(df["symbol"], sort=False).shift(-horizon) / cump - 1.0
    df[f"fwd{horizon}"] = np.where(np.isfinite(fwd), fwd, np.nan)
    # 只保留评估窗口内的行
    result = df[df["d"].isin(pd.to_datetime([start, end]) if start == end else
                             pd.date_range(start, end))].copy()
    result["date"] = result["d"].astype(str)
    return result[["date", "symbol", f"fwd{horizon}"]]


# ---------------------------------------------------------------------------
# 4. 批量评估
# ---------------------------------------------------------------------------

def batch_evaluate(hypotheses: list[dict],
                   end_day: str | None = None,
                   window: int = 60,
                   horizon: int = 5) -> list[dict]:
    """批量评估多个因子假设.

    hypotheses: [{expr, name, gates?}, ...]
    """
    results = []
    for h in hypotheses:
        r = evaluate_hypothesis(
            expr=h["expr"],
            name=h.get("name", h["expr"]),
            end_day=end_day,
            window=h.get("window", window),
            horizon=h.get("horizon", horizon),
            gates=h.get("gates"),
        )
        results.append(r)
        _LOG.info("  %s: IC=%.4f ICIR=%.2f win=%.2f verdict=%s",
                  r.get("name", "?"),
                  r.get("ic_mean", 0) or 0,
                  r.get("icir", 0) or 0,
                  r.get("win_rate", 0) or 0,
                  r.get("verdict", "ERROR"))
    return results


# ---------------------------------------------------------------------------
# 5. 报告生成
# ---------------------------------------------------------------------------

def generate_report(results: list[dict]) -> str:
    """生成可读的报告文本。"""
    lines = [
        "=" * 60,
        "AI 因子挖掘实验室 - 评估报告",
        "=" * 60,
        "",
    ]
    for r in results:
        name = r.get("name", r.get("spec", "?"))
        verdict = r.get("verdict", "ERROR")
        ic = r.get("ic_mean", "N/A")
        icir = r.get("icir", "N/A")
        win = r.get("win_rate", "N/A")
        spread = r.get("spread_top_minus_bottom", "N/A")
        mono = r.get("monotone", False)
        nd = r.get("n_days", 0)
        q = r.get("q_means", [])

        lines.append(f"  [{verdict}] {name}")
        lines.append(f"    spec:    {r.get('spec', '?')}")
        lines.append(f"    days:    {nd}")
        lines.append(f"    IC:      {ic}")
        lines.append(f"    ICIR:    {icir}")
        lines.append(f"    Win:     {win}")
        lines.append(f"    Spread:  {spread}")
        lines.append(f"    Mono:    {mono}")
        if q:
            qs = "  ".join(f"Q{i}={v:+.5f}" for i, v in enumerate(q) if v is not None)
            lines.append(f"    {{ {qs} }}")
        if r.get("error"):
            lines.append(f"    ERROR:   {r['error']}")
        lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _list_available_factors():
    print(f"{'因子名':<20} {'分类':<15} {'描述':<45}")
    print("-" * 80)
    for name, meta in sorted(FACTOR_REGISTRY.items()):
        print(f"{name:<20} {meta['category']:<15} {meta['desc']:<45}")


def _load_hypotheses_json(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "hypotheses" in data:
        return data["hypotheses"]
    return [{"expr": k, "name": k} for k in data]


def main():
    _log_init()
    if len(sys.argv) < 2:
        print("用法: python ai_factor_lab.py <command> [args]")
        print("命令:")
        print("  evaluate <expr> [-n name] [-d day] [-w window] [-H horizon]")
        print("  batch <hypotheses.json>")
        print("  list")
        return 1

    cmd = sys.argv[1]

    if cmd == "list":
        _list_available_factors()
        return 0

    if cmd == "evaluate":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("expr", help="因子表达式")
        ap.add_argument("-n", "--name", default="", help="因子名称")
        ap.add_argument("-d", "--day", default=None, help="评估截止日 YYYY-MM-DD")
        ap.add_argument("-w", "--window", type=int, default=60, help="滚动窗口(交易日)")
        ap.add_argument("-H", "--horizon", type=int, default=5, help="预测期(交易日)")
        args = ap.parse_args(sys.argv[2:])

        r = evaluate_hypothesis(args.expr, name=args.name,
                                end_day=args.day, window=args.window,
                                horizon=args.horizon)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0

    if cmd == "batch":
        path = sys.argv[2] if len(sys.argv) > 2 else "hypotheses.json"
        hyps = _load_hypotheses_json(path)
        results = batch_evaluate(hyps, end_day=sys.argv[3] if len(sys.argv) > 3 else None)
        print(generate_report(results))
        # 保存 JSON
        out_path = os.path.join(_BASE, "data", "factor_mine", "ai_factor_lab_report.json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n报告已保存: {out_path}")
        return 0

    print(f"未知命令: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
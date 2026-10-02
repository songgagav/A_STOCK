# -*- coding: utf-8 -*-
"""离线回放市场状态路由与动态权重 (P0 shadow research).

本脚本只读取历史数据并生成研究报告，不修改选股、下单或生产状态。
它把三件事放在同一份可审计输出里:

1. 用市场广度表的平均涨跌幅识别 ``bull/bear/sideways/high_vol``;
2. 用 ``factor_library.compute_factors`` 复算 PIT 原始因子，并按状态统计
   5 日 RankIC、负 IC 占比和多空十分位收益差;
3. 用 ``regime_detector.route_factor_weights`` 展示状态路由后的权重，
   与既有 ``factor_fusion.FACTOR_WEIGHTS`` 对照。

默认输出位于 data/ 下，属于运行时研究产物，受 .gitignore 保护。
依赖说明: factor_fusion/factor_library 的 h5i 读取通常需要 .venv310；
若环境缺少 h5i_db，脚本必须明确失败，不得生成空报告伪装成功。

用法:
    .venv310\\Scripts\\python.exe scripts\\research\\regime_route_report.py \\
        --end 2026-09-04 --days 20
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_BREADTH = REPO_ROOT / "data" / "views" / "parquet" / "v_market_breadth.parquet"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "evolution" / "regime_route_report.json"


def _rank_average(values: np.ndarray) -> np.ndarray:
    """Return average ranks without requiring scipy."""
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(len(values), dtype=float)
    pos = 0
    while pos < len(values):
        end = pos + 1
        while end < len(values) and sorted_values[end] == sorted_values[pos]:
            end += 1
        ranks[order[pos:end]] = (pos + 1 + end) / 2.0
        pos = end
    return ranks


def spearman_ic(values: Iterable[float], forward_returns: Iterable[float]) -> float | None:
    """Compute Spearman RankIC for two finite, aligned sequences."""
    x = np.asarray(list(values), dtype=float)
    y = np.asarray(list(forward_returns), dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if int(mask.sum()) < 3:
        return None
    xr = _rank_average(x[mask])
    yr = _rank_average(y[mask])
    xstd = float(np.std(xr))
    ystd = float(np.std(yr))
    if xstd <= 1e-12 or ystd <= 1e-12:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])


def decile_spread(values: Iterable[float], forward_returns: Iterable[float]) -> float | None:
    """Return top-decile minus bottom-decile mean forward return."""
    x = np.asarray(list(values), dtype=float)
    y = np.asarray(list(forward_returns), dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if len(x) < 10:
        return None
    order = np.argsort(x, kind="mergesort")
    n = max(1, int(math.ceil(len(x) * 0.10)))
    bottom = y[order[:n]]
    top = y[order[-n:]]
    return float(np.mean(top) - np.mean(bottom))


def factor_day_metrics(
    factor_values: Mapping[str, float], forward_returns: Mapping[str, float]
) -> dict:
    """Calculate one day's cross-sectional metrics for a factor mapping."""
    symbols = sorted(set(factor_values) & set(forward_returns))
    values = [factor_values[s] for s in symbols]
    returns = [forward_returns[s] for s in symbols]
    ic = spearman_ic(values, returns)
    spread = decile_spread(values, returns)
    return {
        "n": int(np.sum(np.isfinite(values) & np.isfinite(returns))),
        "ic": round(ic, 8) if ic is not None else None,
        "decile_spread": round(spread, 8) if spread is not None else None,
    }


def aggregate_metrics(rows: list[dict]) -> dict:
    """Aggregate daily IC/spread rows without turning missing data into zero."""
    ics = [float(r["ic"]) for r in rows if r.get("ic") is not None]
    spreads = [float(r["decile_spread"]) for r in rows
               if r.get("decile_spread") is not None]
    mean_ic = float(np.mean(ics)) if ics else None
    ic_std = float(np.std(ics, ddof=1)) if len(ics) >= 2 else None
    icir = (mean_ic / ic_std) if mean_ic is not None and ic_std and ic_std > 1e-12 else None
    return {
        "n_days": len(rows),
        "n_ic": len(ics),
        "n_spread": len(spreads),
        "mean_ic": round(mean_ic, 8) if mean_ic is not None else None,
        "icir": round(float(icir), 8) if icir is not None else None,
        "negative_ic_share": (round(float(np.mean(np.asarray(ics) < 0)), 8)
                              if ics else None),
        "mean_decile_spread": round(float(np.mean(spreads)), 8) if spreads else None,
        "n_samples": int(sum(int(r.get("n", 0)) for r in rows)),
    }


def _load_breadth(path: Path, end: str | None) -> list[tuple[str, float]]:
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("缺少 pandas，无法读取市场广度 parquet") from exc
    if not path.exists():
        raise FileNotFoundError(f"市场广度文件不存在: {path}")
    frame = pd.read_parquet(path)
    required = {"date", "avg_change_pct"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"市场广度缺少字段: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["avg_change_pct"] = pd.to_numeric(frame["avg_change_pct"], errors="coerce")
    frame = frame.dropna(subset=["date", "avg_change_pct"])
    if end:
        frame = frame[frame["date"] <= pd.Timestamp(end)]
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    return [(d.strftime("%Y-%m-%d"), float(v) / 100.0)
            for d, v in zip(frame["date"], frame["avg_change_pct"])
            if np.isfinite(v)]


def _regime_series(
    dates: list[str], returns: list[tuple[str, float]], *, trend_window: int, vol_window: int
) -> dict[str, dict]:
    from regime_detector import classify_market_regime

    regimes: dict[str, dict] = {}
    market = sorted(returns)
    for day in sorted(dates):
        # Include observations before the replay window.  A decision on the
        # first replay day is allowed to see its prior market history; using
        # only the report rows would manufacture ``unknown`` states.
        history = [value for date, value in market if date <= day]
        info = classify_market_regime(history, trend_window=trend_window,
                                      vol_window=vol_window,
                                      min_observations=max(trend_window, vol_window))
        regimes[day] = info
    return regimes


def _route_summary(regimes: Iterable[str]) -> dict[str, dict[str, float]]:
    from factor_fusion import FACTOR_WEIGHTS
    from regime_detector import route_factor_weights

    out = {}
    for regime in sorted(set(regimes)):
        out[regime] = {k: round(v, 8) for k, v in
                       route_factor_weights(FACTOR_WEIGHTS, regime).items()}
    return out


def build_report(
    *, end: str | None, days: int, breadth_path: Path,
    trend_window: int = 20, vol_window: int = 20,
) -> dict:
    """Build a report using the existing PIT factor and fusion pipelines."""
    if days < 1:
        raise ValueError("--days 必须为正整数")
    try:
        from factor_fusion import score_series_hist
        from factor_library import compute_factors
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "研究回放需要 h5i 运行环境；请使用 .venv310\\Scripts\\python.exe "
            f"(原始错误: {exc})"
        ) from exc

    market = _load_breadth(breadth_path, end)
    if not market:
        raise RuntimeError("市场广度在指定结束日期前没有有效数据")
    fused = score_series_hist(end=end, days=days)
    samples = fused.get("samples") or []
    if not samples:
        raise RuntimeError(f"融合历史回放为空: {fused.get('error', 'unknown error')}")
    samples = samples[-days:]
    regimes = _regime_series([str(s["date"]) for s in samples], market,
                              trend_window=trend_window, vol_window=vol_window)

    evaluated = {"ep": "ep", "bp": "pb_inv", "roe_yy_chg": "roe_yy_chg", "rev_yoy": "rev_yoy"}
    factor_rows: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    fused_rows: dict[str, list[dict]] = defaultdict(list)
    daily = []
    errors = []
    started = time.time()

    for idx, sample in enumerate(samples, start=1):
        day = str(sample["date"])
        regime_info = regimes.get(day, {"ok": False, "regime": "unknown"})
        regime = regime_info.get("regime", "unknown")
        forward = sample.get("fwd5") or {}
        fused_metric = factor_day_metrics(sample.get("scores") or {}, forward)
        fused_rows[regime].append(fused_metric)
        item = {
            "date": day,
            "regime": regime,
            "regime_info": regime_info,
            "n_fused": int(sample.get("n_scored", 0)),
            "fused_5d": fused_metric,
            "factors": {},
        }
        try:
            raw_result = compute_factors(day, factors=list(evaluated), return_raw=True)
            if raw_result.get("error"):
                raise RuntimeError(str(raw_result["error"]))
            raw = raw_result.get("factors") or {}
            for source_name, report_name in evaluated.items():
                values = {symbol: row.get(source_name, np.nan)
                          for symbol, row in raw.items()}
                metric = factor_day_metrics(values, forward)
                item["factors"][report_name] = metric
                factor_rows[regime][report_name].append(metric)
        except Exception as exc:  # noqa: BLE001 - recorded and surfaced below
            error = f"{day}: {type(exc).__name__}: {exc}"
            errors.append(error)
            item["factor_error"] = error
        daily.append(item)
        if idx == 1 or idx == len(samples) or idx % 5 == 0:
            print(f"[replay] {idx}/{len(samples)} {day} regime={regime} "
                  f"factors={len(item['factors'])}", flush=True)

    by_regime = {}
    for regime in sorted(set(regimes[d]["regime"] for d in regimes)):
        by_regime[regime] = {
            "n_days": sum(1 for x in daily if x["regime"] == regime),
            "fused_5d": aggregate_metrics(fused_rows.get(regime, [])),
            "factors": {name: aggregate_metrics(rows)
                        for name, rows in sorted(factor_rows.get(regime, {}).items())},
        }
    regime_names = [x["regime"] for x in daily]
    routed_only = ["ocf_ps", "gp4"]
    return {
        "ok": not errors,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "as_of_start": fused.get("as_of_start"),
        "as_of_end": fused.get("as_of_end"),
        "n_days": len(daily),
        "market_breadth": str(breadth_path.relative_to(REPO_ROOT))
        if breadth_path.is_relative_to(REPO_ROOT) else str(breadth_path),
        "evaluated_factors": sorted(set(evaluated.values())),
        "routed_only_factors": routed_only,
        "by_regime": by_regime,
        "routed_weights": _route_summary(regime_names),
        "daily": daily,
        "errors": errors,
        "note": "研究/影子回放；未接入生产选股、调仓或下单路径。ocf_ps/gp4仅展示既有路由先验，未在本报告中单独复算。",
        "elapsed_s": round(time.time() - started, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", default="2026-09-04", help="回放结束日 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=20, help="回放交易日数量")
    parser.add_argument("--trend-window", type=int, default=20)
    parser.add_argument("--vol-window", type=int, default=20)
    parser.add_argument("--breadth", type=Path, default=DEFAULT_BREADTH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        report = build_report(end=args.end, days=args.days, breadth_path=args.breadth,
                              trend_window=args.trend_window, vol_window=args.vol_window)
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"[error] regime_route_report 未完成: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {args.output}")
    print(json.dumps({"ok": report["ok"], "n_days": report["n_days"],
                      "by_regime": report["by_regime"], "errors": report["errors"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

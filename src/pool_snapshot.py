# -*- coding: utf-8 -*-
"""池快照: 把一次全市场 PIT 选股的**完整打分池**落盘, 使任意排序口径可离线复算.

动机(见 docs/perf-plan.md 第 1 步):
  截尾比例 / 融合排序等"口径"只改变**排序键的组装方式**, 不改变池子与因子值本身。
  但原实现把口径标签写进了缓存名(`_t{q}` / `_rf{mode}a{alpha}`), 导致换个口径就要
  重算整套 66 次全市场选股(约 3.5~5 小时)。落盘池快照后, 第二次起任意口径**秒级**。

落盘内容(每行一个标的):
  canon/name/price + 六个因子字段(signal/trend/vol/mom_rev/govern/liquidity)
  + `score`(**掺入前的旧复合分**, `_blend_fml` 会改写它, 故必须原样保存)
  + `fml_raw`(factor_fusion.fusion_or_fml 的原始融合值, `_blend_fml` 用)
  + `fml_z`(factor_fusion.cross_section_scores 的 z, `_apply_fusion_rank` 用)

排序复算严格对齐 selector 的语义:
  - `rank_by="score"`  : 直接用 `score` 排序(等价 RANK_BY_FUSION=0, TRIM=0)
  - `rank_by="fusion"` : `fusion_rank_key = (1-alpha)*pct(score) + alpha*pct(zs_trimmed)`
                         (等价 `_apply_fusion_rank`, 截尾用 `trim_top` 后置为 nanmin)
  - `blend_w>0`        : 复算 `_blend_fml` 的"小权重掺入"口径
                         (`score' = (1-w)*score + w*rank(fml_raw)`, 被截尾者 rank=0)

文件名带数据版本(`{day}_n{n}_{ver}.parquet`), 数据一变旧快照自然失效。
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from config import DATA_DIR

SNAP_DIR = os.path.join(DATA_DIR, "pit", "pool_snapshot")

# 落盘的因子列(与 selector._select_hist 写入 scored 的字段一致)
FIELDS = ["canon", "name", "price", "signal", "trend", "vol", "mom_rev",
          "govern", "liquidity", "score", "fml_raw", "fml_z"]


def data_version() -> str:
    """PIT 缓存使用的数据版本(与 vnpy_backtest._data_version 一致)."""
    try:
        from vnpy_backtest import _data_version
        return _data_version()
    except Exception:  # noqa: BLE001
        return "np"


def snap_path(day: str, n: int, ver: str | None = None) -> str:
    return os.path.join(SNAP_DIR, f"{day}_n{int(n)}_{ver or data_version()}.parquet")


def save(day: str, n: int, scored: list, fml_raw: dict | None = None,
         fml_z: dict | None = None) -> str | None:
    """落盘池快照. 任何异常都静默返回 None(不阻断选股主流程)."""
    if not scored:
        return None
    fml_raw = fml_raw or {}
    fml_z = fml_z or {}

    def _sym6(c):
        return str(c or "").split(".")[0].zfill(6)

    rows = []
    for s in scored:
        canon = s.get("canon")
        rows.append({
            "canon": canon, "name": s.get("name") or "", "price": s.get("price"),
            "signal": s.get("signal"), "trend": s.get("trend"), "vol": s.get("vol"),
            "mom_rev": s.get("mom_rev"), "govern": s.get("govern"),
            "liquidity": s.get("liquidity"), "score": s.get("score"),
            "fml_raw": fml_raw.get(canon, np.nan) if fml_raw else np.nan,
            "fml_z": fml_z.get(_sym6(canon), np.nan) if fml_z else np.nan,
        })
    try:
        os.makedirs(SNAP_DIR, exist_ok=True)
        pd.DataFrame(rows, columns=FIELDS).to_parquet(snap_path(day, n), index=False)
        return snap_path(day, n)
    except Exception:  # noqa: BLE001
        return None


def load(day: str, n: int, ver: str | None = None) -> pd.DataFrame | None:
    p = snap_path(day, n, ver)
    if not os.path.exists(p):
        return None
    try:
        return pd.read_parquet(p)
    except Exception:  # noqa: BLE001
        return None


def pct_rank(a: np.ndarray) -> np.ndarray:
    """与 selector._apply_fusion_rank 内的 _pct_rank 完全一致."""
    a = np.asarray(a, dtype=float)
    o = np.argsort(np.argsort(a))
    return o / max(len(a) - 1, 1)


def fusion_key_from_snapshot(df: pd.DataFrame, alpha: float = 1.0,
                            trim_q: float = 0.0) -> np.ndarray:
    """复算 `_apply_fusion_rank` 的 fusion_rank_key(严格同口径)."""
    from selector import trim_top
    zs = df["fml_z"].to_numpy(dtype=float)
    if int(np.isfinite(zs).sum()) < 30:
        return np.full(len(df), np.nan)
    med = float(np.nanmedian(zs))
    zs = np.where(np.isfinite(zs), zs, med)
    zs_t, trimmed = trim_top(zs, trim_q)
    if trimmed.any():
        zs_t = np.where(trimmed, float(np.nanmin(zs)), zs_t)
    # NaN 的 score 按 selector 的 `float(x.get("score") or 0.0)` 口径当 0 处理,
    # 保证复算与实时路径逐位一致。
    old = np.nan_to_num(df["score"].to_numpy(dtype=float), nan=0.0)
    return (1.0 - alpha) * pct_rank(old) + alpha * pct_rank(zs_t)


def blend_score_from_snapshot(df: pd.DataFrame, w: float = 0.10,
                              trim_q: float = 0.0) -> np.ndarray:
    """复算 `_blend_fml` 的掺入口径: score' = (1-w)*score + w*rank(fml_raw).

    被截尾者 rank 记 **0.0**(与 `_blend_fml` 完全一致), 而非 nanmin。
    """
    from selector import trim_top
    vals = df["fml_raw"].to_numpy(dtype=float)
    vals_t, trimmed = trim_top(vals, trim_q)
    good = ~np.isnan(vals_t)
    ranks = np.full(len(df), np.nan)
    if int(good.sum()) > 1:
        order = np.argsort(np.argsort(vals_t[good]))
        ranks[good] = order / (good.sum() - 1.0)
    if trimmed.any():
        ranks[trimmed] = 0.0
    base = df["score"].to_numpy(dtype=float)
    out = np.where(np.isnan(ranks), base, (1.0 - w) * base + w * ranks)
    return out


def top_from_snapshot(df: pd.DataFrame, n: int = 10, rank_by: str = "score",
                      alpha: float = 1.0, trim_q: float = 0.0,
                      blend_w: float = 0.0) -> list[dict]:
    """从快照复算 Top-N. `rank_by='score'` 等价原生产路径; 'fusion' 等价 RANK_BY_FUSION."""
    d = df.copy()
    if rank_by == "fusion":
        d["_key"] = fusion_key_from_snapshot(d, alpha=alpha, trim_q=trim_q)
        if not np.isfinite(d["_key"].to_numpy(dtype=float)).any():
            d["_key"] = d["score"]
    elif blend_w > 0:
        d["_key"] = blend_score_from_snapshot(d, w=blend_w, trim_q=trim_q)
    else:
        d["_key"] = d["score"]
    # `kind="stable"`: Python 的 list.sort 是稳定的, 实时路径遇到**同分标的**会保持
    # 原始(uiverse)顺序; pandas 默认快排不稳定 ⇒ 同分两人的先后会不一致(实测出现
    # "仅两两对调"的差异)。必须用稳定排序才能与实时结果逐位相同。
    d = d.sort_values("_key", ascending=False, kind="stable")
    return d.head(int(n)).to_dict("records")


def available_days(n: int = 10, ver: str | None = None) -> list:
    """已落盘的快照日期(升序)."""
    if not os.path.isdir(SNAP_DIR):
        return []
    suf = f"_n{int(n)}_{ver or data_version()}.parquet"
    return sorted(f[:-len(suf)] for f in os.listdir(SNAP_DIR) if f.endswith(suf))

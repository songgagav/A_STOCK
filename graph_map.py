# ============================================================
# graph_map.py -- 知识图谱(概念/行业)接入 LLM 上下文
#
# 改造①: 把 data/concept_map.json + data/industry_map.json
#   (canon -> {name, tags}, 同花顺同源, 5559 只) 真正接入
#   pre_drl_brief / llm_commentary 的 evidence 链路.
#
# 提供能力:
#   load_graphs()          懒加载两个图谱 (进程内缓存)
#   tag_stats(canons)      对一组 canon/6位code 反查行业/概念 -> 聚合统计
#   market_hot_tags(day)   DuckDB 当日涨幅榜 topN 反查概念热度 (轮动研判用)
#   build_graph_evidence() 汇总成 LLM evidence 块 (available 标记 + 控制 token)
#
# 设计约束:
#   - 图谱缺失/查询失败一律容错, 返回 {"available": False}, 绝不抛异常.
#   - 输出只保留高信号聚合 (一级行业分布 + top 概念频次 + 逐符号简表),
#     控制 prompt token 防止上下文膨胀.
# ============================================================

from __future__ import annotations

import json
import os
from typing import Any

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CONCEPT_PATH = os.path.join(_DATA_DIR, "concept_map.json")
_INDUSTRY_PATH = os.path.join(_DATA_DIR, "industry_map.json")

# 进程内懒加载缓存: 原样 map + 6位code 索引
_cache: dict[str, Any] = {
    "concept": None, "industry": None,
    "concept_by_code": None, "industry_by_code": None,
    "loaded": False,
}


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def load_graphs() -> tuple[dict, dict]:
    """懒加载 (concept_map, industry_map) 的 map 子字段.
    返回 (concept_map_map, industry_map_map), 均为 {canon: {name, tags}}.
    """
    if _cache["loaded"]:
        return _cache["concept"] or {}, _cache["industry"] or {}
    c = _load_json(_CONCEPT_PATH)
    i = _load_json(_INDUSTRY_PATH)
    cm = c.get("map") if isinstance(c.get("map"), dict) else {}
    im = i.get("map") if isinstance(i.get("map"), dict) else {}
    _cache["concept"] = cm
    _cache["industry"] = im
    _cache["concept_by_code"] = _build_code_index(cm)
    _cache["industry_by_code"] = _build_code_index(im)
    _cache["loaded"] = True
    return cm, im


def _build_code_index(mapping: dict) -> dict:
    """canon -> 6位code 倒查索引. 兼容 "600395.SH" / "832317.BJ" / 裸 6 位."""
    out = {}
    for canon, v in mapping.items():
        code = str(canon).split(".")[0] if "." in str(canon) else str(canon)
        out[code] = v
    return out


def _first_sector(tag: str) -> str:
    """行业三级链 "汽车-汽车零部件-其他汽车零部件" -> 一级 "汽车"."""
    s = str(tag).strip()
    return s.split("-")[0].strip() if "-" in s else s


def _lookup(code: str) -> dict:
    """给定 6 位 code, 返回 {name, sector, concepts} 图谱摘要 (无则空)."""
    cm, im = load_graphs()
    if not cm and not im:
        return {}
    c = (_cache["concept_by_code"] or {}).get(code) or {}
    i = (_cache["industry_by_code"] or {}).get(code) or {}
    if not c and not i:
        return {}
    name = (c.get("name") or i.get("name") or code)
    sector = None
    if i.get("tags"):
        sector = i["tags"][0] if isinstance(i["tags"], list) else str(i.get("tags"))
    concepts = []
    if c.get("tags") and isinstance(c["tags"], list):
        concepts = [str(t) for t in c["tags"]][:4]  # 控制 token: 每只最多 4 个概念
    return {
        "canon": code,
        "name": name,
        "sector": sector,
        "concepts": concepts,
    }


def _norm_code(canon: str) -> str:
    """任意标识 -> 6位code 候选. canon 可能带后缀; 也可能就是 6 位."""
    s = str(canon).strip()
    if "." in s:
        return s.split(".")[0]
    return s


def tag_stats(canons: list[str], per_symbol_k: int = 6,
              concept_k: int = 8, sector_k: int = 6) -> dict:
    """对一组 canon/6位code 反查行业与概念, 输出聚合统计.

    Returns
    -------
    dict: {
      "available": bool,          # 图谱数据存在
      "n_input": int, "n_matched": int,
      "sector_freq": [{"sector", "n"}],     # 一级行业分布 (降序)
      "concept_freq": [{"tag", "n"}],       # 概念频次 (降序)
      "per_symbol": [{"canon","name","sector","concepts"}],  # 前 per_symbol_k 只
    }
    """
    cm, im = load_graphs()
    if not cm and not im:
        return {"available": False, "n_input": len(canons or []), "n_matched": 0,
                "sector_freq": [], "concept_freq": [], "per_symbol": []}
    canons = canons or []
    sector_cnt: dict[str, int] = {}
    concept_cnt: dict[str, int] = {}
    per_symbol: list[dict] = []
    matched = 0
    for c in canons:
        info = _lookup(_norm_code(c))
        if not info:
            continue
        matched += 1
        if info.get("sector"):
            s1 = _first_sector(info["sector"])
            sector_cnt[s1] = sector_cnt.get(s1, 0) + 1
        for tag in info.get("concepts") or []:
            concept_cnt[tag] = concept_cnt.get(tag, 0) + 1
        if len(per_symbol) < per_symbol_k:
            per_symbol.append(info)
    sector_freq = [{"sector": k, "n": v} for k, v in
                   sorted(sector_cnt.items(), key=lambda x: -x[1])[:sector_k]]
    concept_freq = [{"tag": k, "n": v} for k, v in
                    sorted(concept_cnt.items(), key=lambda x: -x[1])[:concept_k]]
    return {
        "available": True,
        "n_input": len(canons),
        "n_matched": matched,
        "sector_freq": sector_freq,
        "concept_freq": concept_freq,
        "per_symbol": per_symbol,
    }


def market_hot_tags(day: str, top_n: int = 20, concept_k: int = 8) -> dict:
    """DuckDB 取 <= day 最近交易日涨幅榜 top_n, 反查概念/行业热度.
    供 pre_drl_brief 研判轮动强度(rotation_intensity) 与 regime.
    """
    try:
        import duckdb
        from config import DUCKDB_PATH
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    try:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT MAX(date) FROM daily_bars WHERE date <= ?", [day]
            ).fetchone()
            if not row or not row[0]:
                return {"available": False, "reason": "无行情数据"}
            max_date = row[0]
            rows = con.execute(
                "SELECT symbol FROM daily_bars "
                "WHERE date = ? AND change_pct IS NOT NULL "
                "AND isnan(change_pct) = false "
                "ORDER BY change_pct DESC LIMIT ?",
                [max_date, top_n],
            ).fetchall()
        finally:
            con.close()
        codes = [str(r[0]) for r in rows]
        if not codes:
            return {"available": False, "reason": "当日涨幅榜为空"}
        stats = tag_stats(codes, per_symbol_k=0, concept_k=concept_k, sector_k=6)
        day_iso = (max_date.isoformat() if hasattr(max_date, "isoformat")
                   else str(max_date)[:10])
        return {
            "available": bool(stats.get("available")),
            "day": day_iso,
            "top_n": len(codes),
            "concept_freq": stats.get("concept_freq", []),
            "sector_freq": stats.get("sector_freq", []),
        }
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def build_graph_evidence(day: str,
                         attribution: list | None = None,
                         with_hot: bool = True,
                         hot_top: int = 20) -> dict:
    """组装图谱 evidence 块 (供 pre_drl_brief / llm_commentary 拼接).

    Parameters
    ----------
    day : str           目标交易日 YYYY-MM-DD
    attribution : list  绩效归因列表, 元素含 "canon" 键 (可选)
    with_hot : bool     是否附加当日涨幅榜概念热度 (pre_drl_brief 建议 True)
    hot_top : int       涨幅榜取样数量

    Returns
    -------
    dict: {
      "available": bool,
      "source": str,
      "holding": {...} | None,       # 归因持仓图谱 (有 attribution 时)
      "market_hot": {...} | None,    # 涨幅榜概念热度 (with_hot 时)
    }
    """
    cm, im = load_graphs()
    if not cm and not im:
        return {"available": False, "source": "concept/industry_map.json 缺失",
                "holding": None, "market_hot": None}

    holding = None
    if attribution:
        canons = [a.get("canon") for a in attribution if isinstance(a, dict) and a.get("canon")]
        if canons:
            holding = tag_stats(canons)

    market_hot = None
    if with_hot:
        market_hot = market_hot_tags(day, top_n=hot_top)

    available = bool(holding and holding.get("available")) or bool(
        market_hot and market_hot.get("available"))
    return {
        "available": available,
        "source": "concept_map.json/industry_map.json (同花顺, 5559 只)",
        "holding": holding,
        "market_hot": market_hot,
    }


if __name__ == "__main__":
    import sys
    import json as _json
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-08-26"
    demo = [
        {"canon": "002536.SZ", "weight": 0.1, "unrealized_pnl": 1000},
        {"canon": "600395.SH", "weight": 0.1, "unrealized_pnl": -200},
        {"canon": "300017.SZ", "weight": 0.1, "unrealized_pnl": 50},
    ]
    ev = build_graph_evidence(day, attribution=demo, with_hot=True)
    print(_json.dumps(ev, ensure_ascii=False, indent=2))

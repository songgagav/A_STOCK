# -*- coding: utf-8 -*-
"""DRL 学习后检查（DRL-3）—— 轻量模块，**不依赖 torch / gymnasium / h5i_db**。

设计原则（用户 METHOD-1，已固化于 docs/drl-learning-verification.md §📌）
  「边界/阈值的设定必须基于**该值发生时的下游表现**证据，而非**该值的分布范围**」
  故本模块**只记录数值、不产出任何布尔判定**：
    · 汇总里没有 `passed` / `ok_model` / `converged` 之类字段；
    · 落盘时显式标注 `threshold_applied: false`。
  待积累 1–2 个月「数值 → 下游表现」配对数据后，再回头定"新模型是否该部署"的阈值。

设计要求的五项检查与本模块的**如实对应**（不可算的要标明原因，而不是留空）
  ┌──────────────────────────┬────────────────────────────────────────────┐
  │ 滚动窗口表现（近 N 日）    │ ✅ 可算：读 data/daily/*/daily_summary.json │
  │ 决策一致性（同输入不剧变） │ ◐ 代理：相邻日 target_plan 的 top_n 重合度 │
  │ 参数漂移（权重变化幅度）   │ ✅ 已由 drl_drift.py 实现（DRL-7）          │
  │ 新旧模型对比（验证集新≥旧）│ ❌ 需 torch 推理 → 显式标注原因             │
  │ 验证集切分本身             │ ❌ 尚未定义 → 显式标注原因                  │
  └──────────────────────────┴────────────────────────────────────────────┘
  "不可算"必须**显式落盘原因**（`unavailable`），否则复盘时无法区分
  "这项检查通过了" 与 "这项检查根本没有数据" —— 这正是 DRL-2 的教训。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import os

import config

LEDGER_NAME = "drl_post_metrics.jsonl"
#: 滚动窗口默认天数（设计里写的是"近 20 日"）
ROLLING_DAYS = 20
#: 决策一致性默认看最近 N 个交易日
CONSISTENCY_DAYS = 10


def ledger_path() -> str:
    p = os.environ.get("DRL_POST_LEDGER")
    return p or os.path.join(config.DATA_DIR, LEDGER_NAME)


# --------------------------------------------------------------------- 读数

def _daily_rows() -> "list[tuple[str, float]]":
    """读每日复盘里的权益序列: [(day8, equity)]，按日升序。

    日期以**目录名**为准（必须是 8 位数字），并**严格过滤**非 8 位目录。
    为什么必须过滤: 实测生产 `data/` 下存在一个**游离目录** `data/daily/day/`
    （`daily_summary.json` 里 `day` 字面量是 `--day`、`equity=100000.0`），
    是某次脚本调用把 CLI 占位符当成日期参数落下的产物。若不过滤，它会被
    glob 收进来并**污染滚动窗口**（实测会把 `to` 变成字符串 "day"、并把一条
    虚假权益混入收益/回撤计算）。缺文件/坏 JSON 同样跳过。
    """
    root = os.path.join(config.DATA_DIR, "daily")
    out = []
    for fp in sorted(glob.glob(os.path.join(root, "*", "daily_summary.json"))):
        d8 = os.path.basename(os.path.dirname(fp))
        if not (d8.isdigit() and len(d8) == 8):
            continue                     # 非日期分区 -> 不是本模块的数据
        try:
            with open(fp, encoding="utf-8-sig") as f:
                j = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        eq = ((j.get("summary") or {}).get("equity"))
        try:
            eqv = float(eq)
        except (TypeError, ValueError):
            continue
        if math.isfinite(eqv):
            out.append((d8, eqv))
    out.sort(key=lambda x: x[0])
    return out


def _plan_topn() -> "list[tuple[str, list[str]]]":
    """读 target_plan.json 的 top_n canon 列表: [(day8, [canon,...])]，按日升序。"""
    root = os.path.join(config.DATA_DIR, "drl")
    out = []
    for fp in sorted(glob.glob(os.path.join(root, "*", "target_plan.json"))):
        d8 = os.path.basename(os.path.dirname(fp))
        if not (d8.isdigit() and len(d8) == 8):
            continue
        try:
            with open(fp, encoding="utf-8-sig") as f:
                j = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        canons = [str(it.get("canon")) for it in (j.get("top_n") or [])
                  if isinstance(it, dict) and it.get("canon") is not None]
        if canons:
            out.append((d8, canons))
    out.sort(key=lambda x: x[0])
    return out


# --------------------------------------------------------------------- 计算（只出数值）

def rolling_window_metrics(days: int = ROLLING_DAYS) -> dict:
    """近 N 个交易日的权益曲线数值：收益 / 年化夏普 / 最大回撤 / 波动。

    **只记录数值**。数据不足或曲线无波动时，相应字段为 `None` 并给出 `notes`
    —— 不用 0 冒充（0 会被误读为"没有回撤"）。
    """
    rows = _daily_rows()
    tail = rows[-days:] if days and len(rows) > days else rows
    res = {"n": len(tail), "from": tail[0][0] if tail else None,
           "to": tail[-1][0] if tail else None,
           "equity_first": tail[0][1] if tail else None,
           "equity_last": tail[-1][1] if tail else None,
           "return_pct": None, "sharpe": None, "max_dd_pct": None, "vol_pct": None,
           "notes": []}
    if len(tail) < 2:
        res["notes"].append("交易日不足 2 天, 无法计算收益/回撤")
        return res
    eqs = [e for _d, e in tail]
    base = eqs[0]
    if base:
        res["return_pct"] = round((eqs[-1] / base - 1.0) * 100.0, 6)
    rets = [(eqs[i] / eqs[i - 1] - 1.0) for i in range(1, len(eqs)) if eqs[i - 1]]
    if rets:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / len(rets)
        sd = math.sqrt(var)
        res["vol_pct"] = round(sd * 100.0, 6)
        if sd > 1e-12:
            res["sharpe"] = round(mu / sd * math.sqrt(252.0), 6)
        else:
            res["notes"].append("日收益标准差为 0（权益曲线无波动）, 夏普记为 None")
        peak, mdd = eqs[0], 0.0
        for e in eqs:
            peak = max(peak, e)
            if peak:
                mdd = min(mdd, e / peak - 1.0)
        res["max_dd_pct"] = round(mdd * 100.0, 6)
    return res


def decision_consistency(days: int = CONSISTENCY_DAYS) -> dict:
    """相邻交易日 `target_plan.top_n` 的重合度（Jaccard）—— "决策一致性"的**可算代理**。

    ⚠ 如实标注: 这不是设计要求里"同输入两次推理是否一致"的**严格**定义（那需要 torch
    推理），而是"**隔日选股名单漂移程度**"的代理。两者含义不同，故字段名用
    `topn_jaccard_*` 而非 `decision_consistency_*`，避免被读成严格结论。
    """
    rows = _plan_topn()
    tail = rows[-days:] if days and len(rows) > days else rows
    res = {"n_plans": len(tail), "pairs": 0, "jaccard_mean": None,
           "jaccard_min": None, "identical_pairs": 0, "notes": []}
    if len(tail) < 2:
        res["notes"].append("target_plan 不足 2 份, 无法比较相邻日")
        return res
    js = []
    for i in range(1, len(tail)):
        a, b = set(tail[i - 1][1]), set(tail[i][1])
        u = a | b
        if not u:
            continue
        j = len(a & b) / len(u)
        js.append(j)
        if a == b:
            res["identical_pairs"] += 1
    if js:
        res["pairs"] = len(js)
        res["jaccard_mean"] = round(sum(js) / len(js), 6)
        res["jaccard_min"] = round(min(js), 6)
    return res


def _unavailable_items() -> dict:
    """设计要求里**当前算不了**的项 —— 显式落盘原因, 不留空、不假装通过。"""
    reason_torch = ("需要 torch 推理（加载新旧模型跑同一验证集）; "
                    "当前无解释器同时具备 h5i_db 与 torch（登记册 P0-DRLDEP）, "
                    "且按用户决策 D DRL 训练暂不在生产运行")
    return {
        "new_vs_old": {"available": False, "reason": reason_torch},
    }


# --------------------------------------------------------------------- 学习后验证（DRL-3 余下两项）

#: 验证集留出天数。**这是口径定义, 不是阈值** —— 它决定"拿哪几天当验证",
#: 不决定"多少分算通过"（后者按 METHOD-1 必须等下游表现数据）。
VAL_DAYS_DEFAULT = 5

#: 切分口径的书面说明（随每次落盘一起写入, 便于日后复核"当时是怎么切的"）。
SPLIT_SPEC = {
    "method": "temporal_holdout_tail",
    "desc": ("把评分区间 [lookback, n-1] **按时间顺序**切成两段: 末尾 val_days 天为验证集, "
             "其余为训练段。**不做 shuffle** —— IC 序列是时间序列, 打乱会引入未来信息泄漏。"),
    "limitation": ("IC 序列通常只有 40–60 天, 验证集仅 5 天 ⇒ 验证分数**噪声很大**; "
                   "故本模块只记录数值, 不据它判定'新模型是否该部署'(METHOD-1)。"),
}


def split_validation(n: int, lookback: int = 10,
                     val_days: int = VAL_DAYS_DEFAULT) -> dict:
    """按时间顺序切出训练段/验证段的下标。

    评分区间与 `FactorWeightEnv` 的可评步一致: `t ∈ [lookback, n-1]`
    （`reset()` 把 `t` 置为 `lookback`, `step()` 在 `t >= n` 时 done）。

    返回 `{"scored": [...], "train": [...], "val": [...], "n": n, "val_days": k}`；
    数据不足以切分时 `val` 为空并给出 `notes`。
    """
    scored = list(range(max(0, int(lookback)), max(0, int(n))))
    k = max(0, int(val_days))
    out = {"n": int(n), "lookback": int(lookback), "val_days": k,
           "scored": scored, "train": scored, "val": [], "notes": []}
    if k == 0 or len(scored) <= k:
        out["notes"].append(f"可评步数 {len(scored)} 不足以留出 {k} 天验证集, 验证段为空")
        return out
    out["val"] = scored[-k:]
    out["train"] = scored[:-k]
    return out


def score_weights(ic, base_weights, weights, idx) -> dict:
    """在给定下标上给一组因子权重打分 —— **逐字mirror** `FactorWeightEnv.step` 的奖励式。

    为什么要 mirror 而不是另立一个指标: 只有与训练时**同一个式子**, "验证分数"才能被
    解释为"这套权重在留出日上的**期望环境奖励**", 新旧对比才有意义。原式（drl_train.py
    `FactorWeightEnv.step`）:

        ic_diff    = w·ic[t] - base·ic[t]
        ic_vol     = std(ic[max(0,t-20):t])  (不足 2 行取 0.01), 下限 0.001
        vol_scaling= 1 / (1 + ic_vol*10)
        reward     = ic_diff * 10 * vol_scaling
    """
    import numpy as np
    ic = np.asarray(ic, dtype=np.float64)
    bw = np.asarray(base_weights, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    vals = []
    for t in (idx or []):
        t = int(t)
        if t < 0 or t >= len(ic):
            continue
        ic_diff = float(np.dot(w, ic[t]) - np.dot(bw, ic[t]))
        lo = max(0, t - 20)
        win = ic[lo:t]
        ic_vol = float(np.std(win)) if len(win) > 1 else 0.01
        ic_vol = max(0.001, ic_vol)
        vals.append(ic_diff * 10.0 / (1.0 + ic_vol * 10.0))
    if not vals:
        return {"n": 0, "mean": None, "std": None}
    a = np.asarray(vals, dtype=np.float64)
    return {"n": int(a.size), "mean": round(float(a.mean()), 9),
            "std": round(float(a.std()), 9)}


def validation_compare(ic, base_weights, new_weights, old_weights=None,
                       lookback: int = 10, val_days: int = VAL_DAYS_DEFAULT) -> dict:
    """新旧模型在同一**留出验证段**上的对比 —— **只记录数值, 不判定**。

    `old_weights` 为 None 时（例如没有上一版模型）只记录 new/base, 并给出 note。
    `delta_new_minus_old` 是**唯一**的新旧比较量; 是否"新≥旧就算通过"属阈值决策,
    按 METHOD-1 本批次**不做**, 故输出里没有 passed/ok 之类字段。
    """
    import numpy as np
    sp = split_validation(len(ic), lookback=lookback, val_days=val_days)
    res = {
        "schema": 1,
        "threshold_applied": False,
        "note": "只记录数值, 不判定'新模型是否该部署'(METHOD-1: 阈值须基于下游表现)",
        "split": dict(SPLIT_SPEC),
        "lookback": int(lookback), "val_days": int(sp["val_days"]),
        "n_scored": len(sp["scored"]), "n_train": len(sp["train"]),
        "val_index": (sp["val"][0], sp["val"][-1]) if sp["val"] else None,
        "new": score_weights(ic, base_weights, new_weights, sp["val"]),
        "base": score_weights(ic, base_weights, base_weights, sp["val"]),
        "old": (score_weights(ic, base_weights, old_weights, sp["val"])
                if old_weights is not None else None),
        "delta_new_minus_old": None,
        "notes": list(sp["notes"]),
    }
    if old_weights is not None and res["new"]["mean"] is not None \
            and res["old"]["mean"] is not None:
        res["delta_new_minus_old"] = round(res["new"]["mean"] - res["old"]["mean"], 9)
    if old_weights is None:
        res["notes"].append("未提供上一版模型权重, 新旧对比不可算（只记录 new/base）")
    if res["new"]["n"] == 0:
        res["notes"].append("验证段为空, 未产生任何分数")
    return res


# --------------------------------------------------------------------- 落盘

def validation_from_train_meta(day: "str | None") -> "dict | None":
    """从当日 `train_meta.json` 取 `drl_train` 算好的学习后验证结果。

    为什么要走 train_meta 中转而不是让 drl_train 直接写账本: 账本应当**每天一条**
    （否则复盘里同一天两行, 难对齐）。而验证对比只有训练跑了才算得出来, 故由
    `drl_train` 写进 train_meta, `run_daily`（每天都调）再统一落一条账本。
    """
    d8 = str(day or "").replace("-", "")
    if not (d8.isdigit() and len(d8) == 8):
        return None
    fp = os.path.join(config.DATA_DIR, "drl", d8, "train_meta.json")
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding="utf-8-sig") as f:
            m = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    v = m.get("post_train_validation")
    return v if isinstance(v, dict) else None


def summarize(day: "str | None" = None,
              rolling_days: int = ROLLING_DAYS,
              consistency_days: int = CONSISTENCY_DAYS,
              validation: "dict | None" = None) -> dict:
    """汇总一次"学习后检查"的全部**数值**（不判定）。

    `validation`: 由调用方（有 torch 时是 `drl_train`）传入
    `validation_compare(...)` 的结果。**不传也能落盘** —— 此时把"为什么没有"写进
    `unavailable`, 而不是留空（否则复盘无法区分"检查通过"与"没有数据"）。
    """
    un = _unavailable_items()
    if validation is not None:
        un.pop("new_vs_old", None)          # 已提供 -> 不再标为不可用
    elif not un["new_vs_old"].get("reason"):
        un["new_vs_old"]["reason"] = "调用方未提供 IC 序列与模型权重"
    return {
        "schema": 1,
        "day": str(day or "").replace("-", ""),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "kind": "post_train_metrics",
        # 与 DRL-2/drl_drift 一致的诚实标注: 本批次**不施加**任何阈值
        "threshold_applied": False,
        "note": "只记录数值, 不判定'新模型是否该部署'(METHOD-1: 阈值须基于下游表现)",
        "rolling_window": rolling_window_metrics(rolling_days),
        "topn_jaccard": decision_consistency(consistency_days),
        "params_drift": {"see": "drl_weight_drift.jsonl（由 drl_drift 实现, DRL-7）"},
        "validation_split": dict(SPLIT_SPEC),
        "new_vs_old": validation,
        "unavailable": un,
    }


def record_post_metrics(day: "str | None" = None, extra: dict | None = None,
                        validation: "dict | None" = None) -> dict:
    """计算并追加一条到 `data/drl_post_metrics.jsonl`。**绝不抛异常**。

    `validation` 未显式传入时, 自动从当日 `train_meta.json` 的
    `post_train_validation` 取（DRL 训练跑了才有）。**每天只落一条**。
    """
    if validation is None:
        try:
            validation = validation_from_train_meta(day)
        except Exception:  # noqa: BLE001
            validation = None
    try:
        rec = summarize(day, validation=validation)
    except Exception as e:  # noqa: BLE001
        rec = {"schema": 1, "day": str(day or "").replace("-", ""),
               "kind": "post_train_metrics", "threshold_applied": False,
               "error": f"{type(e).__name__}: {e}"}
    if extra:
        rec.update(extra)
    try:
        p = ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  落盘失败不得影响主链路
        pass
    return rec


__all__ = ["LEDGER_NAME", "ROLLING_DAYS", "CONSISTENCY_DAYS", "VAL_DAYS_DEFAULT",
           "SPLIT_SPEC", "ledger_path", "rolling_window_metrics",
           "decision_consistency", "split_validation", "score_weights",
           "validation_compare", "summarize", "record_post_metrics"]

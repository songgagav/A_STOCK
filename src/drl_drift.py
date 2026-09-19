# -*- coding: utf-8 -*-
"""DRL 因子权重漂移检查（轻量模块：**不依赖 torch / gymnasium / stable_baselines3**）.

为什么要独立成模块
  漂移检查只需要读 train_meta 里的 `base_weights`/`prior_weights`/`final_weights`
  并写一行账本 —— 与 DRL 训练框架毫无关系。原先它写在 `drl_train.py` 里, 后者在导入时
  就 `import gymnasium/torch/stable_baselines3`, 导致:
    · 想跑漂移分布报告必须装全套 DRL 依赖(实际只需 pandas 级别);
    · **测试若 import drl_train, 在 CI 的 core job(不装 torch)会收集失败** 而把 CI 弄红。
  故独立成模块: `drl_train` 与报告/测试都从这里导入。

背景（DRL 训练的直接产物是**因子权重**）
  `train_meta.final_weights` 是 6 个因子权重, 键为 signal/trend/govern/liquidity/vol/mom_rev;
  股票 top_n 是由 `drl_final_weights x v_universe_snapshot` 二级算出的。

**只告警, 不阻断**（用户 2026-09-19 明确要求）
  阻断需要 DRL-4 降级链先就位, 否则会引入新的静默行为 —— 权重被改了, 但没人知道改得是否合理。

**阈值 0.3 是保守初值, 不是标定结果**（用户特别提醒）
  「不要在只有单日数据时就定死阈值 —— 那会重复『用单日抽样得出错误结论』的问题」。
  每条记录都追加到账本(`data/drl_weight_drift.jsonl`), 运行 1-2 周后再决定收紧或放宽。
  可用环境变量 `DRL_DRIFT_THRESHOLD` 覆盖; 账本路径可用 `DRL_DRIFT_LEDGER` 覆盖(测试用)。

两种漂移都算, 含义不同:
  · drift_base_to_final : 单次训练把权重从 base 推离多远(用户给的检查式)
  · drift_day_over_day  : 与**上一个有 final_weights 的交易日**比 —— 真正要标定的"日间漂移"
"""
from __future__ import annotations

import datetime as dt
import json
import os

import config
from dataguard import warn_once

#: 告警阈值。**保守初值**, 待数据积累后标定(见模块 docstring)。
DRIFT_THRESHOLD_DEFAULT = 0.3
#: 因子权重键(与 train_meta.final_weights 一致)
DRIFT_FACTORS = ("signal", "trend", "govern", "liquidity", "vol", "mom_rev")
LEDGER_NAME = "drl_weight_drift.jsonl"


def ledger_path() -> str:
    """漂移账本路径。**每次调用动态解析** `config.DATA_DIR` —— 这样 QUANT_DATA_DIR
    沙箱重定向与测试里的 monkeypatch 都能正确生效(而不是导入时固化一个旧路径)。"""
    p = os.environ.get("DRL_DRIFT_LEDGER")
    return p or os.path.join(config.DATA_DIR, LEDGER_NAME)


def max_abs_weight_diff(a: dict, b: dict) -> "tuple[float, dict]":
    """两组因子权重的逐因子绝对差与最大差。缺失因子按 0 计。"""
    keys = set(a or {}) | set(b or {})
    per = {k: abs(float((a or {}).get(k, 0.0)) - float((b or {}).get(k, 0.0)))
           for k in keys}
    return (max(per.values()) if per else 0.0), per


def prev_final_weights(day: str, exclude_dir: str = "") -> "tuple[str, dict] | None":
    """找**严格早于 day** 的最近一个有 final_weights 的 drl 目录。"""
    root = os.path.join(config.DATA_DIR, "drl")
    if not os.path.isdir(root):
        return None
    day8 = str(day or "").replace("-", "")
    cands = []
    for name in os.listdir(root):
        if not (name.isdigit() and len(name) == 8) or name >= day8:
            continue
        if exclude_dir and os.path.abspath(os.path.join(root, name)) == os.path.abspath(exclude_dir):
            continue
        fp = os.path.join(root, name, "train_meta.json")
        if os.path.isfile(fp):
            cands.append((name, fp))
    for name, fp in sorted(cands, reverse=True):
        try:
            with open(fp, encoding="utf-8") as f:
                m = json.load(f)
            w = m.get("final_weights")
            if isinstance(w, dict) and w:
                return name, w
        except Exception:  # noqa: BLE001
            continue
    return None


def check_weight_drift(meta: dict, out_dir: str | None = None,
                       source: str = "run") -> dict:
    """计算并记录权重漂移。**只告警不阻断**；永不抛异常(内部全兜)。

    返回可直接挂到 `train_meta["weight_drift"]` 的 dict。
    """
    try:
        base = meta.get("base_weights") or {}
        prior = meta.get("prior_weights") or {}
        final = meta.get("final_weights") or {}
        if not final:
            return {"ok": False, "error": "缺少 final_weights"}
        try:
            thr = float(os.environ.get("DRL_DRIFT_THRESHOLD", DRIFT_THRESHOLD_DEFAULT))
        except (TypeError, ValueError):
            thr = DRIFT_THRESHOLD_DEFAULT

        d_bf, per_bf = max_abs_weight_diff(base, final)
        d_pf, _per_pf = max_abs_weight_diff(prior, final)
        d_dod, per_dod, prev_day = None, {}, None
        try:
            prev = prev_final_weights(meta.get("day"), out_dir or "")
            if prev:
                prev_day, pw = prev
                d_dod, per_dod = max_abs_weight_diff(pw, final)
        except Exception:  # noqa: BLE001
            pass

        over = d_bf > thr
        rec = {
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": source,                      # run | backfill
            "day": str(meta.get("day") or "").replace("-", ""),
            "threshold": thr,
            "threshold_is_calibrated": False,      # 明确标注: 0.3 是保守初值
            "drift_base_to_final": round(d_bf, 6),
            "per_factor_base_to_final": {k: round(v, 6) for k, v in per_bf.items()},
            "drift_prior_to_final": round(d_pf, 6),
            "drift_day_over_day": None if d_dod is None else round(d_dod, 6),
            "per_factor_day_over_day": {k: round(v, 6) for k, v in per_dod.items()},
            "prev_day": prev_day,
            "over_threshold": bool(over),
            "action": "warn_only" if over else "ok",   # 只告警, 不阻断
            "final_weights": {k: round(float(v), 6) for k, v in final.items()},
        }
        try:
            lp = ledger_path()
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            with open(lp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

        if over:
            top = max(per_bf, key=per_bf.get) if per_bf else "?"
            warn_once("drl_weight_drift",
                      f"DRL 权重漂移 {d_bf:.3f} 超阈值 {thr:.2f}"
                      f"(base->final, 最大变化因子={top} {per_bf.get(top, 0):.3f}); "
                      f"**不阻断当日 plan**; 阈值待标定")
        return {"ok": True, **rec}
    except Exception as e:  # noqa: BLE001  绝不影响训练主链路
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


__all__ = ["DRIFT_THRESHOLD_DEFAULT", "DRIFT_FACTORS", "LEDGER_NAME",
           "check_weight_drift", "ledger_path", "max_abs_weight_diff",
           "prev_final_weights"]

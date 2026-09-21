# -*- coding: utf-8 -*-
"""DRL 学习**前**的独立检查点（登记册 DRL-1）：最小样本量断言 + 净值连续性。

为什么需要它
------------
原先的学习前检查只有 `drl_train.py:1034` 一句隐式判据 `len(rets) < 15`：
不留痕、不落检查结论 —— 事后无法回答"当天到底检查了什么、结论是什么"。
"数据不足"这条路径虽然在 DRL-4 里接进了降级链（不再静默），但**检查本身仍然不可见**。
本模块把它显式化：判据命名、结论落盘(`precheck.json`)、失败走同一条降级链。

阈值不新造
----------
`MIN_TRAIN_DAYS = 15` **直接沿用仓库既有的 15 日判据**（`drl_train.py:1034` 与 `:517`），
不是我另定的数字。本模块只是把它变成一个有名有姓、会留痕的断言。
实测旁证: 一次真实训练 `data/drl/20181019/train_meta.json` 的 `n_dates = 39`，
故 15 这个 floor 明显低于健康值，属"结构性下限"而非"健康线"。

净值连续性检查的是**结构**，不是收益好坏
----------------------------------------
只报**不可能正常**的形态: 空序列、NaN/inf、非正值(净值 ≤0)、日期与数值长度不符、
日期重复或非升序。**不报**"某天涨跌多少" —— 那需要阈值, 而阈值必须由下游表现定,
不该由本模块拍脑袋。故这里只做**无争议的结构校验**。

拿不到净值数据时的态度: **如实记为未判定**
-------------------------------------------
`vnpy_stats` 在实测样本里是**空字典**, 且训练前当天还没有 train_meta。
故 `evaluate()` 允许 `net_values=None`, 此时 `net_checked=False` 并把"净值连续性未判定"
写进 `not_judged` —— **不假装通过, 也不假装失败**。这本身就是有用的信息:
它说明"净值序列目前根本取不到", 该补的是数据来源, 而不是放宽检查。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime

#: 最小训练样本天数 —— **沿用仓库既有判据**(drl_train.py:1034/:517 的 15 日), 非新造
MIN_TRAIN_DAYS = 15

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def check_min_samples(n_dates, min_days: int = MIN_TRAIN_DAYS) -> dict | None:
    """样本量断言。返回 issue dict 或 None（通过）。"""
    try:
        n = int(n_dates)
    except Exception:  # noqa: BLE001
        return {"code": "samples_unknown", "severity": "CRITICAL",
                "detail": f"样本天数无法解析: {n_dates!r}"}
    if n < int(min_days):
        return {"code": "samples_too_few", "severity": "CRITICAL",
                "detail": f"训练样本 {n} 日 < 下限 {min_days} 日(沿用仓库既有判据)"}
    return None


def check_net_value_continuity(values, dates=None) -> list:
    """净值序列的**结构**校验（无争议项；不做收益好坏的判断）。"""
    issues: list = []
    if values is None:
        return issues
    try:
        vals = list(values)
    except Exception:  # noqa: BLE001
        return [{"code": "net_unparsable", "severity": "CRITICAL",
                 "detail": f"净值序列不可解析: {type(values).__name__}"}]
    if not vals:
        return [{"code": "net_empty", "severity": "CRITICAL", "detail": "净值序列为空"}]

    for i, v in enumerate(vals):
        try:
            f = float(v)
        except Exception:  # noqa: BLE001
            issues.append({"code": "net_non_numeric", "severity": "CRITICAL",
                           "detail": f"第 {i+1} 个净值不是数字: {v!r}"})
            continue
        if math.isnan(f) or math.isinf(f):
            issues.append({"code": "net_nan_inf", "severity": "CRITICAL",
                           "detail": f"第 {i+1} 个净值是 NaN/inf"})
        elif f <= 0:
            issues.append({"code": "net_non_positive", "severity": "CRITICAL",
                           "detail": f"第 {i+1} 个净值 ≤ 0 ({f}) —— 净值不可能非正"})

    if dates is not None:
        try:
            ds = [str(x) for x in dates]
        except Exception:  # noqa: BLE001
            ds = []
        if ds:
            if len(ds) != len(vals):
                issues.append({"code": "net_len_mismatch", "severity": "CRITICAL",
                               "detail": f"日期数 {len(ds)} != 净值数 {len(vals)}"})
            if len(set(ds)) != len(ds):
                issues.append({"code": "net_dup_dates", "severity": "CRITICAL",
                               "detail": "净值日期存在重复 —— 同一交易日出现两次"})
            if ds != sorted(ds):
                issues.append({"code": "net_unsorted_dates", "severity": "CRITICAL",
                               "detail": "净值日期非升序 —— 序列顺序与时间不一致"})
    return issues


def evaluate(n_dates=None, net_values=None, net_dates=None,
             min_days: int = MIN_TRAIN_DAYS) -> dict:
    """**纯函数**: 汇总学习前检查。返回 {ok, issues, not_judged, net_checked, checked_at}。"""
    issues: list = []
    not_judged: list = []

    it = check_min_samples(n_dates, min_days)
    if it:
        issues.append(it)
    if net_values is None:
        not_judged.append({"what": "净值连续性",
                           "why": "训练前取不到净值序列(vnpy_stats 实测为空字典) —— 该补的是数据来源, "
                                  "不是放宽检查"})
        net_checked = False
    else:
        issues.extend(check_net_value_continuity(net_values, net_dates))
        net_checked = True

    return {"ok": not issues, "issues": issues, "not_judged": not_judged,
            "net_checked": net_checked, "n_dates": n_dates, "min_days": min_days,
            "checked_at": datetime.now().strftime(_TS_FMT)}


def load_net_values(day_dir: str):
    """尽力取净值序列；取不到返回 None（**不猜**）。

    依次找: `<day_dir>/net_values.json`、`<day_dir>/vnpy_stats.json`（含 net_values/daily_returns）、
    `<day_dir>/train_meta.json` 的 `vnpy_stats`。实测这些当前多为空, 故多数情况会如实记为未判定。
    """
    def _pick(obj):
        if not isinstance(obj, dict):
            return None
        for k in ("net_values", "nav", "equity_curve", "daily_returns"):
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v
        return None

    for name in ("net_values.json", "vnpy_stats.json"):
        fp = os.path.join(day_dir, name)
        if os.path.isfile(fp):
            try:
                v = _pick(json.load(open(fp, encoding="utf-8-sig")))
                if v:
                    return v
            except Exception:  # noqa: BLE001
                pass
    fp = os.path.join(day_dir, "train_meta.json")
    if os.path.isfile(fp):
        try:
            v = _pick(json.load(open(fp, encoding="utf-8-sig")).get("vnpy_stats") or {})
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
    return None


def record(day_dir: str, verdict: dict, name: str = "precheck.json") -> str | None:
    """把检查结论落盘（原子写）。失败返回 None, **不抛**。"""
    try:
        os.makedirs(day_dir, exist_ok=True)
        fp = os.path.join(day_dir, name)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(verdict, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
        return fp
    except Exception:  # noqa: BLE001
        return None


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DRL 学习前检查: 最小样本量 + 净值连续性")
    ap.add_argument("--day-dir", help="data/drl/<YYYYMMDD> 目录")
    ap.add_argument("--n-dates", type=int, help="样本天数(不给则从 train_meta.n_dates 读)")
    args = ap.parse_args(argv)
    n = args.n_dates
    nv = None
    if args.day_dir:
        nv = load_net_values(args.day_dir)
        if n is None:
            fp = os.path.join(args.day_dir, "train_meta.json")
            if os.path.isfile(fp):
                try:
                    n = json.load(open(fp, encoding="utf-8-sig")).get("n_dates")
                except Exception:  # noqa: BLE001
                    pass
    r = evaluate(n_dates=n, net_values=nv)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())

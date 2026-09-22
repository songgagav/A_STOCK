# -*- coding: utf-8 -*-
"""阶段二: 多源**交叉校验**骨架(判据写死预期值)。

## 为什么校验判据要"写死预期"而不是"看起来差不多"

两个源"数值接近"不是判据 —— 判据必须是**事先声明的、可证伪的等式**:

| 量 | 预期值 | 依据 |
|---|---|---|
| `volume_ratio`(引擎/baostock) | **1.0** | 两者同为**股**(实测 min=max=mean=1.0000) |
| `volume_ratio`(引擎/akshare) | **100.0** | akshare 是**手**(实测 mean=100.0001) |
| OHLC | **逐值相等** | 同口径不复权(实测 8 只×3 日全一致) |
| `change_pct` | **逐值相等(容差小)** | 见下"除权日"说明 |

**写死预期的价值**: 若哪天 baostock 悄悄改了单位(或我们接错了字段),
`volume_ratio` 会从 1.0 跳到 100.0 —— **等式立刻不成立**, 而不是"看着还行"。

## ⚠️ 除权日: 为什么必须取源给的 `change_pct`, 不能自算

实测 600177 @ 2026-09-18(**含除权**): 前一日 close=**8.30**, 当日 close=**8.14**。
- **自算** `(8.14/8.30-1)` = **-1.93%** ← 用未调整前收, **错**
- **引擎** `pct_chg` = **+0.49%** ← 用**除权调整后** pre_close=8.10
- **baostock** `pctChg` = **+0.4938%** ← 与引擎一致(它也给 `preclose=8.10`)

⇒ 两源在这一天**是一致的**, 但只有**取源字段**才对; 自算会得出错误结论。
这也说明"除权日"是**必须纳入样本**的区间 —— 无除权的区间上口径差异被掩盖。
"""
from __future__ import annotations

#: 判据: 每源相对引擎的**预期** volume 比值(写死, 不是"看着接近")。
#: 依据见模块 docstring; 两者混用而不换算会得到差 100 倍的量且无任何报错。
EXPECTED_VOLUME_RATIO = {
    "stockdb_sdk": 1.0,    # 基准自身
    "baostock": 1.0,       # 同为"股"(实测 1.0000)
    "akshare": 100.0,      # akshare 是"手"(实测 100.0001)
}
#: OHLC 逐值比对容差(两源都是两位小数报价, 给一点浮点余量)
PRICE_TOL = 1e-4
#: change_pct 容差(**相对**): baostock 给 6 位小数, 引擎给 2 位
PCT_TOL = 0.02
#: volume 比值容差: akshare 的"手"是整数单位, 会截掉不足 1 手的部分(实测差 28 股)。
#: 故换算后**不可能**严格相等, 允许 ≤100 股(即 1 手)的差。
VOLUME_ABS_TOL = 100.0


def _f(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def cross_validate(engine_rows: dict, other_rows: dict, *, source: str,
                   volume_abs_tol: float = VOLUME_ABS_TOL) -> dict:
    """把**同一批 (symbol, day)** 的两源数据逐值比对, 按**写死的预期**判定。

    `engine_rows` / `other_rows`: `{(symbol, day): {open,high,low,close,volume,amount,change_pct}}`
    (键统一为裸 6 位代码与 `YYYYMMDD`)。

    返回 {'ok', 'source', 'n_compared', 'mismatches', 'checks', 'volume_ratio'}
    · `checks` 列出每条**声明的判据**及其通过与否 —— 让"校验了什么"可审计;
    · `volume_ratio` 是实测比值, 供与 `EXPECTED_VOLUME_RATIO[source]` 对照。
    """
    exp_ratio = EXPECTED_VOLUME_RATIO.get(source)
    out = {"ok": True, "source": source, "n_compared": 0, "mismatches": [],
           "checks": [], "volume_ratio": None, "volume_expected": exp_ratio}
    keys = sorted(set(engine_rows) & set(other_rows))
    if not keys:
        out.update({"ok": False,
                    "checks": [{"name": "有可比对的交集", "ok": False,
                                "detail": "两源没有任何共同 (symbol, day)"}]})
        return out
    out["n_compared"] = len(keys)

    price_bad, pct_bad, vol_bad = [], [], []
    ratios = []
    for k in keys:
        e, o = engine_rows[k], other_rows[k]
        for f in ("open", "high", "low", "close"):
            ev, ov = _f(e.get(f)), _f(o.get(f))
            if ev is None or ov is None or abs(ev - ov) > PRICE_TOL:
                price_bad.append((k, f, ev, ov))
        ep, op = _f(e.get("change_pct")), _f(o.get("change_pct"))
        if ep is not None and op is not None and abs(ep - op) > PCT_TOL:
            pct_bad.append((k, ep, op))
        ev, ov = _f(e.get("volume")), _f(o.get("volume"))
        if ev is not None and ov and ov > 0:
            ratios.append(ev / ov)
            if exp_ratio == 1.0 and abs(ev - ov) > volume_abs_tol:
                vol_bad.append((k, ev, ov))
    if ratios:
        out["volume_ratio"] = sum(ratios) / len(ratios)

    checks = [
        {"name": f"OHLC 逐值相等(容差 {PRICE_TOL:g})", "ok": not price_bad,
         "detail": f"{len(keys) - len({b[0] for b in price_bad})}/{len(keys)} 组一致"
                   if price_bad else f"{len(keys)}/{len(keys)} 组一致",
         "bad": price_bad[:5]},
        {"name": f"change_pct 一致(容差 {PCT_TOL:g})", "ok": not pct_bad,
         "detail": "一致" if not pct_bad else f"{len(pct_bad)} 处不一致",
         "bad": pct_bad[:5],
         "note": "除权日必须取源的 change_pct, 自算会错(见模块 docstring)"},
    ]
    if exp_ratio is not None:
        got = out["volume_ratio"]
        ratio_ok = got is not None and abs(got - exp_ratio) <= max(0.01, exp_ratio * 0.01)
        checks.append({
            "name": f"volume 比值 == 预期 {exp_ratio:g}", "ok": ratio_ok,
            "detail": f"实测 {got:.4f}" if got is not None else "无样本",
            "expected": exp_ratio, "got": got})
        if exp_ratio == 1.0:
            checks.append({"name": f"volume 逐值相近(容差 {volume_abs_tol:g} 股)",
                           "ok": not vol_bad, "bad": vol_bad[:5],
                           "detail": "股 vs 股, 应逐值相等" if not vol_bad
                                     else f"{len(vol_bad)} 处超出容差"})
    out["checks"] = checks
    out["mismatches"] = [c for c in checks if not c["ok"]]
    out["ok"] = not out["mismatches"]
    return out


def summarize(result: dict) -> str:
    """把校验结果压成一行, 便于回执/日志。"""
    tag = "OK" if result.get("ok") else "**不一致**"
    vr = result.get("volume_ratio")
    return (f"[cross_validate {result.get('source')}] {tag}  "
            f"比对 {result.get('n_compared')} 组  "
            f"volume比值={('%.4f' % vr) if vr is not None else 'n/a'} "
            f"(预期 {result.get('volume_expected')})  "
            f"失败判据={[m['name'] for m in result.get('mismatches') or []]}")


def _main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="多源交叉校验: 判据与预期值")
    ap.add_argument("--criteria", action="store_true", help="打印声明的判据")
    args = ap.parse_args(argv)
    if args.criteria:
        print(json.dumps({
            "expected_volume_ratio": EXPECTED_VOLUME_RATIO,
            "price_tol": PRICE_TOL, "pct_tol": PCT_TOL,
            "volume_abs_tol": VOLUME_ABS_TOL,
            "note": ("volume 比值是**写死的预期**, 不是'看着接近': "
                     "baostock/引擎同为股 => 1.0; akshare 是手 => 100.0。"
                     "哪天源改了单位, 等式立刻不成立。"),
        }, ensure_ascii=False, indent=2))
        return 0
    print("多源交叉校验骨架。用 --criteria 看声明判据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

# -*- coding: utf-8 -*-
"""估值覆盖率哨兵: 每日盘后校验 valuation 的 **可用 ep 率** / pb / float_shares 覆盖率.

为什么需要
    2026-09-13 的 pe_ttm 诊断发现: 主表 `valuation` 的 `pe_ttm` 在部分交易日**整体缺失**
    (2025 年均值 28.5%、2026 年均值 5.0%, 2026-03-05 只有 0.4%), 而同期 `pb` 始终 100%。
    这类缺口是**静默**的: 上层 `ep` 因子只是"没有值", 既不报错也不降级, 融合分会悄悄
    退化成三因子而不自知(这正是 2026-03-05 窗口失真的原因)。
    2026-09-14 Step4 重建后, 近似行 (`source='approx_pb_rebuild'`) 也开始计算 PE,
    缺口已被根本修复; 补丁 `data/pit/pe_patch/*.parquet` 仅作过渡兜底。

判据口径的关键修正 (2026-09-14 观察期首日校准)
    原判据用 `pe_ttm` 的**非空率**, 会把"结构上限"误判成"上游缺口"。
    实测 (2026-08-20 ~ 09-08):

        source            行数    非空率    pe>0率   pe<0率
        approx_pb_rebuild 60183   70.6%     70.6%    0.0%
        snapshot          12593  100.0%     72.0%   28.0%

    —— 两个源的**可用 ep 率其实都是 ~70-72%**: 差别只在表示方式。重建把亏损公司的
    负 PE 置为 NULL, 而快照保留为负数; 而 `ep = 1/pe (pe>0)` 在两种情形下都取不到值。
    因此哨兵必须以 **可用 ep 率 (pe_ttm > 0, 合并补丁后)** 为主判据, 非空率仅作参考。

判据(可用环境变量覆盖)
    可用 ep 率 < 45%          -> CRITICAL  因子层已实质退化
    非空率 < 50% 且可用 ep >= 60% -> WARN   上游写入缺口(补丁兜底中, 需补上游)
    可用 ep 率 < 60%          -> WARN      低于结构区间(实测 0.65~0.72)
    pb      < 95%             -> CRITICAL  估值表大面积缺失
    float_shares < 90%        -> WARN      影响 ln_size 市值中性化

用法
    python scripts/valuation_coverage_sentinel.py [--days 20] [--json PATH] [--quiet]
退出码: 存在 CRITICAL 时为 1(便于定时任务/CI 捕获), 否则 0。
建议调度: 每日盘后(如 18:30)执行一次; 告警写入 logs/valuation_coverage.log, 若设置
    DING_WEBHOOK_URL 则同时转发钉钉(复用 ops/alert_hook 的通道)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)
sys.path.insert(0, os.path.join(_BASE, "src"))

# 阈值(可用环境变量覆盖)
TH_EP_CRIT = float(os.environ.get("VAL_EP_CRIT", "0.45"))       # 可用 ep 率 CRITICAL
TH_EP_WARN = float(os.environ.get("VAL_EP_WARN", "0.60"))       # 可用 ep 率 WARN
TH_PE_GAP = float(os.environ.get("VAL_PE_GAP", "0.50"))         # 非空率"上游缺口"门槛
TH_PB_CRIT = float(os.environ.get("VAL_PB_CRIT", "0.95"))
TH_FS_WARN = float(os.environ.get("VAL_FS_WARN", "0.90"))
OUT_JSON = os.path.join(_BASE, "data", "valuation_coverage.json")
OUT_LOG = os.path.join(_BASE, "logs", "valuation_coverage.log")


def classify(ep_usable: float, raw_pe: float, raw_pb: float,
             raw_fs: float) -> tuple[str, str]:
    """返回 (级别, 原因). 纯函数便于测试.

    ep_usable 合并补丁后**可算 ep** 的比例(pe_ttm > 0)
    raw_pe    主表 pe_ttm 非空率(仅参考; 重建日天然≈ep_usable)
    """
    if raw_pb < TH_PB_CRIT:
        return "CRITICAL", (f"pb 覆盖率 {raw_pb:.1%} < {TH_PB_CRIT:.0%}: "
                            f"估值表大面积缺失")
    if ep_usable < TH_EP_CRIT:
        return "CRITICAL", (f"可用 ep 率 {ep_usable:.1%} < {TH_EP_CRIT:.0%}: "
                            f"因子层已实质退化(结构区间 65%~72%)")
    if raw_pe < TH_PE_GAP and ep_usable >= TH_EP_WARN:
        return "WARN", (f"主表 pe_ttm 非空率 {raw_pe:.1%} < {TH_PE_GAP:.0%} 但可用 ep "
                        f"{ep_usable:.1%}: 上游写入缺口(补丁已兜底)")
    if ep_usable < TH_EP_WARN:
        return "WARN", (f"可用 ep 率 {ep_usable:.1%} < {TH_EP_WARN:.0%}: "
                        f"低于结构区间(实测 65%~72%)")
    if raw_fs < TH_FS_WARN:
        return "WARN", f"float_shares 覆盖率 {raw_fs:.1%} < {TH_FS_WARN:.0%}: 影响 ln_size"
    return "OK", ""


def _s6(x) -> str:
    return str(x or "").split(".")[0].zfill(6)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20, help="检查最近 N 个交易日")
    ap.add_argument("--json", default=OUT_JSON)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    # 在 main 内切工作目录(不在 import 时切, 保证本模块可被测试安全导入)
    os.chdir(_BASE)

    from factor_fusion import _pe_patch_all, _sql

    # 取 symbol 级明细: 可用 ep 需要"主表 ∪ 补丁"的并集, 单靠计数算不出来。
    df = _sql(f"""
        SELECT CAST(ts AS DATE) d, symbol, pe_ttm, pb, float_shares, source
        FROM valuation
        WHERE CAST(ts AS DATE) >= (SELECT CAST(MAX(ts) AS DATE) FROM valuation)
                                    - INTERVAL {int(a.days) * 3} DAY
        ORDER BY 1 DESC
    """)
    if df.empty:
        print("[哨兵] valuation 表无数据, 视为 CRITICAL")
        return 1
    df["d"] = df["d"].astype(str).str.slice(0, 10)
    df["s6"] = df["symbol"].map(_s6)
    df["pe_ttm"] = (df["pe_ttm"].astype("float64") if hasattr(df["pe_ttm"], "astype")
                    else df["pe_ttm"])
    days = sorted(df["d"].unique(), reverse=True)[:int(a.days)]

    # 补丁: 每日 pe_ttm > 0 的 symbol 集合
    patch = _pe_patch_all()
    patch_pos: dict[str, set] = {}
    if patch is not None and len(patch):
        pp = patch.assign(
            d=patch["d"].dt.strftime("%Y-%m-%d"),
            s6=patch["sym_i"].map(_s6),
            pos=(patch["pe_ttm"] > 0),
        )
        pp = pp[pp["pos"] & pp["d"].isin(days)]
        for d, grp in pp.groupby("d"):
            patch_pos[str(d)] = set(grp["s6"])

    rows = []
    for d in days:
        g = df[df["d"] == d]
        n = max(len(g), 1)
        pe = g["pe_ttm"]
        raw_pe = float(pe.notna().sum()) / n
        main_pos = set(g.loc[pe > 0, "s6"])
        union_pos = main_pos | patch_pos.get(d, set())
        ep_usable = len(union_pos) / n
        raw_pb = float(g["pb"].notna().sum()) / n
        raw_fs = float(g["float_shares"].notna().sum()) / n
        lvl, why = classify(ep_usable, raw_pe, raw_pb, raw_fs)
        src = (g["source"].value_counts().to_dict() if "source" in g.columns else {})
        rows.append({"day": d, "n": len(g), "raw_pe": round(raw_pe, 4),
                     "ep_usable": round(ep_usable, 4),
                     "patch_only": len(union_pos - main_pos),
                     "raw_pb": round(raw_pb, 4), "raw_fs": round(raw_fs, 4),
                     "sources": {str(k): int(v) for k, v in src.items()},
                     "level": lvl, "why": why})

    worst = min(rows, key=lambda x: x["ep_usable"])
    crit = [x for x in rows if x["level"] == "CRITICAL"]
    warn = [x for x in rows if x["level"] == "WARN"]
    med = sorted(x["ep_usable"] for x in rows)[len(rows) // 2]

    if not a.quiet:
        print(f"[哨兵] 最近 {len(rows)} 个交易日 (可用 ep = 主表 pe>0 ∪ 补丁)")
        print(f"{'日期':<12}{'行数':>7}{'可用ep':>9}{'补丁独有':>9}{'非空率':>9}"
              f"{'pb':>8}{'float_sh':>10}{'级别':>10}")
        for x in rows:
            print(f"{x['day']:<12}{x['n']:>7}{x['ep_usable']:>9.1%}{x['patch_only']:>9}"
                  f"{x['raw_pe']:>9.1%}{x['raw_pb']:>8.1%}{x['raw_fs']:>10.1%}"
                  f"{x['level']:>10}")
        print(f"\n可用 ep 中位 {med:.1%}  最低 {worst['day']} {worst['ep_usable']:.1%}  "
              f"CRITICAL {len(crit)} / WARN {len(warn)}")

    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    rec = {"checked_at": datetime.now().isoformat(timespec="seconds"),
           "days": len(rows), "crit": len(crit), "warn": len(warn),
           "ep_median": round(med, 4), "worst": worst, "rows": rows}
    old = []
    if os.path.exists(a.json):
        try:
            old = json.load(open(a.json, encoding="utf-8"))
            if not isinstance(old, list):
                old = [old]
        except Exception:  # noqa: BLE001
            old = []
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump((old + [rec])[-120:], f, ensure_ascii=False, indent=2)

    if crit or warn:
        os.makedirs(os.path.dirname(OUT_LOG), exist_ok=True)
        lines = [f"[{rec['checked_at']}] 估值覆盖率哨兵: "
                 f"CRITICAL {len(crit)} / WARN {len(warn)}"]
        for x in (crit + warn):
            lines.append(f"  {x['level']} {x['day']}: 可用ep {x['ep_usable']:.1%} "
                         f"pb {x['raw_pb']:.1%} | {x['why']}")
        with open(OUT_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        try:
            from ops.alert_hook import _forward_ding, _log_line
            for ln in lines:
                _log_line("[sentinel] " + ln)
            _forward_ding(lines)
        except Exception as e:  # noqa: BLE001
            print(f"[哨兵] 告警通道不可用({type(e).__name__}: {str(e)[:80]}), "
                  f"已写入 {OUT_LOG}")
    return 1 if crit else 0


if __name__ == "__main__":
    sys.exit(main())

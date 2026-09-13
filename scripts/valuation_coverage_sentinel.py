# -*- coding: utf-8 -*-
"""估值覆盖率哨兵: 每日盘后校验 valuation 主表的 pe_ttm / pb / float_shares 覆盖率.

为什么需要
    2026-09-13 的 pe_ttm 诊断发现: 主表 `valuation` 的 `pe_ttm` 在部分交易日**整体缺失**
    (2025 年均值 28.5%、2026 年均值 5.0%, 2026-03-05 只有 0.4%), 而同期 `pb` 始终 100%。
    这类缺口是**静默**的: 上层 `ep` 因子只是"没有值", 既不报错也不降级, 融入分会
    悄悄退化成三因子而不自知(这正是 2026-03-05 窗口失真的原因)。
    补丁 `data/pit/pe_patch/*.parquet` 能把数值补回来, 但补丁会**掩盖**上游故障,
    因此哨兵必须校验**主表原始覆盖率**, 而不是补丁之后的覆盖率 —— 否则上游坏掉
    也不会被发现(补丁是"兜底", 不是"修复上游")。

判据(可用环境变量覆盖)
    主表 pe_ttm < 50% 且补丁后可用 ep < 60%  -> CRITICAL  因子层已实质退化
    主表 pe_ttm < 50%                        -> WARN      上游缺口(补丁兜底中, 需补上游)
    主表 pe_ttm < 70%                        -> WARN      低于历史区间(77%~90%)
    主表 pb      < 95%                       -> CRITICAL  估值表大面积缺失
    主表 float_shares < 90%                  -> WARN      影响 ln_size 市值中性化

用法
    python scripts/valuation_coverage_sentinel.py [--days 20] [--json PATH] [--quiet]
退出码: 存在 CRITICAL 时为 1(便于定时任务/CI 捕获), 否则 0。
建议调度: 每日盘后(如 18:30)执行一次; 告警写入 logs/alerts.log, 若设置
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
TH_PE_CRIT = float(os.environ.get("VAL_PE_CRIT", "0.50"))
TH_PE_WARN = float(os.environ.get("VAL_PE_WARN", "0.70"))
TH_PATCH_USABLE = float(os.environ.get("VAL_PATCH_USABLE", "0.60"))
TH_PB_CRIT = float(os.environ.get("VAL_PB_CRIT", "0.95"))
TH_FS_WARN = float(os.environ.get("VAL_FS_WARN", "0.90"))
OUT_JSON = os.path.join(_BASE, "data", "valuation_coverage.json")
OUT_LOG = os.path.join(_BASE, "logs", "valuation_coverage.log")


def classify(raw_pe: float, patched_usable: float, raw_pb: float,
             raw_fs: float) -> tuple[str, str]:
    """返回 (级别, 原因). 纯函数便于测试.

    raw_pe        主表 pe_ttm 非空率
    patched_usable 合并补丁后**可算 ep** 的比例(即 pe_ttm > 0 的比例)
    """
    if raw_pb < TH_PB_CRIT:
        return "CRITICAL", (f"pb 覆盖率 {raw_pb:.1%} < {TH_PB_CRIT:.0%}: "
                            f"估值表大面积缺失")
    if raw_pe < TH_PE_CRIT and patched_usable < TH_PATCH_USABLE:
        return "CRITICAL", (f"pe_ttm 覆盖率 {raw_pe:.1%} < {TH_PE_CRIT:.0%} 且补丁后"
                            f"可用 ep 仅 {patched_usable:.1%}: 因子层已实质退化")
    if raw_pe < TH_PE_CRIT:
        return "WARN", (f"pe_ttm 覆盖率 {raw_pe:.1%} < {TH_PE_CRIT:.0%}: 上游写入缺口"
                        f"(补丁已兜底, 可算 ep {patched_usable:.1%})")
    if raw_pe < TH_PE_WARN:
        return "WARN", f"pe_ttm 覆盖率 {raw_pe:.1%} 低于历史区间(77%~90%)"
    if raw_fs < TH_FS_WARN:
        return "WARN", f"float_shares 覆盖率 {raw_fs:.1%} < {TH_FS_WARN:.0%}: 影响 ln_size"
    return "OK", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20, help="检查最近 N 个交易日")
    ap.add_argument("--json", default=OUT_JSON)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    # 在 main 内切工作目录(不在 import 时切, 保证本模块可被测试安全导入)
    os.chdir(_BASE)

    from factor_fusion import _pe_patch_all, _sql

    df = _sql(f"""
        SELECT CAST(ts AS DATE) d, COUNT(*) n, COUNT(pe_ttm) n_pe,
               COUNT(pb) n_pb, COUNT(float_shares) n_fs
        FROM valuation GROUP BY 1 ORDER BY 1 DESC LIMIT {int(a.days)}
    """)
    if df.empty:
        print("[哨兵] valuation 表无数据, 视为 CRITICAL")
        return 1

    # 补丁(合并后)每日"可算 ep"的比例 —— 即 pe_ttm > 0 的占比
    patch = _pe_patch_all()
    usable = {}
    if len(patch):
        g = (patch.assign(pos=(patch["pe_ttm"] > 0).astype(int))
                  .groupby(patch["d"].dt.strftime("%Y-%m-%d"))["pos"].mean())
        usable = {k: float(v) for k, v in g.items()}

    rows = []
    for r in df.itertuples(index=False):
        d = str(r.d)
        n = max(int(r.n), 1)
        raw_pe = int(r.n_pe) / n
        raw_pb = int(r.n_pb) / n
        raw_fs = int(r.n_fs) / n
        pu = usable.get(d, 0.0)
        lvl, why = classify(raw_pe, pu, raw_pb, raw_fs)
        rows.append({"day": d, "n": int(r.n), "raw_pe": round(raw_pe, 4),
                     "raw_pb": round(raw_pb, 4), "raw_fs": round(raw_fs, 4),
                     "patched_usable_ep": round(pu, 4), "level": lvl, "why": why})

    worst = min(rows, key=lambda x: x["raw_pe"])
    crit = [x for x in rows if x["level"] == "CRITICAL"]
    warn = [x for x in rows if x["level"] == "WARN"]
    med = sorted(x["raw_pe"] for x in rows)[len(rows) // 2]

    if not a.quiet:
        print(f"[哨兵] 最近 {len(rows)} 个交易日 (主表原始覆盖率)")
        print(f"{'日期':<12}{'行数':>7}{'pe_ttm':>9}{'pb':>8}{'float_sh':>10}"
              f"{'补丁后可用ep':>13}{'级别':>10}")
        for x in rows:
            print(f"{x['day']:<12}{x['n']:>7}{x['raw_pe']:>9.1%}{x['raw_pb']:>8.1%}"
                  f"{x['raw_fs']:>10.1%}{x['patched_usable_ep']:>13.1%}{x['level']:>10}")
        print(f"\npe_ttm 中位 {med:.1%}  最低 {worst['day']} {worst['raw_pe']:.1%}  "
              f"CRITICAL {len(crit)} / WARN {len(warn)}")

    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    rec = {"checked_at": datetime.now().isoformat(timespec="seconds"),
           "days": len(rows), "crit": len(crit), "warn": len(warn),
           "pe_median": round(med, 4), "worst": worst, "rows": rows}
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
            lines.append(f"  {x['level']} {x['day']}: pe_ttm {x['raw_pe']:.1%} "
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

# -*- coding: utf-8 -*-
"""把「行情数据断供期」记入降级账本 `data/drl_degrade_events.jsonl`（用户 2026-09-20 要求）。

用户第四步的决定
  · 断供期（09-05 至恢复日）的 target_plan：**不产出**（推荐）—— 避免用陈旧截面伪造日常产物污染台账;
  · 并在 `drl_degrade_events.jsonl` 记录断供期。

为什么只记**一条窗口事件**而不是逐日
  账本里"哪些天是交易日"的依据是 `data/trade_calendar.json`; 而该日历是**从数据派生的**
  （实测 `generated_at=2026-09-19T19:12` 但 `last=2026-09-08`, 即它忠实反映了"数据只到 09-08"）。
  ⇒ **09-08 之后的交易日无法枚举**。逐日写会变成"按我们猜的日子编事件", 反而制造假精度。
  故只写一条窗口事件, 并在 `affected_days` 里列出**可枚举**的日子, 同时用
  `enumeration_limited: true` 明确标注"其后无法枚举"。

幂等: 若账本里已存在**同窗口**的 `data_outage` 记录, 则跳过（不重复记账）。

用法
  python scripts/record_data_outage.py --from 2026-09-05 --to 2026-09-20            # 预演
  python scripts/record_data_outage.py --from 2026-09-05 --to 2026-09-20 --apply    # 落盘
退出码: 0 = 成功（含"已存在故跳过"）; 1 = 参数/环境错误
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

ACTION = ("**不产出断供期的 target_plan**（避免用陈旧截面伪造日常产物污染台账）; "
          "待上游恢复后按用户第三步流程验证并重启 daemon")


def _akshare_state() -> dict:
    """AKShare 兜底路径的**运行时**可用性。

    [2026-09-20 订正] 原实现把『AKShare 兜底未装』**硬编码**在 REASON 字符串里, 后果是
    **写入即错**: 本账本 2026-09-20 20:30:58 的那条记录断言"未装", 而 akshare 1.18.88
    早在 **20:30:08** 就已装入 `.venv310` —— 文本比事实晚了 50 秒, 且此后会**永久**说谎
    (账本是 append-only, 没人会回头改)。教训与 METHOD-1 同源: 凡"环境事实"都必须**当场探测**,
    不得写死在字符串常量里。
    """
    import importlib.util as u
    try:
        spec = u.find_spec("akshare")
    except Exception as e:  # noqa: BLE001
        return {"installed": None, "version": None, "interpreter": sys.executable,
                "probe_error": f"{type(e).__name__}: {e}"}
    ver = None
    if spec is not None:
        try:
            import akshare as ak
            ver = getattr(ak, "__version__", None)
        except Exception:  # noqa: BLE001  装了但导入失败也算"不可用"
            ver = None
    return {"installed": bool(spec), "version": ver, "interpreter": sys.executable}


def _reason(ak: dict) -> str:
    """按**探测结果**拼装断供原因, 不预设兜底路径的状态。"""
    if ak.get("installed") and ak.get("version"):
        ak_txt = (f"AKShare 兜底**已装**(akshare {ak['version']}, 解释器 {ak['interpreter']})"
                  f"但**尚未验证能否真正产出** ⇒ 不计入『已恢复』的证据")
    elif ak.get("installed"):
        ak_txt = (f"AKShare 兜底**已装但 import 失败**(解释器 {ak['interpreter']}) "
                  f"⇒ 实际不可用")
    else:
        ak_txt = f"AKShare 兜底未装(解释器 {ak['interpreter']} 无 akshare)"
    return ("行情数据断供: 上游 free-stockdb 存储损坏(09-05 17:55 leveldb Corruption/remote "
            "unavailable) + 更新器停摆 + " + ak_txt + " + legacy DuckDB 已退役 "
            "⇒ h5i daily_bars 无新数据（登记册 P1-DATA-STALE）")


def _d8(s: str) -> str:
    return str(s or "").replace("-", "")


def _resolution_reason(lo: str, hi: str) -> str:
    """**当场探测**恢复状态再写结论（不写死）。

    与 `_akshare_state` 同一条教训: 恢复与否是**环境事实**, 必须现场量。
    """
    parts = []
    try:
        import h5i_sync
        parts.append(f"h5i daily_bars MAX(date)={h5i_sync.max_bar_date(force=True)}")
    except Exception as e:  # noqa: BLE001
        parts.append(f"h5i 读取失败({type(e).__name__}: {e})")
    try:
        import engine_bars_sync as E
        p = E.engine_available()
        tag = f"引擎探针 ok={p.get('ok')} day={p.get('day')}"
        if p.get("error"):
            tag += f" err={p['error']}"
        parts.append(tag)
    except Exception as e:  # noqa: BLE001
        parts.append(f"引擎探针异常({type(e).__name__}: {e})")
    return (f"行情数据已恢复: " + "; ".join(parts)
            + f"; 断供窗口 {lo}..{hi} 已由 src/engine_bars_sync.py 经厂商 SDK 直连补齐"
            + "（登记册 P1-DATA-STALE / P1-MIRRORDEAD）")


def _record_resolution(lo: str, hi: str, apply: bool) -> int:
    """追加一条『断供已恢复』记录。幂等: 同窗口已有 resolved 记录则跳过。"""
    import drl_degrade as D
    lp = D.event_ledger_path()
    if os.path.isfile(lp):
        try:
            with open(lp, encoding="utf-8-sig") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("kind") == "data_outage_resolved" and r.get("window") == [lo, hi]:
                        print(f"[跳过] 已有同窗口恢复记录 (at={r.get('at')}) —— 幂等")
                        return 0
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 读取既有账本失败, 按未记录处理: {type(e).__name__}: {e}")

    reason = _resolution_reason(lo, hi)
    print("=" * 78)
    print("记录『行情断供已恢复』")
    print("=" * 78)
    print(f"  窗口   : {lo}..{hi}")
    print(f"  原因   : {reason}")
    if not apply:
        print("\n(--预演: 未写账本; 加 --apply 落盘)")
        return 0
    rec = D.record_event(
        D.LEVEL_OK, reason,
        "恢复常规产出: 后续 target_plan 由引擎直连数据生成, 不再有断供豁免",
        hi, extra={"kind": "data_outage_resolved", "model_degrade": False,
                   "window": [lo, hi],
                   "source": "engine_bars_sync (stockdb.exe SDK 直连)",
                   "ref": "登记册 P1-DATA-STALE / P1-MIRRORDEAD / P1-ENGINEDEP"})
    print(f"\n[已记录] at={rec.get('at')} level={rec.get('level')} kind=resolved")
    print(f"  账本现有事件数: {D.event_count()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", required=True, help="断供起 YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", required=True, help="断供止 YYYY-MM-DD（含）")
    ap.add_argument("--apply", action="store_true", help="真正写账本（默认预演）")
    ap.add_argument("--resolve", action="store_true",
                    help="改记『断供已恢复』（幂等; 恢复后应记这条, 以便 last_event 不再停留在断供态）")
    args = ap.parse_args()

    lo, hi = _d8(args.d_from), _d8(args.d_to)
    if not (lo.isdigit() and hi.isdigit() and len(lo) == 8 and len(hi) == 8 and lo <= hi):
        print("[FAIL] 日期区间非法")
        return 1

    if args.resolve:
        return _record_resolution(lo, hi, apply=args.apply)

    import config
    import drl_degrade as D
    cal = os.path.join(config.DATA_DIR, "trade_calendar.json")
    days: "list[str]" = []
    cal_last = None
    if os.path.isfile(cal):
        with open(cal, encoding="utf-8-sig") as f:
            j = json.load(f)
        all_days = [str(x).replace("-", "") for x in (j.get("days") or [])]
        days = [d for d in all_days if lo <= d <= hi]
        cal_last = all_days[-1] if all_days else None

    print("=" * 78)
    print("记录行情断供期 -> 降级账本")
    print("=" * 78)
    print(f"  窗口        : {lo} .. {hi}")
    print(f"  账本        : {D.event_ledger_path()}")
    print(f"  日历末条    : {cal_last}  (日历由数据派生, 故其末条即数据的最后一个交易日)")

    print("\n  --- 窗口内可枚举的交易日及 target_plan 状态 ---")
    affected, have_plan = [], []
    for d in days:
        tp = os.path.join(config.DATA_DIR, "drl", d, "target_plan.json")
        src = None
        if os.path.isfile(tp):
            try:
                with open(tp, encoding="utf-8-sig") as f:
                    src = json.load(f).get("source") or "(normal)"
            except Exception:  # noqa: BLE001
                src = "?"
            have_plan.append(d)
        else:
            affected.append(d)
        print(f"    {d}  target_plan={'有' if os.path.isfile(tp) else '无'}"
              f"{'  source=' + str(src) if src else ''}")

    enum_limited = bool(cal_last and cal_last < hi)
    print(f"\n  可枚举但无 plan 的交易日: {affected or '（无）'}")
    print(f"  已有 plan（如回补）: {have_plan or '（无）'}")
    print(f"  枚举受限（日历末条 {cal_last} < 窗口止 {hi}）: {enum_limited}")

    # 兜底路径状态**当场探测**（见 _akshare_state 的订正说明）
    ak = _akshare_state()
    print(f"\n  --- 兜底路径 AKShare（当场探测, 不写死）---")
    print(f"    解释器      : {ak.get('interpreter')}")
    print(f"    已装        : {ak.get('installed')}")
    print(f"    版本        : {ak.get('version')}")
    if ak.get("probe_error"):
        print(f"    探测异常    : {ak['probe_error']}")
    if ak.get("installed") and ak.get("version"):
        print("    注: 已装 **不等于** 该路径能产出 —— 需实跑 update_all 验证后才可当作已恢复。")

    extra = {"kind": "data_outage", "model_degrade": False,
             "window": [lo, hi], "affected_days": affected,
             "days_with_plan": have_plan, "calendar_last_day": cal_last,
             "enumeration_limited": enum_limited,
             "akshare": ak,
             "upstream": "free-stockdb (E:\\A_stockDB)",
             "ref": "登记册 P1-DATA-STALE / P2-LAKEROOT"}

    # 幂等: 同窗口已记过就跳过
    lp = D.event_ledger_path()
    if os.path.isfile(lp):
        try:
            with open(lp, encoding="utf-8-sig") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    r = json.loads(ln)
                    if r.get("kind") == "data_outage" and r.get("window") == [lo, hi]:
                        print(f"\n[跳过] 账本里已存在同窗口 data_outage 记录"
                              f"（at={r.get('at')}）—— 幂等, 不重复记账")
                        return 0
        except Exception as e:  # noqa: BLE001
            print(f"  [警告] 读取既有账本失败, 将按未记录处理: {type(e).__name__}: {e}")

    if not args.apply:
        print("\n(--预演: 未写账本; 加 --apply 落盘)")
        return 0

    rec = D.record_event(D.LEVEL_FALLBACK, _reason(ak), ACTION, hi, extra=extra)
    print(f"\n[已记录] at={rec.get('at')} level={rec.get('level')} "
          f"severity={rec.get('severity')} kind={rec.get('kind')}")
    print(f"  账本现有事件数: {D.event_count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
